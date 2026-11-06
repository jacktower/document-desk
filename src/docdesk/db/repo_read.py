"""Read-side queries for the web/API. Everything shown to the operator is read from SQL (and artifacts SQL points at)."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from .connection import Database
from ..domain.stages import STAGES

DOC_COLUMNS = ("document_id, original_filename, content_type, size_bytes, page_count, status, current_attempt_no, category, needs_review, "
               "last_error_code, last_error_message, created_at, updated_at")


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec="milliseconds") + "Z"  # all stored timestamps are UTC
    return v.isoformat() if isinstance(v, date) else v


def _doc_out(r: dict) -> dict:
    out = {k: (str(v).lower() if k == "document_id" else _iso(v)) for k, v in r.items()}
    out["needs_review"] = bool(out.get("needs_review"))
    return out


def list_documents(db: Database, *, status: str | None = None, category: str | None = None, q: str | None = None,
                   limit: int = 50, cursor: str | None = None) -> dict:
    """Newest first. The cursor is `<created_at>|<document_id>` of the last row returned, matching the sort key
    (created_at DESC, document_id ASC) exactly, so rows that share a timestamp are neither skipped nor repeated."""
    limit = max(1, min(limit, 200))
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if category:
        where.append("category=?")
        params.append(category)
    if q:
        where.append("original_filename LIKE ? ESCAPE '\\'")
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if cursor:
        ts, doc_id = parse_cursor(cursor)
        where.append("(created_at < CAST(? AS DATETIME2(3)) OR (created_at = CAST(? AS DATETIME2(3)) AND document_id > CAST(? AS UNIQUEIDENTIFIER)))")
        params.extend([ts, ts, doc_id])
    sql = f"SELECT TOP ({limit + 1}) {DOC_COLUMNS} FROM dbo.documents" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC, document_id"
    rows = db.query(sql, *params)
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_doc_out(r) for r in rows]
    nxt = f"{items[-1]['created_at']}|{items[-1]['document_id']}" if more and items else None
    return {"items": items, "next_cursor": nxt}


class BadCursor(ValueError):
    pass


def parse_cursor(cursor: str) -> tuple[str, str]:
    try:
        ts, doc_id = cursor.split("|", 1)
        ts = ts.rstrip("Z")
        datetime.fromisoformat(ts)
        return ts, str(uuid.UUID(doc_id))
    except ValueError:
        raise BadCursor("malformed cursor") from None


def get_document(db: Database, document_id: str) -> dict | None:
    rows = db.query(f"SELECT {DOC_COLUMNS}, sha256, blob_container, blob_name, retain_until, current_result_id, source FROM dbo.documents WHERE document_id=?", document_id)
    if not rows:
        return None
    r = rows[0]
    doc = _doc_out(r)
    doc["sha256"] = r["sha256"]
    doc["retain_until"] = r["retain_until"].isoformat() if r["retain_until"] else None
    doc["source"] = r["source"]
    for k in ("blob_container", "blob_name", "current_result_id"):
        doc.pop(k, None)
    current_result_id = str(r["current_result_id"]).lower() if r["current_result_id"] else None

    attempts = db.query("SELECT attempt_id, attempt_no, trigger_kind, replay_from_stage, replay_reason, prior_attempt_id, status, created_at, completed_at "
                        "FROM dbo.processing_attempts WHERE document_id=? ORDER BY attempt_no", document_id)
    stages = db.query("SELECT attempt_id, stage, status, tries, max_tries, next_run_at, first_started_at, last_started_at, completed_at, "
                      "carried_from_attempt_id, last_error_code, last_error_message FROM dbo.stage_states WHERE document_id=?", document_id)
    tries = db.query("SELECT attempt_id, stage, try_no, worker_id, started_at, finished_at, outcome, error_code, error_message, http_status, "
                     "provider_request_id FROM dbo.stage_tries WHERE document_id=? ORDER BY started_at, try_id", document_id)
    by_attempt = {}
    for a in attempts:
        aid = str(a["attempt_id"]).lower()
        ao = {k: (str(v).lower() if k.endswith("_id") and v else _iso(v)) for k, v in a.items()}
        ao["stages"] = []
        for st in STAGES:
            sr = next((s for s in stages if str(s["attempt_id"]).lower() == aid and s["stage"] == st), None)
            if sr is None:
                continue
            so = {k: (str(v).lower() if k.endswith("_id") and v else _iso(v)) for k, v in sr.items() if k != "attempt_id"}
            so["tries_detail"] = [{k: _iso(v) for k, v in t.items() if k not in ("attempt_id", "stage")}
                                  for t in tries if str(t["attempt_id"]).lower() == aid and t["stage"] == st]
            ao["stages"].append(so)
        by_attempt[aid] = ao
    doc["attempts"] = list(by_attempt.values())

    doc["result"] = None
    if current_result_id:
        res = db.query("SELECT * FROM dbo.document_results WHERE result_id=?", current_result_id)[0]
        fields = db.query("SELECT field_name, value_type, status, value_text, value_string, value_string_truncated, value_number, value_date, "
                          "value_bool, value_json, confidence, source FROM dbo.document_fields WHERE result_id=?", current_result_id)
        doc["result"] = {
            "result_id": current_result_id, "schema_version": res["result_schema_version"], "category": res["category"],
            "classification_status": res["classification_status"], "category_provider_raw": res["category_provider_raw"],
            "provider": {"name": res["provider_name"], "api_version": res["api_version"], "classifier_analyzer_id": res["analyzer_id"],
                         "extractor_analyzer_id": res["extractor_analyzer_id"], "config_version": res["analyzer_config_version"],
                         "config_hash": res["analyzer_config_hash"], "operation_id": res["provider_operation_id"],
                         "simulated": bool(res["simulated"])},
            "page_count": res["page_count"], "text_char_count": res["text_char_count"], "needs_review": bool(res["needs_review"]),
            "flags": json.loads(res["flags_json"] or "[]"), "attempt_id": str(res["attempt_id"]).lower(),
            "fields": [_field_out(f) for f in fields],
        }
    arts = db.query("SELECT artifact_id, attempt_id, stage, try_no, kind, size_bytes, content_type, sha256, created_at FROM dbo.artifacts "
                    "WHERE document_id=? ORDER BY created_at", document_id)
    doc["artifacts"] = [{k: (str(v).lower() if k.endswith("_id") and v else _iso(v)) for k, v in a.items()} for a in arts]
    return doc


def _field_out(f: dict) -> dict:
    value = None
    if f["status"] == "present":
        if f["value_json"] is not None:
            value = json.loads(f["value_json"])
        elif f["value_number"] is not None:
            value = float(f["value_number"]) if f["value_type"] != "integer" else int(f["value_number"])
        elif f["value_date"] is not None:
            value = f["value_date"].isoformat()
        elif f["value_bool"] is not None:
            value = bool(f["value_bool"])
        else:
            value = f["value_text"]
    return {"name": f["field_name"], "type": f["value_type"], "status": f["status"], "value": value,
            "raw_value": f["value_string"] if f["status"] == "invalid" else None,
            "confidence": f["confidence"], "source": f["source"]}


def get_artifact(db: Database, document_id: str, artifact_id: str) -> dict | None:
    rows = db.query("SELECT * FROM dbo.artifacts WHERE document_id=? AND artifact_id=?", document_id, artifact_id)
    return rows[0] if rows else None


def get_original_ref(db: Database, document_id: str) -> dict | None:
    rows = db.query("SELECT original_filename, content_type, blob_container, blob_name, status FROM dbo.documents WHERE document_id=?", document_id)
    return rows[0] if rows else None


def diagnostics(db: Database) -> dict:
    out: dict = {}
    out["documents_by_status"] = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM dbo.documents GROUP BY status")}
    out["failed_stages_24h"] = [{"error_code": r["last_error_code"], "count": r["n"]} for r in db.query(
        "SELECT last_error_code, COUNT(*) n FROM dbo.stage_states WHERE status='failed' AND updated_at > DATEADD(HOUR,-24,SYSUTCDATETIME()) "
        "GROUP BY last_error_code ORDER BY n DESC")]
    out["oldest_undispatched_outbox_s"] = db.scalar(
        "SELECT DATEDIFF(SECOND, MIN(created_at), SYSUTCDATETIME()) FROM dbo.outbox WHERE dispatched_at IS NULL")
    out["oldest_runnable_stage_s"] = db.scalar(
        "SELECT DATEDIFF(SECOND, MIN(COALESCE(next_run_at, updated_at)), SYSUTCDATETIME()) FROM dbo.stage_states "
        "WHERE status IN ('pending','retry_wait') AND COALESCE(next_run_at, updated_at) < SYSUTCDATETIME()")
    out["expired_leases"] = db.scalar("SELECT COUNT(*) FROM dbo.stage_states WHERE status='running' AND lease_expires_at < SYSUTCDATETIME()")
    out["workers"] = [{"worker_id": r["worker_id"], "role": r["role"], "version": r["version"], "started_at": _iso(r["started_at"]),
                       "last_seen_age_s": r["age"]} for r in db.query(
        "SELECT TOP (20) worker_id, role, version, started_at, DATEDIFF(SECOND, last_seen_at, SYSUTCDATETIME()) AS age FROM dbo.worker_heartbeats "
        "ORDER BY last_seen_at DESC")]
    out["worker_dependencies"] = [{"name": r["name"], "status": r["status"], "checked_age_s": r["age"], "error_class": r["error_class"], "detail": r["detail"]}
                                  for r in db.query("SELECT name, status, error_class, detail, DATEDIFF(SECOND, checked_at, SYSUTCDATETIME()) AS age "
                                                    "FROM dbo.dependency_health ORDER BY name")]
    return out
