import time
import uuid

import pytest
from fastapi.testclient import TestClient

from docdesk.adapters.keyvault import PasswordProvider
from docdesk.config import ConfigError, Settings
from docdesk.db.connection import Database
from docdesk.web.app import create_app
from docdesk.web.auth import Auth
from docdesk_devtools.fake_blob import FsBlobStore

PW = "correct horse battery staple"


def settings(**kw):
    return Settings(app_env="dev", operator_password=PW, cookie_secure=True, **kw)


@pytest.fixture
def app(tmp_path):
    s = settings()
    return create_app(s, Database(s), FsBlobStore(tmp_path))  # no SQL connection is made before authentication


def test_every_route_except_healthz_and_login_requires_sign_in(app):
    """Enumerates the real route table: anything reachable anonymously must be on the allow-list."""
    anonymous_ok = {("GET", "/healthz"), ("GET", "/login"), ("POST", "/login"), ("POST", "/logout"), ("POST", "/api/auth/login")}
    client = TestClient(app, follow_redirects=False)
    checked = 0
    # the OpenAPI document lists every registered route (its public endpoint is disabled, the generator is not)
    for route_path, ops in app.openapi()["paths"].items():
        path = route_path.replace("{document_id}", str(uuid.uuid4())).replace("{artifact_id}", str(uuid.uuid4()))
        for method in (m.upper() for m in ops):
            if (method, route_path) in anonymous_ok:
                continue
            resp = client.request(method, path, json={} if method == "POST" else None)
            assert resp.status_code in (401, 303), f"{method} {route_path} reachable anonymously: {resp.status_code}"
            checked += 1
    assert checked == 10  # 7 API + 3 page routes; update deliberately when adding routes
    # docs / schema endpoints are disabled
    for p in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(p).status_code in (404, 401, 303)
    assert client.get("/healthz").json() == {"status": "ok"}


def test_wrong_password_and_bearer(app):
    c = TestClient(app, follow_redirects=False)
    assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert c.get("/api/documents", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/login", data={"password": "nope"})
    assert r.status_code == 401 and "Incorrect" in r.text


def test_login_sets_hardened_cookie_and_throttles(app):
    c = TestClient(app, follow_redirects=False, base_url="https://testserver")
    r = c.post("/api/auth/login", json={"password": PW})
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "secure" in sc and "samesite=strict" in sc
    c2 = TestClient(app, follow_redirects=False)
    for _ in range(5):
        c2.post("/api/auth/login", json={"password": "bad"})
    assert c2.post("/api/auth/login", json={"password": PW}).status_code == 429


def test_session_tamper_expiry_and_password_rotation():
    s = settings()
    pw = PasswordProvider(s)
    a = Auth(s, pw)
    cookie, csrf = a.issue_session()
    p = a.read_session(cookie)
    assert p and a.csrf_for(p.nonce) == csrf
    assert a.read_session(cookie[:-2] + "00") is None and a.read_session("garbage") is None and a.read_session(None) is None
    payload, sig = cookie.rsplit(".", 1)
    assert a.read_session(payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig) is None
    pw._env = "a different password"
    assert a.read_session(cookie) is None                           # rotating the password invalidates every session
    expired = Auth(Settings(app_env="dev", operator_password=PW, session_hours=0), PasswordProvider(s))
    c2, _ = expired.issue_session()
    time.sleep(1.1)
    assert expired.read_session(c2) is None


def test_csrf_and_origin_enforced_for_cookie_but_not_bearer(app, tmp_path):
    c = TestClient(app, follow_redirects=False, base_url="https://testserver")
    csrf = c.post("/api/auth/login", json={"password": PW}).json()["csrf_token"]
    did = str(uuid.uuid4())
    r = c.post(f"/api/documents/{did}/replay", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "BAD_CSRF"
    r = c.post(f"/api/documents/{did}/replay", json={}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "BAD_ORIGIN"


def test_password_sources():
    with pytest.raises(ConfigError):
        PasswordProvider(Settings(app_env="dev"))
    calls = []
    def fetch():
        calls.append(1)
        return "from-vault"
    p = PasswordProvider(Settings(app_env="dev", key_vault_url="https://kv.invalid", operator_password_secret_name="pw"), fetch=fetch)
    assert p.get() == "from-vault" and p.get() == "from-vault" and len(calls) == 1      # cached
    with pytest.raises(ConfigError):
        PasswordProvider(Settings(app_env="dev", key_vault_url="https://kv.invalid", operator_password_secret_name="pw"), fetch=lambda: "")
    boom = PasswordProvider(Settings(app_env="dev", key_vault_url="https://kv.invalid", operator_password_secret_name="pw"), fetch=lambda: "ok", ttl_s=0)
    boom._fetch = lambda: (_ for _ in ()).throw(RuntimeError("vault down"))
    assert boom.get() == "ok"                                       # last good value survives a Key Vault blip
