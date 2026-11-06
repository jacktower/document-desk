import io
import re

import pytest
from fastapi.testclient import TestClient

from docdesk.web.app import create_app
from helpers import pump

PW = "test-operator-password"


@pytest.fixture
def client(rt, settings):
    app = create_app(settings, rt.db, rt.blob)
    return TestClient(app, base_url="http://testserver", follow_redirects=False)


def test_smoke_pages(client, rt, testdocs):
    r = client.post("/api/auth/login", json={"password": PW})
    assert r.status_code == 200
    csrf = r.json()["csrf_token"]
    with open(testdocs / "invoice_northwind.pdf", "rb") as f:
        up = client.post("/api/documents", files={"file": ("invoice_northwind.pdf", f, "application/pdf")}, headers={"X-CSRF-Token": csrf})
    assert up.status_code == 202, up.text
    did = up.json()["document_id"]
    from helpers import emit_blob_event
    emit_blob_event(rt, did)
    pump(rt)
    for path in ("/", f"/documents/{did}", "/status"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    page = client.get(f"/documents/{did}").text
    assert "SIMULATED PROVIDER" in page and "INV-2026-0042" in page and "Invoice" in page
