"""Pipeline state transitions. Every function is one SQL transaction (retried only on deadlock) and every
`documents.status` / `stage_states` write is a guarded UPDATE: zero rows affected means someone else moved first."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable

import pyodbc

from ..config import Settings
from ..domain.errors import LeaseLost, StageError
from ..domain.stages import STAGE_QUEUES, STAGES, next_stage
from .connection import Database, is_unique_violation, rows_as_dicts


def new_id() -> str:
    return str(uuid.uuid4())


def stage_message(document_id: str, attempt_id: str, stage: str) -> dict:
    return {"v": 1, "document_id": str(document_id).lower(), "attempt_id": str(attempt_id).lower(), "stage": stage}


@dataclass(frozen=True)
class Claim:
    document_id: str
    attempt_id: str
    attempt_no: int
    stage: str
    try_no: int
    max_tries: int
    worker_id: str


# -- building blocks usable inside any transaction ---------------------------------------------------
def add_outbox(cur: pyodbc.Cursor, queue: str, body: dict, document_id: str | None = None,
               attempt_id: str | None = None, stage: str | None = None, not_before=None) -> str:
    oid = new_id()
    cur.execute(
        "INSERT dbo.outbox (outbox_id, queue, body, document_id, attempt_id, stage, not_before) "
        "VALUES (?,?,?,?,?,?, COALESCE(?, SYSUTCDATETIME()))",
        oid, queue, json.dumps(body, separators=(",", ":")), document_id, attempt_id, stage, not_before)
    return oid


def enqueue_stage(cur: pyodbc.Cursor, settings: Settings, document_id: str, attempt_id: str, stage: str) -> None:
    """Create the pending stage row and its outbox message, inside the caller's transaction."""
    cur.execute(
        "INSERT dbo.stage_states (attempt_id, stage, document_id, status, max_tries) VALUES (?,?,?,'pending',?)",
        attempt_id, stage, document_id, settings.max_tries(stage))
    add_outbox(cur, STAGE_QUEUES[stage], stage_message(document_id, attempt_id, stage), document_id, attempt_id, stage)


# -- arrival ------------------------------------------------------------------------------------
def create_upload_row(db: Database, settings: Settings, document_id: str, filename: str, content_type: str,
                      size: int, container: str, blob_name: str) -> None:
    def _do(cur):
        cur.execute(
            "INSERT dbo.documents (document_id, original_filename, content_type, size_bytes, blob_container, blob_name, "
            "source, status, current_attempt_no, retain_until) "
            "VALUES (?,?,?,?,?,?, 'upload','uploading',0, DATEADD(DAY, ?, CAST(SYSUTCDATETIME() AS DATE)))",
            document_id, filename[:260], content_type, size, container, blob_name, settings.retention_days)
    db.run(_do)


def mark_upload_failed(db: Database, document_id: str, code: str, message: str) -> bool:
    def _do(cur):
        cur.execute(
            "UPDATE dbo.documents SET status='upload_failed', last_error_code=?, last_error_message=?, updated_at=SYSUTCDATETIME() "
            "WHERE document_id=? AND status='uploading'", code, message[:500], document_id)
        return cur.rowcount == 1
    return db.run(_do)


def register_arrival(db: Database, settings: Settings, document_id: str, container: str, blob_name: str,
                     etag: str | None, size: int | None = None, content_type: str | None = None) -> bool:
    """Idempotent arrival. Returns True only for the call that elected itself winner and created attempt 1."""
    exists = db.scalar("SELECT COUNT(*) FROM dbo.documents WHERE document_id=?", document_id)
    if not exists:
        def _ins(cur):
            cur.execute(
                "INSERT dbo.documents (document_id, original_filename, content_type, size_bytes, blob_container, blob_name, "
                "source, status, current_attempt_no, retain_until) "
                "VALUES (?,?,?,?,?,?, 'blob_event','uploading',0, DATEADD(DAY, ?, CAST(SYSUTCDATETIME() AS DATE)))",
                document_id, blob_name.rsplit("/", 1)[-1][:260], content_type, size, container, blob_name, settings.retention_days)
        try:
            db.run(_ins)
        except pyodbc.IntegrityError as e:
            if not is_unique_violation(e):
                raise  # a concurrent arrival created the row first; continue to winner election

    def _win(cur):
        cur.execute(
            "UPDATE dbo.documents SET status='processing', current_attempt_no=1, blob_etag=?, last_error_code=NULL, "
            "last_error_message=NULL, updated_at=SYSUTCDATETIME() "
            "WHERE document_id=? AND current_attempt_no=0 AND status IN ('uploading','upload_failed')", etag, document_id)
        if cur.rowcount != 1:
            return False  # duplicate or late event: change nothing
        attempt_id = new_id()
        cur.execute("INSERT dbo.processing_attempts (attempt_id, document_id, attempt_no, trigger_kind) VALUES (?,?,1,'initial')",
                    attempt_id, document_id)
        enqueue_stage(cur, settings, document_id, attempt_id, "intake")
        return True
    return db.run(_win)


