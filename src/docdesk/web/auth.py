"""Single-operator authentication: a password in Key Vault, a signed stateless session cookie, or the password as a Bearer token."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock

from fastapi import HTTPException, Request

from ..adapters.keyvault import PasswordProvider
from ..config import Settings

COOKIE = "docdesk_session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class NotAuthenticated(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    kind: str  # "cookie" | "bearer"
    nonce: str = ""


class Auth:
    FAIL_LIMIT = 5
    FAIL_WINDOW_S = 60

    def __init__(self, settings: Settings, passwords: PasswordProvider):
        self.s = settings
        self.passwords = passwords
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    # -- keys ---------------------------------------------------------------------------------------
    def _key(self, purpose: bytes) -> bytes:
        prk = hmac.new(b"docdesk-v1", self.passwords.get().encode(), hashlib.sha256).digest()  # HKDF-extract
        return hmac.new(prk, purpose + b"\x01", hashlib.sha256).digest()                         # HKDF-expand (one block)

    # -- password check -------------------------------------------------------------------------------
    def check_password(self, candidate: str) -> bool:
        a = hashlib.sha256(candidate.encode()).digest()
        b = hashlib.sha256(self.passwords.get().encode()).digest()
        return hmac.compare_digest(a, b)

    def throttled(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._fails[ip]
            while q and now - q[0] > self.FAIL_WINDOW_S:
                q.popleft()
            return len(q) >= self.FAIL_LIMIT

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._fails[ip].append(time.monotonic())

    # -- sessions -------------------------------------------------------------------------------------
    def issue_session(self) -> tuple[str, str]:
        """Returns (cookie value, csrf token)."""
        nonce = secrets.token_urlsafe(16)
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + self.s.session_hours * 3600, "n": nonce}).encode()).decode().rstrip("=")
        sig = hmac.new(self._key(b"session"), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}", self.csrf_for(nonce)

    def csrf_for(self, nonce: str) -> str:
        return hmac.new(self._key(b"csrf"), nonce.encode(), hashlib.sha256).hexdigest()

    def read_session(self, cookie: str | None) -> Principal | None:
        if not cookie or "." not in cookie:
            return None
        payload, _, sig = cookie.rpartition(".")
        good = hmac.new(self._key(b"session"), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if int(data["exp"]) < time.time():
                return None
            return Principal("cookie", str(data["n"]))
        except (ValueError, KeyError, TypeError):
            return None

    def authenticate(self, request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            if self.check_password(header[7:].strip()):
                return Principal("bearer")
            raise NotAuthenticated()
        p = self.read_session(request.cookies.get(COOKIE))
        if p is None:
            raise NotAuthenticated()
        return p

    def enforce_csrf(self, request: Request, p: Principal) -> None:
        """Cookie-authenticated state changes need the CSRF header and a same-origin Origin. Bearer requests carry no ambient credential."""
        if p.kind != "cookie" or request.method not in UNSAFE:
            return
        origin = request.headers.get("origin")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if origin and origin.split("://", 1)[-1] != host:
            raise HTTPException(403, {"code": "BAD_ORIGIN", "message": "cross-origin request refused"})
        token = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(token, self.csrf_for(p.nonce)):
            raise HTTPException(403, {"code": "BAD_CSRF", "message": "missing or invalid CSRF token"})
