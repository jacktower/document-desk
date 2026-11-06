"""REQ-007 / REQ-010 / REQ-011: bounded retries, exhausted failures, lease expiry, lost events, upload recovery."""
import io
import json

import pytest

from docdesk.db import repo_pipeline as repo
from docdesk.domain.stages import STAGE_QUEUES
from docdesk.pipeline import reaper
from docdesk.web.uploads import UploadFailed, save_upload
from helpers import (counts, emit_blob_event, fast_forward, invoice_pdf, outbox_rows, pump, pump_until_quiet, stage_rows, status_of, tries, upload)


def stage(db, doc, name, attempt=1):
    return [r for r in stage_rows(db, doc) if r["stage"] == name and r["attempt_no"] == attempt][0]


def test_transient_provider_errors_retry_then_succeed(rt, sim):
    sim.script = ["throttle", "unavailable", "empty"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "completed"
    t = tries(rt.db, doc, "analyze")
    assert [x["outcome"] for x in t] == ["retryable_error", "retryable_error", "retryable_error", "succeeded"]
    assert [x["error_code"] for x in t[:3]] == ["PROVIDER_THROTTLED", "PROVIDER_UNAVAILABLE", "PROVIDER_RESPONSE_MALFORMED"]
    assert stage(rt.db, doc, "analyze")["tries"] == 4
    assert all(x["provider_request_id"] for x in rt.db.query("SELECT provider_request_id FROM dbo.stage_tries WHERE stage='analyze'"))


def test_exhausted_retries_fail_the_document_with_full_history(rt, sim):
    sim.rules = [lambda text: "unavailable"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    d = rt.db.query("SELECT status, last_error_code FROM dbo.documents WHERE document_id=?", doc)[0]
    assert (d["status"], d["last_error_code"]) == ("failed", "PROVIDER_UNAVAILABLE")
    assert stage(rt.db, doc, "analyze")["status"] == "failed" and stage(rt.db, doc, "analyze")["tries"] == 5       # bounded: MAX_TRIES_ANALYZE
    assert [t["outcome"] for t in tries(rt.db, doc, "analyze")] == ["retryable_error"] * 4 + ["terminal_error"]
    assert rt.db.scalar("SELECT status FROM dbo.processing_attempts WHERE document_id=?", doc) == "failed"
    assert sim.analyze_calls == 5
    pump_until_quiet(rt)
    assert sim.analyze_calls == 5 and counts(rt.db, doc)["processing_attempts"] == 1        # nothing keeps retrying after exhaustion
    assert not [r for r in outbox_rows(rt.db, doc) if r["dispatched_at"] is None]


@pytest.mark.parametrize("behaviour,code", [("rejected", "PROVIDER_REQUEST_REJECTED"), ("forbidden", "PROVIDER_CONFIGURATION"),
                                            ("failed_content", "PROVIDER_OPERATION_FAILED")])
def test_terminal_provider_errors_do_not_retry(rt, sim, behaviour, code):
    sim.script = [behaviour]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "failed" and sim.analyze_calls == 1
    assert rt.db.scalar("SELECT last_error_code FROM dbo.documents WHERE document_id=?", doc) == code


def test_unusable_provider_result_fails_index_without_new_provider_call(rt, sim):
    sim.script = ["no_contents"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    assert stage(rt.db, doc, "analyze")["status"] == "succeeded" and stage(rt.db, doc, "index")["status"] == "failed"
    assert rt.db.scalar("SELECT last_error_code FROM dbo.documents WHERE document_id=?", doc) == "PROVIDER_RESULT_UNUSABLE"
    assert sim.analyze_calls == 1 and counts(rt.db, doc)["artifacts"] == 2           # original + raw response are retained as evidence


def test_a_bug_in_a_handler_is_recorded_and_bounded_not_stranded(rt, monkeypatch):
    from docdesk.pipeline import handlers
    from docdesk.pipeline import runner
    def boom(rt_, claim):
        raise ZeroDivisionError("SECRET detail")
    monkeypatch.setitem(runner.HANDLERS, "intake", boom)
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    d = rt.db.query("SELECT status, last_error_code, last_error_message FROM dbo.documents WHERE document_id=?", doc)[0]
    assert d["status"] == "failed" and d["last_error_code"] == "INTERNAL_ERROR" and "SECRET" not in d["last_error_message"]
    assert stage(rt.db, doc, "intake")["tries"] == 3


def test_worker_crash_lease_expiry_reaper_rerun(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt, only={"blob-events"})
    pump(rt, only={"blob-events", STAGE_QUEUES["intake"]})
    attempt = str(rt.db.scalar("SELECT attempt_id FROM dbo.processing_attempts WHERE document_id=?", doc)).lower()
    claim = repo.claim_stage(rt.db, rt.settings, attempt, "analyze", "crashed-worker")      # ...and the process dies here
    assert claim and stage(rt.db, doc, "analyze")["status"] == "running"
    assert reaper.reap_once(rt)["leases"] == 0                                              # lease still live: leave it alone
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET lease_expires_at=DATEADD(SECOND,-5,SYSUTCDATETIME()) WHERE attempt_id=? AND stage='analyze'", attempt))
    assert reaper.reap_once(rt)["leases"] == 1
    assert stage(rt.db, doc, "analyze")["status"] == "pending"
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    assert [t["outcome"] for t in tries(rt.db, doc, "analyze")] == ["lease_expired", "succeeded"]
    assert counts(rt.db, doc)["document_results"] == 1


def test_crash_loop_exhausts_and_fails_document(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt, only={"blob-events", STAGE_QUEUES["intake"]})
    attempt = str(rt.db.scalar("SELECT attempt_id FROM dbo.processing_attempts WHERE document_id=?", doc)).lower()
    for i in range(5):
        assert repo.claim_stage(rt.db, rt.settings, attempt, "analyze", f"crash{i}")
        rt.db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET lease_expires_at=DATEADD(SECOND,-5,SYSUTCDATETIME()) WHERE attempt_id=? AND stage='analyze'", attempt))
        reaper.reap_once(rt)
    d = rt.db.query("SELECT status, last_error_code FROM dbo.documents WHERE document_id=?", doc)[0]
    assert (d["status"], d["last_error_code"]) == ("failed", "CRASH_LOOP_EXHAUSTED")
    assert stage(rt.db, doc, "analyze")["status"] == "failed"


def test_reaper_recovers_a_stage_whose_message_was_lost(rt, settings):
    object.__setattr__(rt.settings, "stage_recovery_s", 1)
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt, only={"blob-events", STAGE_QUEUES["intake"]})
    pump(rt, max_rounds=1, only=set())                         # dispatch analyze's outbox row to the broker...
    rt.broker._q[STAGE_QUEUES["analyze"]].clear()              # ...and lose the message (broker data loss)
    assert status_of(rt.db, doc) == "processing"
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET updated_at=DATEADD(SECOND,-60,SYSUTCDATETIME()) WHERE status='pending'"))
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.outbox SET created_at=DATEADD(SECOND,-60,SYSUTCDATETIME())"))
    assert reaper.reap_once(rt)["lost_events"] == 1
    assert reaper.reap_once(rt)["lost_events"] == 0           # the fresh outbox row suppresses another one
    pump(rt)
    assert status_of(rt.db, doc) == "completed"


# ---- uploads that never produced an arrival event ---------------------------------------------------------
def test_lost_event_is_recovered_from_the_blob(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf", emit_event=False)
    pump(rt)
    assert status_of(rt.db, doc) == "uploading"
    assert reaper.reap_uploads(rt) == 0                                    # inside the grace period
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.documents SET created_at=DATEADD(SECOND,-3600,SYSUTCDATETIME())"))
    assert reaper.reap_uploads(rt) == 1
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    emit_blob_event(rt, doc)                                               # and the real event finally shows up: no effect
    pump(rt)
    assert counts(rt.db, doc)["processing_attempts"] == 1


def test_upload_whose_blob_never_arrived_becomes_upload_failed_and_a_late_blob_is_still_accepted(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf", emit_event=False)
    name = rt.db.scalar("SELECT blob_name FROM dbo.documents WHERE document_id=?", doc)
    rt.blob.delete("originals", name)
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.documents SET created_at=DATEADD(SECOND,-100,SYSUTCDATETIME())"))
    assert reaper.reap_uploads(rt) == 0 and status_of(rt.db, doc) == "uploading"      # past the grace period, not yet the timeout
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.documents SET created_at=DATEADD(SECOND,-7200,SYSUTCDATETIME())"))
    assert reaper.reap_uploads(rt) == 1 and status_of(rt.db, doc) == "upload_failed"
    rt.blob.put("originals", name, invoice_pdf(), "application/pdf")                  # the blob lands after all
    emit_blob_event(rt, doc)
    pump(rt)
    assert status_of(rt.db, doc) == "completed"


def test_blob_write_with_lost_acknowledgement_is_not_treated_as_failure(rt):
    rt.blob.lose_ack_puts = 1
    data = invoice_pdf()
    res = save_upload(rt.db, rt.blob, rt.settings, "a.pdf", io.BytesIO(data), len(data))
    assert res["status"] == "uploading" and status_of(rt.db, res["document_id"]) == "uploading"
    emit_blob_event(rt, res["document_id"])
    pump(rt)
    assert status_of(rt.db, res["document_id"]) == "completed"


def test_blob_write_failure_marks_upload_failed_and_reports(rt):
    rt.blob.fail_puts = 1
    data = invoice_pdf()
    with pytest.raises(UploadFailed) as e:
        save_upload(rt.db, rt.blob, rt.settings, "a.pdf", io.BytesIO(data), len(data))
    assert e.value.outcome == "failed" and status_of(rt.db, e.value.document_id) == "upload_failed"


def test_blob_write_with_unknown_outcome_leaves_uploading_for_the_reaper(rt):
    rt.blob.fail_puts = 1
    orig_head = rt.blob.head
    def head_fails(c, n):
        from docdesk.adapters.blobstore import BlobUnavailable
        raise BlobUnavailable("down")
    rt.blob.head = head_fails
    data = invoice_pdf()
    with pytest.raises(UploadFailed) as e:
        save_upload(rt.db, rt.blob, rt.settings, "a.pdf", io.BytesIO(data), len(data))
    assert e.value.outcome == "unknown" and status_of(rt.db, e.value.document_id) == "uploading"


def test_missing_original_at_intake_retries_then_fails(rt):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    rt.blob.delete("originals", rt.db.scalar("SELECT blob_name FROM dbo.documents WHERE document_id=?", doc))
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "failed" and stage(rt.db, doc, "intake")["tries"] == 3
    assert rt.db.scalar("SELECT last_error_code FROM dbo.documents WHERE document_id=?", doc) == "STORAGE_MISSING"


def provider_error_artifacts(db, doc):
    return db.query("SELECT a.*, t.outcome FROM dbo.artifacts a JOIN dbo.stage_tries t ON t.attempt_id=a.attempt_id AND t.stage=a.stage AND t.try_no=a.try_no "
                    "WHERE a.document_id=? AND a.kind='provider_error' ORDER BY a.try_no", doc)


def test_final_failed_provider_response_is_retained_byte_for_byte(rt, sim):
    """Regression: the unmodified final operation response of a failed analysis is an addressable artifact."""
    sim.script = ["failed_content"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "failed"
    (art,) = provider_error_artifacts(rt.db, doc)
    stored = rt.blob.get(art["container"], art["blob_name"])
    op_id = json.loads(stored)["id"]
    expected = json.dumps({"id": op_id, "status": "Failed", "error": {"code": "InvalidRequest", "innererror": {"code": "InvalidContent"}}}).encode()
    assert stored == expected and art["outcome"] == "terminal_error" and art["size_bytes"] == len(expected)
    from docdesk.adapters.blobstore import sha256_hex
    assert art["sha256"].lower() == sha256_hex(expected) and json.loads(art["provenance_json"])["config_version"] == "1"
    assert not [r for r in stage_rows(rt.db, doc) if r["stage"] == "index"]               # a failed analysis never advances to indexing


def test_unreadable_final_response_and_retried_failures_keep_their_evidence_and_later_success_is_used(rt, sim):
    sim.script = ["malformed", "failed_transient"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "completed"
    arts = provider_error_artifacts(rt.db, doc)
    assert [a["outcome"] for a in arts] == ["retryable_error", "retryable_error"]
    assert rt.blob.get(arts[0]["container"], arts[0]["blob_name"]) == b"<html>not json</html>"
    assert json.loads(rt.blob.get(arts[1]["container"], arts[1]["blob_name"]))["status"] == "Failed"
    assert rt.db.scalar("SELECT category FROM dbo.documents WHERE document_id=?", doc) == "invoice"      # indexed from the successful response
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.artifacts WHERE document_id=? AND kind='provider_response'", doc) == 1


def test_failed_response_survives_a_blob_failure_at_the_moment_of_failure(rt, sim):
    """Regression: bytes already received from the provider are never lost because storage was down when the attempt failed."""
    from docdesk.domain.stages import BLOB_EVENTS_QUEUE, STAGE_QUEUES
    sim.script = ["failed_content"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt, only={BLOB_EVENTS_QUEUE, STAGE_QUEUES["intake"]})
    rt.blob.fail_puts = 1                                   # the write of the failure evidence fails; reads still work
    pump(rt)
    assert status_of(rt.db, doc) == "failed" and sim.analyze_calls == 1
    assert provider_error_artifacts(rt.db, doc) == []        # no artifact row yet...
    (pend,) = rt.db.query("SELECT * FROM dbo.pending_artifacts WHERE document_id=?", doc)
    held = bytes(pend["content"])                            # ...but the exact bytes are durably held in SQL with the failure
    assert json.loads(held)["status"] == "Failed" and pend["persisted_at"] is None
    from docdesk.adapters.blobstore import sha256_hex
    assert sha256_hex(held) == pend["sha256"].lower()

    rt.blob.down = True                                      # storage still down: the sweep must keep the bytes
    assert reaper.reap_once(rt)["pending_artifacts"] == 0
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.pending_artifacts WHERE persisted_at IS NULL") == 1
    rt.blob.down = False                                     # storage recovers
    assert reaper.reap_once(rt)["pending_artifacts"] == 1 and reaper.reap_once(rt)["pending_artifacts"] == 0   # once only
    (art,) = provider_error_artifacts(rt.db, doc)
    assert rt.blob.get(art["container"], art["blob_name"]) == held and art["outcome"] == "terminal_error"
    assert json.loads(art["provenance_json"])["config_version"] == "1"
    done = rt.db.query("SELECT content, artifact_id FROM dbo.pending_artifacts")[0]
    assert done["content"] is None and str(done["artifact_id"]).lower() == str(art["artifact_id"]).lower()
    assert sim.analyze_calls == 1                            # recovered without another (billable) analysis


def test_failed_response_survives_an_sdk_http_503_on_the_evidence_write(rt, sim):
    """Regression (F-7, SDK path): the real AzureBlobStore raises an HTTP error, not BlobUnavailable, when Blob answers 503.
    The received bytes must still be kept and later persisted, and the provider failure must stay the recorded failure."""
    from azure.storage.blob import BlobServiceClient
    from docdesk.adapters.blobstore import AzureBlobStore
    from docdesk.domain.stages import BLOB_EVENTS_QUEUE, STAGE_QUEUES
    from test_blob_http_errors import serve

    srv = serve(503)
    broken = AzureBlobStore(rt.settings, service=BlobServiceClient(account_url=f"http://127.0.0.1:{srv.server_port}", credential=None, retry_total=0))
    fs = rt.blob

    class Split:
        """Filesystem blobs, except that writes to the artifacts container go to the (503) SDK adapter while `outage` is set."""
        outage = False

        def __getattr__(self, name):
            return getattr(fs, name)

        def put(self, container, name, data, content_type, overwrite=False):
            if self.outage and container == rt.settings.artifacts_container:
                return broken.put(container, name, data, content_type, overwrite)
            return fs.put(container, name, data, content_type, overwrite)

    split = Split()
    rt.blob = split
    try:
        sim.script = ["failed_content"]
        doc = upload(rt, invoice_pdf(), "a.pdf")
        pump(rt, only={BLOB_EVENTS_QUEUE, STAGE_QUEUES["intake"]})
        split.outage = True
        pump(rt)
        d = rt.db.query("SELECT status, last_error_code FROM dbo.documents WHERE document_id=?", doc)[0]
        assert (d["status"], d["last_error_code"]) == ("failed", "PROVIDER_OPERATION_FAILED") and sim.analyze_calls == 1   # not INTERNAL_ERROR, no re-analysis
        (pend,) = rt.db.query("SELECT * FROM dbo.pending_artifacts WHERE document_id=?", doc)
        held = bytes(pend["content"])
        assert json.loads(held)["status"] == "Failed"
        assert reaper.reap_once(rt)["pending_artifacts"] == 0                       # still 503: bytes stay in SQL
        split.outage = False
        assert reaper.reap_once(rt)["pending_artifacts"] == 1
        (art,) = provider_error_artifacts(rt.db, doc)
        assert fs.get(art["container"], art["blob_name"]) == held and sim.analyze_calls == 1
    finally:
        srv.shutdown()