# -- claim / complete / fail ----------------------------------------------------------------------
def claim_stage(db: Database, settings: Settings, attempt_id: str, stage: str, worker_id: str,
                message_id: str | None = None) -> Claim | None:
    lease = settings.lease_for(stage)

    def _do(cur):
        cur.execute(
            "UPDATE ss SET status='running', tries=ss.tries+1, lease_owner=?, "
            "lease_expires_at=DATEADD(SECOND,?,SYSUTCDATETIME()), last_started_at=SYSUTCDATETIME(), "
            "first_started_at=COALESCE(ss.first_started_at,SYSUTCDATETIME()), updated_at=SYSUTCDATETIME() "
            "OUTPUT inserted.tries, inserted.document_id, inserted.max_tries, deleted.status, deleted.tries "
            "FROM dbo.stage_states ss "
            "WHERE ss.attempt_id=? AND ss.stage=? AND ss.tries < ss.max_tries "
            "AND EXISTS (SELECT 1 FROM dbo.processing_attempts a WHERE a.attempt_id=ss.attempt_id AND a.status='active') "
            "AND (ss.status='pending' "
            "  OR (ss.status='retry_wait' AND ss.next_run_at <= SYSUTCDATETIME()) "
            "  OR (ss.status='running' AND ss.lease_expires_at < SYSUTCDATETIME()))",
            worker_id, lease, attempt_id, stage)
        row = cur.fetchone()
        if row is None:
            return None
        tries, document_id, max_tries, prev_status, prev_tries = row
        if prev_status == "running":  # stole an expired lease: close the dead try first
            cur.execute(
                "UPDATE dbo.stage_tries SET finished_at=SYSUTCDATETIME(), outcome='lease_expired', error_code='LEASE_EXPIRED' "
                "WHERE attempt_id=? AND stage=? AND try_no=? AND finished_at IS NULL", attempt_id, stage, prev_tries)
        cur.execute(
            "INSERT dbo.stage_tries (attempt_id, stage, try_no, document_id, worker_id, message_id) VALUES (?,?,?,?,?,?)",
            attempt_id, stage, tries, document_id, worker_id, message_id)
        cur.execute("SELECT attempt_no FROM dbo.processing_attempts WHERE attempt_id=?", attempt_id)
        return Claim(str(document_id).lower(), str(attempt_id).lower(), cur.fetchone()[0], stage, tries, max_tries, worker_id)
    return db.run(_do)


def renew_lease(db: Database, settings: Settings, claim: Claim) -> bool:
    def _do(cur):
        cur.execute(
            "UPDATE dbo.stage_states SET lease_expires_at=DATEADD(SECOND,?,SYSUTCDATETIME()), updated_at=SYSUTCDATETIME() "
            "WHERE attempt_id=? AND stage=? AND status='running' AND lease_owner=? AND tries=?",
            settings.lease_for(claim.stage), claim.attempt_id, claim.stage, claim.worker_id, claim.try_no)
        return cur.rowcount == 1
    return db.run(_do)


def set_try_provider_request(db: Database, claim: Claim, request_id: str, http_status: int | None = None) -> None:
    db.run(lambda cur: cur.execute(
        "UPDATE dbo.stage_tries SET provider_request_id=?, http_status=COALESCE(?,http_status) WHERE attempt_id=? AND stage=? AND try_no=?",
        request_id, http_status, claim.attempt_id, claim.stage, claim.try_no))


