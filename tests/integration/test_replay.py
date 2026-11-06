"""REQ-010 / REQ-013: replay creates a distinguishable new attempt and never rewrites earlier evidence."""
import threading
import uuid

import pyodbc
import pytest
from fastapi.testclient import TestClient

from docdesk.db import repo_pipeline as repo
from docdesk.db.connection import Database
from docdesk.web.app import create_app
from helpers import counts, invoice_pdf, pump, pump_until_quiet, stage_rows, status_of, tries, upload
from conftest import dev_settings

SNAPSHOT_SQL = {
    "attempts": "SELECT attempt_id, attempt_no, trigger_kind, status, completed_at FROM dbo.processing_attempts WHERE document_id=? AND attempt_no=1",
    "stages": "SELECT s.stage, s.status, s.tries, s.last_error_code, s.completed_at FROM dbo.stage_states s JOIN dbo.processing_attempts a ON a.attempt_id=s.attempt_id WHERE s.document_id=? AND a.attempt_no=1 ORDER BY s.stage",
    "tries": "SELECT t.stage, t.try_no, t.outcome, t.error_code, t.started_at, t.finished_at FROM dbo.stage_tries t JOIN dbo.processing_attempts a ON a.attempt_id=t.attempt_id WHERE t.document_id=? AND a.attempt_no=1 ORDER BY t.stage, t.try_no",
    "artifacts": "SELECT artifact_id, kind, blob_name, sha256 FROM dbo.artifacts WHERE document_id=? ORDER BY artifact_id",
}


def snapshot(db, doc):
    return {k: db.query(q, doc) for k, q in SNAPSHOT_SQL.items()}


def fail_analyze(rt, sim):
    sim.rules = [lambda text: "unavailable"]
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "failed"
    return doc


def test_replay_creates_attempt_two_and_leaves_attempt_one_untouched(rt, sim):
    doc = fail_analyze(rt, sim)
    before = snapshot(rt.db, doc)
    sim.rules = []                                                    # the provider recovers
    res = repo.replay_document(rt.db, doc, 1, None, "provider was down")
    assert res["outcome"] == "OK" and res["attempt_no"] == 2
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "completed"
    a = rt.db.query("SELECT attempt_no, trigger_kind, replay_from_stage, replay_reason, status, prior_attempt_id FROM dbo.processing_attempts WHERE document_id=? ORDER BY attempt_no", doc)
    assert [(x["attempt_no"], x["trigger_kind"], x["status"]) for x in a] == [(1, "initial", "failed"), (2, "replay", "succeeded")]
    assert a[1]["replay_from_stage"] == "analyze" and a[1]["replay_reason"] == "provider was down" and a[1]["prior_attempt_id"] is not None
    assert snapshot(rt.db, doc)["attempts"] == before["attempts"] and snapshot(rt.db, doc)["stages"] == before["stages"]
    assert snapshot(rt.db, doc)["tries"] == before["tries"]                                  # attempt 1's history is intact
    assert all(x in snapshot(rt.db, doc)["artifacts"] for x in before["artifacts"])           # nothing deleted
    st = {(r["attempt_no"], r["stage"]): r["status"] for r in stage_rows(rt.db, doc)}
    assert st[(2, "intake")] == "carried" and st[(2, "analyze")] == "succeeded"
    assert rt.db.scalar("SELECT current_attempt_no FROM dbo.documents WHERE document_id=?", doc) == 2
    assert counts(rt.db, doc)["document_results"] == 1


def test_replay_from_index_reuses_stored_provider_response_without_a_provider_call(rt, sim):
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    # simulate a result-stage failure (the normalized artifact was lost), making the document replayable
    norm = rt.db.query("SELECT container, blob_name FROM dbo.artifacts WHERE document_id=? AND kind='normalized_result'", doc)[0]
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.documents SET status='failed', last_error_code='ARTIFACT_MISSING' WHERE document_id=?", doc))
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET status='failed' WHERE document_id=? AND stage='result'", doc))
    rt.db.run(lambda cur: cur.execute("UPDATE dbo.processing_attempts SET status='failed' WHERE document_id=?", doc))
    calls = sim.analyze_calls
    res = repo.replay_document(rt.db, doc, 1, "index", "re-normalize")
    assert res["outcome"] == "OK"
    pump_until_quiet(rt)
    assert sim.analyze_calls == calls                                           # no new (billable) analysis
    st = {(r["attempt_no"], r["stage"]): r["status"] for r in stage_rows(rt.db, doc)}
    assert st[(2, "intake")] == "carried" and st[(2, "analyze")] == "carried" and st[(2, "index")] == "succeeded"
    assert status_of(rt.db, doc) == "completed"
    assert counts(rt.db, doc)["document_results"] == 2                          # one per attempt; the first is retained
    new = rt.db.query("SELECT d.current_result_id, r.attempt_id FROM dbo.documents d JOIN dbo.document_results r ON r.result_id=d.current_result_id WHERE d.document_id=?", doc)[0]
    assert str(new["attempt_id"]).lower() == str(rt.db.scalar("SELECT attempt_id FROM dbo.processing_attempts WHERE document_id=? AND attempt_no=2", doc)).lower()


