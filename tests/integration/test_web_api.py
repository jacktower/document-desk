import io
import uuid

import pytest
from fastapi.testclient import TestClient

from docdesk.web.app import create_app
from helpers import emit_blob_event, invoice_pdf, pump, pump_until_quiet, upload

H = {"Authorization": "Bearer test-operator-password"}


@pytest.fixture
def client(rt, settings):
    return TestClient(create_app(settings, rt.db, rt.blob), follow_redirects=False)


def post_file(client, name, data, headers=H, ctype="application/octet-stream"):
    return client.post("/api/documents", files={"file": (name, io.BytesIO(data), ctype)}, headers=headers)


@pytest.mark.parametrize("name,data,status,code", [
    ("report.docx", b"PK\x03\x04", 415, "UNSUPPORTED_TYPE"), ("empty.pdf", b"", 400, "EMPTY_FILE"),
    ("fake.pdf", b"just text", 400, "CORRUPT_HEADER"), ("fake.tif", b"%PDF-1.4", 400, "CORRUPT_HEADER"),
])
def test_upload_rejections_are_precise_and_create_no_row(client, rt, name, data, status, code):
    r = post_file(client, name, data)
    assert r.status_code == status and r.json()["error"]["code"] == code and r.json()["error"]["message"]
    assert rt.db.scalar("SELECT COUNT(*) FROM dbo.documents") == 0


def test_oversize_rejected(client, rt, settings):
    object.__setattr__(settings, "max_upload_mb", 1)
    r = post_file(client, "big.pdf", b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10))
    assert r.status_code == 413 and r.json()["error"]["code"] == "TOO_LARGE" and rt.db.scalar("SELECT COUNT(*) FROM dbo.documents") == 0


def test_upload_storage_failure_is_reported_with_document_id(client, rt):
    rt.blob.fail_puts = 1
    r = post_file(client, "a.pdf", invoice_pdf())
    assert r.status_code == 502 and r.json()["error"]["code"] == "UPLOAD_FAILED"
    did = r.json()["error"]["document_id"]
    assert client.get(f"/api/documents/{did}", headers=H).json()["status"] == "upload_failed"


def test_list_detail_original_and_artifacts_are_served_from_sql_and_blob(client, rt):
    r = post_file(client, "Rechnung ü.pdf", invoice_pdf())
    assert r.status_code == 202
    did = r.json()["document_id"]
    assert client.get(f"/api/documents/{did}", headers=H).json()["status"] == "uploading"
    emit_blob_event(rt, did)
    pump(rt)
    lst = client.get("/api/documents?limit=5", headers=H).json()
    assert [d["document_id"] for d in lst["items"]] == [did] and lst["items"][0]["category"] == "invoice"
    assert client.get("/api/documents?status=failed", headers=H).json()["items"] == []
    assert len(client.get("/api/documents?q=Rechnung", headers=H).json()["items"]) == 1
    d = client.get(f"/api/documents/{did}", headers=H).json()
    assert d["status"] == "completed" and d["result"]["category"] == "invoice" and d["result"]["provider"]["simulated"] is True
    f = {x["name"]: x for x in d["result"]["fields"]}
    assert f["reference_number"]["value"] == "INV-A" and f["due_date"]["status"] == "absent" and f["due_date"]["value"] is None
    stages = d["attempts"][0]["stages"]
    assert [s["stage"] for s in stages] == ["intake", "analyze", "index", "result"] and all(s["status"] == "succeeded" for s in stages)
    o = client.get(f"/api/documents/{did}/original", headers=H)
    assert o.status_code == 200 and o.content[:5] == b"%PDF-" and o.headers["content-disposition"].startswith("inline")
    assert o.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/documents/{did}/original?download=1", headers=H).headers["content-disposition"].startswith("attachment")
    raw = next(a for a in d["artifacts"] if a["kind"] == "provider_response")
    rr = client.get(f"/api/documents/{did}/artifacts/{raw['artifact_id']}", headers=H)
    assert rr.status_code == 200 and rr.json()["status"] == "Succeeded"
    assert client.get(f"/api/documents/{did}/artifacts/{uuid.uuid4()}", headers=H).status_code == 404
    assert client.get("/api/documents/not-a-uuid", headers=H).status_code == 404
    assert client.get(f"/api/documents/{did}/original").status_code == 401                 # originals only for signed-in operators


def test_pagination_cursor(client, rt):
    for i in range(3):
        upload(rt, invoice_pdf(str(i)), f"{i}.pdf", emit_event=False)
    p1 = client.get("/api/documents?limit=2", headers=H).json()
    assert len(p1["items"]) == 2 and p1["next_before"]
    p2 = client.get(f"/api/documents?limit=2&before={p1['next_before']}", headers=H).json()
    assert len(p2["items"]) == 1 and p2["next_before"] is None


def test_diagnostics_show_failures_and_backlog(client, rt, sim):
    sim.rules = [lambda t: "unavailable"]
    upload(rt, invoice_pdf(), "a.pdf")
    pump_until_quiet(rt)
    d = client.get("/api/diagnostics", headers=H).json()
    assert d["web_checks"]["sql"]["status"] == "ok" and d["web_checks"]["blob"]["status"] == "ok"
    assert d["documents_by_status"] == {"failed": 1} and d["failed_stages_24h"] == [{"error_code": "PROVIDER_UNAVAILABLE", "count": 1}]
    assert "workers" in d and "worker_dependencies" in d


def test_ui_pages_render_for_signed_in_operator_and_html_is_escaped(client, rt):
    r = client.post("/login", data={"password": "test-operator-password"})
    assert r.status_code == 303
    evil = "<img src=x onerror=alert(1)>.pdf"
    did = post_file(client, evil, invoice_pdf(), headers={"X-CSRF-Token": client_csrf(client)}).json()["document_id"]
    emit_blob_event(rt, did)
    pump(rt)
    for path in ("/", f"/documents/{did}", "/status"):
        resp = client.get(path)
        assert resp.status_code == 200 and "<img src=x" not in resp.text, path
    assert "&lt;img src=x onerror=alert(1)&gt;.pdf" in client.get(f"/documents/{did}").text
    assert client.get("/documents/" + str(uuid.uuid4())).status_code == 404
    assert client.get("/nonexistent").status_code == 404


def client_csrf(client):
    import re
    m = re.search(r'name="csrf-token" content="([0-9a-f]+)"', client.get("/").text)
    return m.group(1)