def complete_stage(db: Database, settings: Settings, claim: Claim, effects: Callable[[pyodbc.Cursor], None] | None = None,
                   enqueue_next: bool = True) -> None:
    """Fenced completion. Raises LeaseLost (and rolls everything back) when this try no longer owns the lease."""
    def _do(cur):
        cur.execute(
            "UPDATE dbo.stage_states SET status='succeeded', completed_at=SYSUTCDATETIME(), lease_owner=NULL, lease_expires_at=NULL, "
            "last_error_code=NULL, last_error_message=NULL, updated_at=SYSUTCDATETIME() "
            "WHERE attempt_id=? AND stage=? AND status='running' AND lease_owner=? AND tries=?",
            claim.attempt_id, claim.stage, claim.worker_id, claim.try_no)
        if cur.rowcount != 1:
            raise LeaseLost()
        cur.execute("UPDATE dbo.stage_tries SET finished_at=SYSUTCDATETIME(), outcome='succeeded' "
                    "WHERE attempt_id=? AND stage=? AND try_no=?", claim.attempt_id, claim.stage, claim.try_no)
        if effects:
            effects(cur)
        nxt = next_stage(claim.stage)
        if nxt and enqueue_next:
            enqueue_stage(cur, settings, claim.document_id, claim.attempt_id, nxt)
    db.run(_do)


def record_failure(db: Database, claim: Claim, err: StageError, delay_s: int) -> str:
    """Returns 'retry' | 'failed' | 'rejected' | 'lease_lost'."""
    def _do(cur):
        will_retry = err.retryable and claim.try_no < claim.max_tries
        if will_retry:
            cur.execute(
                "UPDATE dbo.stage_states SET status='retry_wait', next_run_at=DATEADD(SECOND,?,SYSUTCDATETIME()), lease_owner=NULL, "
                "lease_expires_at=NULL, last_error_code=?, last_error_message=?, updated_at=SYSUTCDATETIME() "
                "OUTPUT inserted.next_run_at "
                "WHERE attempt_id=? AND stage=? AND status='running' AND lease_owner=? AND tries=?",
                delay_s, err.code, err.message, claim.attempt_id, claim.stage, claim.worker_id, claim.try_no)
            row = cur.fetchone()
            if row is None:
                return "lease_lost"
            _close_try(cur, claim, "retryable_error", err)
            _failure_artifacts(cur, claim, err)
            add_outbox(cur, STAGE_QUEUES[claim.stage], stage_message(claim.document_id, claim.attempt_id, claim.stage),
                       claim.document_id, claim.attempt_id, claim.stage, not_before=row[0])
            return "retry"
        cur.execute(
            "UPDATE dbo.stage_states SET status='failed', lease_owner=NULL, lease_expires_at=NULL, last_error_code=?, "
            "last_error_message=?, updated_at=SYSUTCDATETIME() "
            "WHERE attempt_id=? AND stage=? AND status='running' AND lease_owner=? AND tries=?",
            err.code, err.message, claim.attempt_id, claim.stage, claim.worker_id, claim.try_no)
        if cur.rowcount != 1:
            return "lease_lost"
        _close_try(cur, claim, "terminal_error", err)
        _failure_artifacts(cur, claim, err)
        doc_status = "rejected" if err.rejects_document else "failed"
        _fail_attempt(cur, claim.document_id, claim.attempt_id, claim.attempt_no, doc_status, err.code, err.message)
        return doc_status
    return db.run(_do)


def _close_try(cur, claim: Claim, outcome: str, err: StageError) -> None:
    cur.execute(
        "UPDATE dbo.stage_tries SET finished_at=SYSUTCDATETIME(), outcome=?, error_code=?, error_message=?, "
        "http_status=COALESCE(?,http_status), provider_request_id=COALESCE(?,provider_request_id) "
        "WHERE attempt_id=? AND stage=? AND try_no=?",
        outcome, err.code, err.message, err.http_status, err.provider_request_id, claim.attempt_id, claim.stage, claim.try_no)


def _fail_attempt(cur, document_id: str, attempt_id: str, attempt_no: int, doc_status: str, code: str, message: str) -> None:
    cur.execute("UPDATE dbo.processing_attempts SET status='failed', completed_at=SYSUTCDATETIME() WHERE attempt_id=? AND status='active'",
                attempt_id)
    cur.execute(
        "UPDATE dbo.documents SET status=?, last_error_code=?, last_error_message=?, updated_at=SYSUTCDATETIME() "
        "WHERE document_id=? AND status='processing' AND current_attempt_no=?",
        doc_status, code, message[:500], document_id, attempt_no)