def test_replay_cannot_start_after_the_failed_stage_and_only_failed_documents_replay(rt, sim):
    doc = fail_analyze(rt, sim)
    assert repo.replay_document(rt.db, doc, 1, "index", None)["outcome"] == "BAD_STAGE"
    assert status_of(rt.db, doc) == "failed" and counts(rt.db, doc)["processing_attempts"] == 1       # the rejected request changed nothing
    assert repo.replay_document(rt.db, doc, 99, None, None)["outcome"] == "CONFLICT"                   # stale view of the document
    ok = upload(rt, invoice_pdf("Z"), "z.pdf")
    sim.rules = []
    pump(rt)
    assert repo.replay_document(rt.db, ok, 1, None, None)["outcome"] == "CONFLICT"                     # completed documents are not replayable


def test_concurrent_replays_have_one_winner(rt, sim):
    doc = fail_analyze(rt, sim)
    barrier = threading.Barrier(6)
    out = []
    def go():
        barrier.wait()
        out.append(repo.replay_document(rt.db, doc, 1, None, None)["outcome"])
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["CONFLICT"] * 5 + ["OK"]
    assert counts(rt.db, doc)["processing_attempts"] == 2
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE document_id=? AND stage='analyze' AND dispatched_at IS NULL", doc) == 1


def test_replay_through_the_web_role_only_has_execute_not_table_writes(rt, sim, settings):
    """The documented web identity (a SQL user holding only docdesk_web_role) can replay but cannot write attempt rows directly."""
    doc = fail_analyze(rt, sim)
    login = f"web_{uuid.uuid4().hex[:8]}"
    pw = "Wb!" + uuid.uuid4().hex
    admin = rt.db.connect(autocommit=True)
    master = Database(settings).connect(database="master", autocommit=True)
    master.execute(f"CREATE LOGIN [{login}] WITH PASSWORD='{pw}', CHECK_POLICY=OFF")
    admin.execute(f"CREATE USER [{login}] FOR LOGIN [{login}]")
    admin.execute(f"ALTER ROLE docdesk_web_role ADD MEMBER [{login}]")
    try:
        web_db = Database(dev_settings(sql_database=settings.sql_database, sql_user=login, sql_password=pw))
        with pytest.raises(pyodbc.Error) as e:
            web_db.run(lambda cur: cur.execute("INSERT dbo.processing_attempts (attempt_id, document_id, attempt_no, trigger_kind) VALUES (NEWID(), ?, 9, 'replay')", doc))
        assert "229" in str(e.value) or "permission" in str(e.value).lower()
        with pytest.raises(pyodbc.Error):
            web_db.run(lambda cur: cur.execute("DELETE dbo.documents WHERE document_id=?", doc))
        with pytest.raises(pyodbc.Error):
            web_db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET status='succeeded' WHERE document_id=?", doc))
        assert web_db.scalar("SELECT COUNT(*) FROM dbo.documents") == 1                                        # it can read
        sim.rules = []
        res = repo.replay_document(web_db, doc, 1, None, "from the web role")                                  # EXEC via ownership chaining
        assert res["outcome"] == "OK"
        assert counts(rt.db, doc)["processing_attempts"] == 2
        # and it can run the same race: a second replay of the same attempt loses
        assert repo.replay_document(web_db, doc, 1, None, None)["outcome"] == "CONFLICT"
        # the documented upload-side writes still work for the web role
        web_db.run(lambda cur: cur.execute("UPDATE dbo.documents SET last_error_message=last_error_message WHERE document_id=?", doc))
    finally:
        admin.execute(f"ALTER ROLE docdesk_web_role DROP MEMBER [{login}]")
        admin.execute(f"DROP USER [{login}]")
        master.execute(f"DROP LOGIN [{login}]")
        admin.close()
        master.close()
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "completed"


def test_replay_endpoint_conflicts_and_success(rt, sim, settings):
    client = TestClient(create_app(settings, rt.db, rt.blob), follow_redirects=False)
    H = {"Authorization": "Bearer test-operator-password"}
    doc = fail_analyze(rt, sim)
    assert client.post(f"/api/documents/{doc}/replay", json={"from_stage": "bogus"}, headers=H).status_code == 422
    assert client.post(f"/api/documents/{doc}/replay", json={"from_stage": "index"}, headers=H).status_code == 422
    sim.rules = []
    r = client.post(f"/api/documents/{doc}/replay", json={"reason": "retry after outage"}, headers=H)
    assert r.status_code == 202 and r.json()["attempt_no"] == 2
    assert client.post(f"/api/documents/{doc}/replay", json={}, headers=H).status_code == 409       # no longer failed
    assert client.post(f"/api/documents/{uuid.uuid4()}/replay", json={}, headers=H).status_code == 404
    pump_until_quiet(rt)
    detail = client.get(f"/api/documents/{doc}", headers=H).json()
    assert detail["status"] == "completed" and [a["attempt_no"] for a in detail["attempts"]] == [1, 2]
    assert detail["attempts"][0]["status"] == "failed" and detail["attempts"][1]["trigger_kind"] == "replay"
    assert detail["attempts"][0]["stages"][1]["tries_detail"][0]["error_code"] == "PROVIDER_UNAVAILABLE"      # evidence still visible
