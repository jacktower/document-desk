"""REQ-009 / REQ-011 / REQ-012: duplicate delivery, concurrent workers, stale leases, guarded transitions."""
import json
import threading
import uuid

import pytest

from docdesk.db import repo_pipeline as repo
from docdesk.domain.errors import LeaseLost
from docdesk.domain.stages import BLOB_EVENTS_QUEUE
from docdesk.pipeline import outbox
from docdesk.pipeline.handlers import HANDLERS
from docdesk.pipeline.runner import handle_stage_message
from docdesk_devtools.events import blob_created_event
from helpers import counts, emit_blob_event, invoice_pdf, outbox_rows, pump, stage_rows, status_of, tries, upload


def run_threads(n, fn):
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def work(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    return results


# ---- arrival ----------------------------------------------------------------------------------------
def test_duplicate_blob_events_create_one_attempt_and_one_outbox_row(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf", emit_event=False)
    for _ in range(4):
        emit_blob_event(rt, doc)
    rt.broker.duplicate_pending(BLOB_EVENTS_QUEUE)
    pump(rt)
    c = counts(rt.db, doc)
    assert c["processing_attempts"] == 1 and rt.db.scalar("SELECT COUNT(*) FROM dbo.documents WHERE document_id=?", doc) == 1
    assert sum(1 for r in outbox_rows(rt.db, doc) if r["stage"] == "intake") == 1


def test_concurrent_arrivals_elect_exactly_one_winner(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf", emit_event=False)
    row = rt.db.query("SELECT blob_container, blob_name FROM dbo.documents WHERE document_id=?", doc)[0]
    won = run_threads(8, lambda i: repo.register_arrival(rt.db, rt.settings, doc, row["blob_container"], row["blob_name"], f"etag{i}"))
    assert sum(won) == 1
    assert counts(rt.db, doc)["processing_attempts"] == 1 and sum(1 for r in outbox_rows(rt.db, doc) if r["stage"] == "intake") == 1


def test_arrival_for_unknown_document_creates_the_record(rt):
    """The event may beat (or replace) the upload's own SQL row: arrival creates the durable record itself."""
    did = str(uuid.uuid4())
    rt.blob.put("originals", f"{did}/source.pdf", invoice_pdf(), "application/pdf")
    rt.broker.inject(BLOB_EVENTS_QUEUE, blob_created_event("originals", f"{did}/source.pdf", size=10))
    pump(rt)
    d = rt.db.query("SELECT status, source, category FROM dbo.documents WHERE document_id=?", did)[0]
    assert (d["status"], d["source"], d["category"]) == ("completed", "blob_event", "invoice")


@pytest.mark.parametrize("terminal", ["completed", "failed", "rejected"])
def test_late_or_duplicate_event_never_moves_a_settled_document(rt, testdocs, sim, terminal):
    if terminal == "completed":
        doc = upload(rt, invoice_pdf(), "a.pdf")
    elif terminal == "failed":
        sim.script = ["rejected"]
        doc = upload(rt, invoice_pdf(), "a.pdf")
    else:
        doc = upload(rt, testdocs / "corrupt_header_only.pdf", "a.pdf")
    pump(rt)
    assert status_of(rt.db, doc) == terminal
    before = (counts(rt.db, doc), rt.db.query("SELECT current_attempt_no, updated_at FROM dbo.documents WHERE document_id=?", doc))
    emit_blob_event(rt, doc)
    emit_blob_event(rt, doc)
    pump(rt)
    after = (counts(rt.db, doc), rt.db.query("SELECT current_attempt_no, updated_at FROM dbo.documents WHERE document_id=?", doc))
    assert status_of(rt.db, doc) == terminal and before == after


def test_upload_error_path_cannot_overwrite_a_document_that_arrival_advanced(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt, max_rounds=1)
    assert status_of(rt.db, doc) != "uploading"
    assert repo.mark_upload_failed(rt.db, doc, "UPLOAD_STORAGE_ERROR", "late error") is False
    assert status_of(rt.db, doc) != "upload_failed"


# ---- stage messages ---------------------------------------------------------------------------------
def prepare_stage(rt, stage="analyze"):
    """Drive a document through the stages before `stage` only; its message is published but not consumed."""
    from docdesk.domain.stages import STAGES, STAGE_QUEUES
    doc = upload(rt, invoice_pdf(), "a.pdf")
    allowed = {BLOB_EVENTS_QUEUE} | {STAGE_QUEUES[s] for s in STAGES[: STAGES.index(stage)]}
    pump(rt, only=allowed)
    outbox.dispatch_once(rt)
    attempt_id = str(rt.db.scalar("SELECT attempt_id FROM dbo.processing_attempts WHERE document_id=?", doc)).lower()
    assert rt.db.scalar("SELECT status FROM dbo.stage_states WHERE attempt_id=? AND stage=?", attempt_id, stage) == "pending"
    return doc, attempt_id


def test_same_stage_message_twice_gives_one_effect(rt):
    rt.broker.duplicate_sends = True
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    c = counts(rt.db, doc)
    assert c["document_results"] == 1 and c["processing_attempts"] == 1 and c["artifacts"] == 4
    assert [t["try_no"] for t in tries(rt.db, doc, "analyze")] == [1]          # the duplicate did not run a second analysis
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE document_id=? AND stage='index'", doc) == 1


def test_concurrent_claims_of_one_stage_have_exactly_one_winner(rt):
    doc, attempt = prepare_stage(rt, "analyze")
    claims = run_threads(8, lambda i: repo.claim_stage(rt.db, rt.settings, attempt, "analyze", f"w{i}"))
    assert sum(1 for c in claims if c is not None) == 1
    assert rt.db.scalar("SELECT tries FROM dbo.stage_states WHERE attempt_id=? AND stage='analyze'", attempt) == 1


def test_concurrent_delivery_of_the_same_message_to_many_workers(rt):
    """Eight workers receive the same analyze message at once: one runs it, the CU is called once, one result results."""
    doc, attempt = prepare_stage(rt, "analyze")
    msg = json.dumps({"v": 1, "document_id": doc, "attempt_id": attempt, "stage": "analyze"})
    from docdesk.adapters.broker import Received
    def deliver(i):
        return handle_stage_message(rt, Received("stage-analyze", f"m{i}", msg, 1, None))
    run_threads(8, deliver)
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    assert counts(rt.db, doc)["document_results"] == 1
    assert len(tries(rt.db, doc, "analyze")) == 1


def test_stale_lease_worker_cannot_complete(rt):
    doc, attempt = prepare_stage(rt, "analyze")
    a = repo.claim_stage(rt.db, rt.settings, attempt, "analyze", "worker-A")
    assert a and repo.claim_stage(rt.db, rt.settings, attempt, "analyze", "worker-B") is None   # live lease blocks others
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET lease_expires_at=DATEADD(SECOND,-5,SYSUTCDATETIME()) WHERE attempt_id=?", attempt))
    b = repo.claim_stage(rt.db, rt.settings, attempt, "analyze", "worker-B")
    assert b and b.try_no == 2
    effect_ran = []
    with pytest.raises(LeaseLost):
        repo.complete_stage(rt.db, rt.settings, a, lambda cur: effect_ran.append(1))     # A wakes up late
    assert not effect_ran
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE attempt_id=? AND stage='index'", attempt) == 0
    repo.complete_stage(rt.db, rt.settings, b, lambda cur: None)
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE attempt_id=? AND stage='index'", attempt) == 1
    outcomes = [t["outcome"] for t in tries(rt.db, doc, "analyze")]
    assert outcomes == ["lease_expired", "succeeded"]


def test_lost_lease_during_work_discards_the_try(rt):
    """The analyze handler's lease renewal fails once another worker owns the stage; nothing is stored."""
    doc, attempt = prepare_stage(rt, "analyze")
    claim = repo.claim_stage(rt.db, rt.settings, attempt, "analyze", "worker-A")
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET lease_owner='thief' WHERE attempt_id=?", attempt))
    with pytest.raises(LeaseLost):
        HANDLERS["analyze"](rt, claim)
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.artifacts WHERE attempt_id=?", attempt) == 0


# ---- outbox ------------------------------------------------------------------------------------------
def test_dispatcher_claims_disjoint_rows_on_an_rcsi_database(rt):
    assert rt.db.scalar("SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name=DB_NAME()") == 1
    for i in range(6):
        rt.db.run(lambda cur, i=i: repo.add_outbox(cur, "stage-intake", {"v": 1, "n": i}))
    sets = run_threads(3, lambda i: {str(r["outbox_id"]) for r in repo.claim_outbox(rt.db, f"d{i}", batch=2)})
    flat = [x for s in sets for x in s]
    assert len(flat) == len(set(flat)) == 6 and all(len(s) == 2 for s in sets)


def test_two_dispatchers_publish_each_row_once(rt):
    docs = [upload(rt, invoice_pdf(str(i)), f"{i}.pdf", emit_event=False) for i in range(5)]
    for d in docs:
        emit_blob_event(rt, d)
    # arrival only, then two dispatchers race
    from helpers import QUEUE_HANDLERS
    while (m := rt.broker.receive(BLOB_EVENTS_QUEUE, wait_s=0)) is not None:
        QUEUE_HANDLERS[0][1](rt, m)
        rt.broker.complete(m)
    run_threads(2, lambda i: [outbox.dispatch_once(rt) for _ in range(3)])
    intake_sends = [s for s in rt.broker.sent if s[0] == "stage-intake"]
    assert len(intake_sends) == 5 and len({s[1] for s in intake_sends}) == 5


def test_send_failure_after_commit_leaves_the_row_and_it_is_published_later(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    rt.broker.send_failures = 2
    pump(rt, max_rounds=2)
    assert status_of(rt.db, doc) == "processing" and rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE document_id=? AND dispatched_at IS NULL", doc) >= 1
    row = rt.db.query("SELECT dispatch_attempts, last_error FROM dbo.outbox WHERE document_id=? AND dispatched_at IS NULL", doc)[0]
    assert row["dispatch_attempts"] >= 1 and row["last_error"] == "BrokerDown"
    for _ in range(5):  # claims back off briefly; release them for the test rather than sleeping
        rt.db.run(lambda cur: cur.execute("UPDATE dbo.outbox SET claimed_until=NULL WHERE dispatched_at IS NULL"))
        pump(rt)
    assert status_of(rt.db, doc) == "completed" and counts(rt.db, doc)["document_results"] == 1


def test_broker_outage_then_recovery(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt)
    rt.broker.down = True
    doc2 = upload(rt, invoice_pdf("B"), "b.pdf", emit_event=False)
    emit_blob_event_direct = rt.broker.down
    rt.broker.down = False
    emit_blob_event(rt, doc2)                       # the (simulated) Event Grid delivery is not what is down
    rt.broker.down = True
    pump_failures = 0
    try:
        pump(rt, max_rounds=3)                      # receive() raises while the broker is down; nothing may be lost
    except Exception:
        pump_failures += 1
    assert status_of(rt.db, doc2) == "uploading"
    rt.broker.down = False
    pump(rt)
    assert status_of(rt.db, doc2) == "completed" and counts(rt.db, doc2)["document_results"] == 1


def test_crash_between_publish_and_mark_resends_and_state_is_unchanged(rt, monkeypatch):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    real = repo.mark_dispatched
    monkeypatch.setattr(repo, "mark_dispatched", lambda db, oid: (_ for _ in ()).throw(RuntimeError("process died after send, before mark")))
    with pytest.raises(RuntimeError):
        pump(rt)                                                   # message sent, row not marked: a dispatcher crash
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE document_id=? AND dispatched_at IS NULL", doc) == 1
    monkeypatch.setattr(repo, "mark_dispatched", real)
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.outbox SET claimed_until=DATEADD(SECOND,-1,SYSUTCDATETIME()) WHERE dispatched_at IS NULL"))  # claim expires
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    sent = [s[1] for s in rt.broker.sent if s[0] == "stage-intake"]
    assert len(sent) == 2 and len(set(sent)) == 1                  # the same outbox row was published twice (at-least-once)...
    c = counts(rt.db, doc)
    assert c["document_results"] == 1 and c["processing_attempts"] == 1 and len(tries(rt.db, doc, "intake")) == 1   # ...applied once