# -- reaper -------------------------------------------------------------------------------------
def reap_expired_leases(db: Database) -> int:
    rows = db.query("SELECT attempt_id, stage, tries, max_tries, document_id FROM dbo.stage_states "
                    "WHERE status='running' AND lease_expires_at < SYSUTCDATETIME()")
    n = 0
    for r in rows:
        def _one(cur, r=r):
            cur.execute(
                "UPDATE dbo.stage_states SET status=CASE WHEN tries < max_tries THEN 'pending' ELSE 'failed' END, lease_owner=NULL, "
                "lease_expires_at=NULL, last_error_code=CASE WHEN tries < max_tries THEN 'LEASE_EXPIRED' ELSE 'CRASH_LOOP_EXHAUSTED' END, "
                "last_error_message=CASE WHEN tries < max_tries THEN 'worker lease expired' ELSE 'worker lease expired and tries are exhausted' END, "
                "updated_at=SYSUTCDATETIME() OUTPUT inserted.status "
                "WHERE attempt_id=? AND stage=? AND status='running' AND tries=? AND lease_expires_at < SYSUTCDATETIME()",
                r["attempt_id"], r["stage"], r["tries"])
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("UPDATE dbo.stage_tries SET finished_at=SYSUTCDATETIME(), outcome='lease_expired', error_code='LEASE_EXPIRED' "
                        "WHERE attempt_id=? AND stage=? AND try_no=? AND finished_at IS NULL", r["attempt_id"], r["stage"], r["tries"])
            if row[0] == "pending":
                add_outbox(cur, STAGE_QUEUES[r["stage"]], stage_message(r["document_id"], r["attempt_id"], r["stage"]),
                           r["document_id"], r["attempt_id"], r["stage"])
            else:
                cur.execute("SELECT attempt_no FROM dbo.processing_attempts WHERE attempt_id=?", r["attempt_id"])
                _fail_attempt(cur, r["document_id"], r["attempt_id"], cur.fetchone()[0], "failed", "CRASH_LOOP_EXHAUSTED",
                              "worker lease expired and tries are exhausted")
            return True
        if db.run(_one):
            n += 1
    return n


def reap_lost_stage_events(db: Database, recovery_s: int) -> int:
    """Belt and braces: a runnable stage with no pending outbox row and no recent one gets a fresh outbox row."""
    rows = db.query(
        "SELECT ss.attempt_id, ss.stage, ss.document_id FROM dbo.stage_states ss "
        "JOIN dbo.processing_attempts a ON a.attempt_id=ss.attempt_id AND a.status='active' "
        "WHERE ss.tries < ss.max_tries AND (ss.status='pending' OR ss.status='retry_wait') "
        "AND COALESCE(ss.next_run_at, ss.updated_at) < DATEADD(SECOND, -?, SYSUTCDATETIME()) "
        "AND NOT EXISTS (SELECT 1 FROM dbo.outbox o WHERE o.attempt_id=ss.attempt_id AND o.stage=ss.stage "
        "                AND (o.dispatched_at IS NULL OR o.created_at > DATEADD(SECOND, -?, SYSUTCDATETIME())))",
        recovery_s, recovery_s)
    n = 0
    for r in rows:
        def _one(cur, r=r):
            cur.execute(
                "SELECT COUNT(*) FROM dbo.stage_states WHERE attempt_id=? AND stage=? AND status IN ('pending','retry_wait')",
                r["attempt_id"], r["stage"])
            if not cur.fetchone()[0]:
                return False
            add_outbox(cur, STAGE_QUEUES[r["stage"]], stage_message(r["document_id"], r["attempt_id"], r["stage"]),
                       r["document_id"], r["attempt_id"], r["stage"])
            return True
        if db.run(_one):
            n += 1
    return n


def stale_uploads(db: Database, grace_s: int) -> list[dict]:
    return db.query(
        "SELECT document_id, blob_container, blob_name, size_bytes, content_type, "
        "DATEDIFF(SECOND, created_at, SYSUTCDATETIME()) AS age_s FROM dbo.documents "
        "WHERE status='uploading' AND current_attempt_no=0 AND created_at < DATEADD(SECOND, -?, SYSUTCDATETIME())", grace_s)


