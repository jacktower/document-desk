import uuid

import pytest

from docdesk.db.connection import Database
from docdesk.db.migrate import migrate, split_batches
from conftest import dev_settings
from helpers import counts, invoice_pdf, make_pdf, pump, status_of, upload


def test_migrate_on_empty_database_and_rerun_is_noop(settings):
    name = f"docdesk_mig_{uuid.uuid4().hex[:8]}"
    master = Database(settings).connect(database="master", autocommit=True)
    master.execute(f"CREATE DATABASE [{name}]")
    try:
        d = Database(dev_settings(sql_database=name))
        assert migrate(d) == ["0001_init", "0002_artifact_provenance"]
        assert migrate(d) == []
        tables = {r["name"] for r in d.query("SELECT name FROM sys.tables")}
        assert {"documents", "processing_attempts", "stage_states", "stage_tries", "artifacts", "document_results", "document_fields",
                "outbox", "worker_heartbeats", "dependency_health", "schema_migrations"} <= tables
        roles = {r["name"] for r in d.query("SELECT name FROM sys.database_principals WHERE type='R'")}
        assert {"docdesk_web_role", "docdesk_worker_role"} <= roles
        assert d.scalar("SELECT COUNT(*) FROM sys.procedures WHERE name='usp_replay_document'") == 1
        # the worker role has no DELETE anywhere (history is never erased by the application identity)
        assert d.scalar("SELECT COUNT(*) FROM sys.database_permissions p JOIN sys.database_principals r ON r.principal_id=p.grantee_principal_id "
                        "WHERE r.name='docdesk_worker_role' AND p.permission_name='DELETE'") == 0
    finally:
        master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        master.execute(f"DROP DATABASE [{name}]")
        master.close()


def test_split_batches():
    assert split_batches("A;\nGO\nB;\n  go  \nC") == ["A;", "B;", "C"]


@pytest.mark.parametrize("length", [900, 1300, 5000])
def test_long_clause_values_persist_in_full_with_bounded_search_key(rt, sim, length):
    clause = ("Either party may terminate this agreement. " * 200)[:length]
    sim.field_overrides = {"termination_clause_text": {"type": "string", "valueString": clause}}
    doc = upload(rt, make_pdf(["MASTER SERVICE AGREEMENT", "Parties: A Ltd and B Ltd"]), "msa.pdf")
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    row = rt.db.query("SELECT f.* FROM dbo.document_fields f JOIN dbo.documents d ON d.current_result_id=f.result_id WHERE d.document_id=? AND f.field_name='termination_clause_text'", doc)[0]
    assert row["value_text"] == clause and len(row["value_string"]) == min(length, 400) and bool(row["value_string_truncated"]) == (length > 400)
    art = rt.db.query("SELECT container, blob_name FROM dbo.artifacts WHERE document_id=? AND kind='normalized_result'", doc)[0]
    import json
    assert json.loads(rt.blob.get(art["container"], art["blob_name"]))["fields"]["termination_clause_text"]["value"] == clause
    # the bounded search key is index-safe: an index seek on it works
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.document_fields WHERE field_name='termination_clause_text' AND value_string=?", clause[:400]) == 1


def test_invalid_provider_value_is_kept_as_invalid_not_dropped(rt, sim):
    sim.field_overrides = {"total_amount": {"type": "string", "valueString": "N/A"}}
    doc = upload(rt, invoice_pdf(), "a.pdf")
    pump(rt)
    row = rt.db.query("SELECT f.* FROM dbo.document_fields f JOIN dbo.documents d ON d.current_result_id=f.result_id WHERE d.document_id=? AND f.field_name='total_amount'", doc)[0]
    assert row["status"] == "invalid" and row["value_number"] is None and "N/A" in row["value_string"]
    assert rt.db.scalar("SELECT needs_review FROM dbo.documents WHERE document_id=?", doc) == 1
