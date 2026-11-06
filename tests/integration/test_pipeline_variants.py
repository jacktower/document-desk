import pytest

from helpers import counts, pump, pump_until_quiet, stage_rows, status_of, tries, upload


def test_two_different_types_get_their_own_categories_and_fields(rt, testdocs):
    inv = upload(rt, testdocs / "invoice_northwind.pdf", "invoice.pdf")
    nda = upload(rt, testdocs / "nda_fabrikam.pdf", "nda.pdf")
    pump(rt)
    cats = {r["document_id"].lower(): r["category"] for r in rt.db.query("SELECT document_id, category FROM dbo.documents")}
    assert cats == {inv: "invoice", nda: "nda"}
    nda_fields = {r["field_name"]: r for r in rt.db.query(
        "SELECT f.* FROM dbo.document_fields f JOIN dbo.documents d ON d.current_result_id=f.result_id WHERE d.document_id=?", nda)}
    assert nda_fields["governing_law"]["value_text"].startswith("State of Delaware")
    assert nda_fields["parties"]["status"] == "present" and "Fabrikam" in nda_fields["parties"]["value_json"]
    assert nda_fields["termination_clause_text"]["status"] == "absent"      # explicit "not found", not dropped, not invented
    assert "total_amount" not in nda_fields


def test_other_category_and_tiff_and_provenance_recorded(rt, testdocs):
    other = upload(rt, testdocs / "other_bake_sale_flyer.pdf", "flyer.pdf")
    tif = upload(rt, testdocs / "invoice_scan.tif", "scan.tif")
    pump(rt)
    d = rt.db.query("SELECT * FROM dbo.documents WHERE document_id=?", other)[0]
    assert d["status"] == "completed" and d["category"] == "other" and d["needs_review"]
    res = rt.db.query("SELECT * FROM dbo.document_results WHERE document_id=?", other)[0]
    assert res["classification_status"] == "other" and res["api_version"] == "2025-11-01" and res["analyzer_id"] == "docdesk-classifier-v1"
    assert res["extractor_analyzer_id"] is None and res["analyzer_config_hash"].startswith("sha256:") and res["analyzer_config_version"] == "1"
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.document_fields WHERE result_id=?", res["result_id"]) == 0
    # the TIFF reaches CU and completes (the simulator cannot read pixels, so it classifies as other; that is a simulator limit)
    assert status_of(rt.db, tif) == "completed"
    assert rt.db.scalar("SELECT content_type FROM dbo.documents WHERE document_id=?", tif) == "image/tiff"


def test_artifacts_are_addressable_and_raw_response_is_unmodified(rt, testdocs):
    doc = upload(rt, testdocs / "invoice_northwind.pdf", "invoice.pdf")
    pump(rt)
    arts = {a["kind"]: a for a in rt.db.query("SELECT * FROM dbo.artifacts WHERE document_id=?", doc)}
    assert set(arts) == {"original", "provider_response", "extracted_text", "normalized_result"}
    from docdesk.adapters.blobstore import sha256_hex
    for a in arts.values():
        data = rt.blob.get(a["container"], a["blob_name"])
        assert sha256_hex(data) == a["sha256"].lower() and len(data) == a["size_bytes"]
    import json
    raw = json.loads(rt.blob.get(arts["provider_response"]["container"], arts["provider_response"]["blob_name"]))
    assert raw["status"] == "Succeeded" and raw["_simulated"] is True and raw["result"]["contents"][1]["category"] == "invoice"
    norm = json.loads(rt.blob.get(arts["normalized_result"]["container"], arts["normalized_result"]["blob_name"]))
    assert norm["schema_version"] == "docdesk.result/1" and norm["provider"]["simulated"] is True and norm["classification"]["confidence"] is None


def test_unsupported_content_is_rejected_at_intake_and_not_replayable(rt, testdocs):
    doc = upload(rt, testdocs / "corrupt_header_only.pdf", "corrupt.pdf")     # passes the header check, fails the parse
    pump(rt)
    d = rt.db.query("SELECT status, last_error_code FROM dbo.documents WHERE document_id=?", doc)[0]
    assert (d["status"], d["last_error_code"]) == ("rejected", "INPUT_CORRUPT")
    assert tries(rt.db, doc, "intake")[0]["outcome"] == "terminal_error"
    from docdesk.db import repo_pipeline as repo
    assert repo.replay_document(rt.db, doc, 1, None, None)["outcome"] == "CONFLICT"


def test_too_many_pages_rejected(rt, testdocs, settings):
    object.__setattr__(rt.settings, "max_pages", 1)
    doc = upload(rt, testdocs / "multipage_invoice.pdf", "m.pdf")
    pump(rt)
    assert rt.db.scalar("SELECT last_error_code FROM dbo.documents WHERE document_id=?", doc) == "INPUT_TOO_MANY_PAGES"
    assert status_of(rt.db, doc) == "rejected"
