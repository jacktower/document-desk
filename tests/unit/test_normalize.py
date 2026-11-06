import json

import pytest

from docdesk.config import Settings
from docdesk.cu.normalize import normalize, parse_operation
from docdesk.domain.errors import StageError
from docdesk_devtools.sim_cu import simulate_result

S = Settings(app_env="dev")


def op(contents, status="Succeeded", **extra):
    return json.dumps({"id": "op-1", "status": status, "result": {"apiVersion": "2025-11-01", "analyzerId": "docdesk-classifier-v1",
                                                                  "contents": contents, **extra}}).encode()


def root(md="Some text", **kw):
    return {"mimeType": "application/pdf", "markdown": md, "startPageNumber": 1, "endPageNumber": 2, **kw}


def routed(category, fields, md="routed text"):
    return {"path": "input1/segment1", "category": category, "fields": fields, "markdown": md}  # K-006: no mimeType


def run(raw, **kw):
    return normalize(raw, document_id="d1", attempt_no=1, settings=S, **kw)


def test_routed_invoice_present_absent_and_provenance():
    fields = {
        "reference_number": {"type": "string", "valueString": " INV-1 ", "confidence": 0.93, "source": "D(1,1,1,2,2,3,3,4,4)"},
        "total_amount": {"type": "number", "valueNumber": 12.5},
        "due_date": {"type": "date", "valueDate": "2026-04-13"},
        "vendor_name": {"type": "string", "valueString": ""},                      # empty -> absent
        "line_items": {"type": "array", "valueArray": [{"type": "object", "valueObject": {
            "description": {"type": "string", "valueString": "Beans"}, "quantity": {"type": "number", "valueNumber": 2},
            "amount": {"type": "number", "valueNumber": 37}}}]},
    }
    n = run(op([root(), routed("invoice", fields)]))
    assert n.category == "invoice" and n.classification_status == "classified"
    f = {x.name: x for x in n.fields}
    assert f["reference_number"].value == "INV-1" and f["reference_number"].confidence == 0.93 and f["reference_number"].source
    assert f["total_amount"].value_number == 12.5 and f["total_amount"].confidence is None   # nothing invented
    assert f["due_date"].status == "present" and str(f["due_date"].value_date) == "2026-04-13"
    assert f["vendor_name"].status == "absent" and f["customer_name"].status == "absent"
    assert f["line_items"].value == [{"description": "Beans", "quantity": 2, "unit_price": None, "amount": 37}]
    assert n.document["classification"]["confidence"] is None                                # never invented
    assert n.text == "Some text" and n.page_count == 2 and not n.simulated
    assert "payment_method" not in f                                                        # not applicable to invoices
    assert n.document["fields"]["total_amount"]["confidence"] is None


def test_other_with_root_only_and_unknown_and_missing_category():
    n = run(op([root(category="other")]))
    assert (n.category, n.classification_status, n.fields, n.needs_review) == ("other", "other", [], True)
    n = run(op([root()]))
    assert (n.category, n.classification_status, n.needs_review) == (None, "not_classified", True)
    n = run(op([root(), routed("spaceship_manual", {})]))
    assert (n.category, n.classification_status, n.provider_category_raw) == ("other", "unrecognized_category", "spaceship_manual")
    assert any(f["code"] == "CATEGORY_UNRECOGNIZED" for f in n.flags)


def test_invalid_values_are_flagged_not_coerced():
    fields = {"total_amount": {"type": "string", "valueString": "twelve"}, "due_date": {"type": "date", "valueDate": "13/04/2026"},
              "reference_number": {"type": "string", "valueString": "X-1"}}
    n = run(op([root(), routed("invoice", fields)]))
    f = {x.name: x for x in n.fields}
    assert f["total_amount"].status == "invalid" and f["total_amount"].value_number is None and "twelve" in f["total_amount"].value_string
    assert f["due_date"].status == "invalid"
    assert {x["field"] for x in n.flags if x["code"] == "FIELD_INVALID"} == {"total_amount", "due_date"}
    assert n.needs_review


def test_key_fields_absent_flag_and_low_confidence_info():
    n = run(op([root(), routed("nda", {"governing_law": {"type": "string", "valueString": "Delaware", "confidence": 0.2}})]))
    codes = {f["code"] for f in n.flags}
    assert "KEY_FIELDS_ABSENT" in codes and "FIELD_LOW_CONFIDENCE" in codes and n.needs_review
    n = run(op([root(), routed("nda", {"parties": {"type": "array", "valueArray": [{"type": "string", "valueString": "A"}, {"type": "string", "valueString": "B"}]}})]))
    assert "KEY_FIELDS_ABSENT" not in {f["code"] for f in n.flags}
    assert {x.name: x for x in n.fields}["parties"].value == ["A", "B"]


def test_text_fallback_and_missing():
    n = run(op([{"mimeType": "application/pdf"}, routed("invoice", {}, md="from routed")]))
    assert n.text == "from routed"
    n = run(op([{"mimeType": "application/pdf"}, {"path": "input1/segment1", "category": "invoice", "fields": {}}]))
    assert n.text is None and any(f["code"] == "TEXT_MISSING" for f in n.flags) and n.document["text"]["status"] == "missing"


def test_long_values_kept_in_full():
    clause = "X" * 1300
    n = run(op([root(), routed("msa", {"termination_clause_text": {"type": "string", "valueString": clause}})]))
    f = {x.name: x for x in n.fields}["termination_clause_text"]
    assert f.value_text == clause and len(f.value_string) == 400 and f.value_string_truncated
    assert n.document["fields"]["termination_clause_text"]["value"] == clause


@pytest.mark.parametrize("raw,code", [
    (b"", "PROVIDER_RESPONSE_MALFORMED"), (b"<html>", "PROVIDER_RESPONSE_MALFORMED"), (b"[]", "PROVIDER_RESPONSE_MALFORMED"),
    (op([], status="Running"), "PROVIDER_RESPONSE_MALFORMED"),
    (op([]), "PROVIDER_RESULT_UNUSABLE"), (json.dumps({"status": "Succeeded", "result": {}}).encode(), "PROVIDER_RESULT_UNUSABLE"),
    (op(["x"]), "PROVIDER_RESULT_UNUSABLE"),
])
def test_unusable_responses(raw, code):
    with pytest.raises(StageError) as e:
        parse_operation(raw)
    assert e.value.code == code
    assert e.value.retryable == (code == "PROVIDER_RESPONSE_MALFORMED")


def test_warnings_and_extra_fields_and_simulated_marker():
    raw = json.dumps({"id": "o", "status": "Succeeded", "_simulated": True, "result": {"warnings": [{"code": "W1", "message": "m"}], "contents": [
        root(), routed("receipt", {"vendor_name": {"type": "string", "valueString": "Shop"}, "mystery": {"type": "string", "valueString": "?"}})]}}).encode()
    n = run(raw)
    codes = {f["code"] for f in n.flags}
    assert {"PROVIDER_WARNING", "PROVIDER_EXTRA_FIELDS"} <= codes and n.simulated and n.document["provider"]["simulated"]


def test_simulator_shape_round_trips_through_normalizer():
    res = simulate_result("INVOICE\nInvoice No: A-1\nTotal: USD 5.00\n", "application/pdf", "x")
    n = run(json.dumps({"id": "o", "status": "Succeeded", "result": res}).encode())
    f = {x.name: x for x in n.fields}
    assert n.category == "invoice" and f["reference_number"].value == "A-1" and f["total_amount"].value_number == 5