# -- replay -------------------------------------------------------------------------------------
def replay_document(db: Database, document_id: str, seen_attempt_no: int, from_stage: str | None, reason: str | None) -> dict:
    """Calls dbo.usp_replay_document (the only write path the web identity has for replay)."""
    if from_stage is not None and from_stage not in STAGES:
        return {"outcome": "BAD_STAGE", "attempt_id": None, "attempt_no": None}
    def _do(cur):
        cur.execute("SET NOCOUNT ON; EXEC dbo.usp_replay_document ?,?,?,?", document_id, seen_attempt_no, from_stage, (reason or "")[:500] or None)
        return rows_as_dicts(cur)[0]
    return db.run(_do)


# -- outbox dispatch ------------------------------------------------------------------------------
def claim_outbox(db: Database, worker_id: str, batch: int = 50, claim_s: int = 60) -> list[dict]:
    def _do(cur):
        cur.execute(
            ";WITH c AS (SELECT TOP (?) * FROM dbo.outbox WITH (UPDLOCK, READPAST, READCOMMITTEDLOCK) "
            "WHERE dispatched_at IS NULL AND not_before <= SYSUTCDATETIME() "
            "AND (claimed_until IS NULL OR claimed_until < SYSUTCDATETIME()) ORDER BY created_at) "
            "UPDATE c SET claimed_by=?, claimed_until=DATEADD(SECOND,?,SYSUTCDATETIME()), dispatch_attempts=dispatch_attempts+1 "
            "OUTPUT inserted.outbox_id, inserted.queue, inserted.body, inserted.stage, inserted.not_before, inserted.dispatch_attempts",
            batch, worker_id, claim_s)
        return rows_as_dicts(cur)
    return db.run(_do)


def mark_dispatched(db: Database, outbox_id: str) -> None:
    db.run(lambda cur: cur.execute("UPDATE dbo.outbox SET dispatched_at=SYSUTCDATETIME(), last_error=NULL WHERE outbox_id=? AND dispatched_at IS NULL", outbox_id))


def mark_dispatch_failed(db: Database, outbox_id: str, error_class: str, attempts: int) -> None:
    backoff = min(30, 2 ** min(attempts, 5))
    db.run(lambda cur: cur.execute(
        "UPDATE dbo.outbox SET last_error=?, claimed_until=DATEADD(SECOND,?,SYSUTCDATETIME()) WHERE outbox_id=?",
        error_class[:200], backoff, outbox_id))


# -- artifact helpers -----------------------------------------------------------------------------
def insert_artifact(cur, *, document_id: str, attempt_id: str | None, stage: str | None, try_no: int | None, kind: str,
                    container: str, blob_name: str, sha256: str, size: int, content_type: str,
                    provenance: dict | None = None) -> str:
    aid = new_id()
    cur.execute(
        "INSERT dbo.artifacts (artifact_id, document_id, attempt_id, stage, try_no, kind, container, blob_name, sha256, size_bytes, "
        "content_type, provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", aid, document_id, attempt_id, stage, try_no, kind, container,
        blob_name, sha256, size, content_type, json.dumps(provenance, sort_keys=True) if provenance else None)
    return aid


def _failure_artifacts(cur, claim: "Claim", err: StageError) -> None:
    """Evidence kept with the failure: the provider's final response bytes, stored unchanged by the handler."""
    for a in err.artifacts:
        insert_artifact(cur, document_id=claim.document_id, attempt_id=claim.attempt_id, stage=claim.stage, try_no=claim.try_no, **a)


def resolve_artifact(db: Database, attempt_id: str, kind: str) -> dict | None:
    """The artifact of `kind` visible to an attempt: its own, or the one it carried forward from an earlier attempt."""
    stage = {"provider_response": "analyze", "extracted_text": "analyze", "normalized_result": "index", "original": None}[kind]
    if stage is None:
        rows = db.query("SELECT a.* FROM dbo.artifacts a JOIN dbo.processing_attempts p ON p.document_id=a.document_id "
                        "WHERE p.attempt_id=? AND a.kind='original'", attempt_id)
        return rows[0] if rows else None
    rows = db.query(
        "SELECT a.* FROM dbo.artifacts a WHERE a.kind=? AND a.attempt_id = COALESCE("
        "(SELECT carried_from_attempt_id FROM dbo.stage_states WHERE attempt_id=? AND stage=? AND status='carried'), ?)",
        kind, attempt_id, stage, attempt_id)
    return rows[0] if rows else None
