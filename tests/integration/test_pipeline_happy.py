from helpers import counts, pump, stage_rows, status_of, upload


def test_invoice_flows_to_completed(rt, testdocs):
    doc = upload(rt, testdocs / "invoice_northwind.pdf", "invoice_northwind.pdf")
    assert status_of(rt.db, doc) == "uploading"
    pump(rt)
    assert status_of(rt.db, doc) == "completed"
    assert [r["status"] for r in stage_rows(rt.db, doc)] == ["succeeded"] * 4
    d = rt.db.query("SELECT * FROM dbo.documents WHERE document_id=?", doc)[0]
    assert d["category"] == "invoice" and d["current_attempt_no"] == 1 and d["sha256"] and d["page_count"] == 1
    f = {r["field_name"]: r for r in rt.db.query("SELECT * FROM dbo.document_fields WHERE result_id=?", d["current_result_id"])}
    assert f["reference_number"]["value_text"] == "INV-2026-0042"
    assert str(f["total_amount"]["value_number"]) == "408.2400"
    assert f["due_date"]["status"] == "present" and str(f["due_date"]["value_date"]) == "2026-04-13"
    assert "payment_method" not in f  # not applicable to invoices
    assert f["amount_due"]["status"] == "present"
