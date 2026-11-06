"""FastAPI application factory. Everything except /healthz and the sign-in endpoints sits behind the authenticated router."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..adapters.blobstore import BlobStore
from ..adapters.keyvault import PasswordProvider
from ..config import Settings
from ..db.connection import Database
from ..domain.logs import log
from .api import build_api_router
from .auth import COOKIE, Auth, NotAuthenticated, Principal
from .ui import build_ui_router, render


@dataclass
class Context:
    settings: Settings
    db: Database
    blob: BlobStore
    auth: Auth


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'",
}


def create_app(settings: Settings, db: Database, blob: BlobStore, passwords: PasswordProvider | None = None) -> FastAPI:
    passwords = passwords or PasswordProvider(settings)
    ctx = Context(settings, db, blob, Auth(settings, passwords))
    app = FastAPI(title="Document Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if request.url.path != "/healthz":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(NotAuthenticated)
    async def _unauth(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": {"code": "UNAUTHENTICATED", "message": "sign in required"}}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        d = exc.detail if isinstance(exc.detail, dict) else {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail)}
        if not request.url.path.startswith("/api/") and exc.status_code in (403, 404, 409):
            return HTMLResponse(render("error.html", request, ctx, error=d, status=exc.status_code), status_code=exc.status_code)
        return JSONResponse({"error": d}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": {"code": "BAD_REQUEST", "message": "the request could not be understood"}}, status_code=422)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        log("request_failed", logging.ERROR, error_class=type(exc).__name__)
        return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "unexpected error"}}, status_code=500)

    # -- public: health and sign-in -------------------------------------------------------------------
    pub = APIRouter()

    @pub.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @pub.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return HTMLResponse(render("login.html", request, ctx, error=None))

    def _set_cookie(resp, cookie: str):
        resp.set_cookie(COOKIE, cookie, max_age=settings.session_hours * 3600, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/")

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @pub.post("/login")
    def login(request: Request, password: str = Form("")):
        ip = _client_ip(request)
        if ctx.auth.throttled(ip):
            return HTMLResponse(render("login.html", request, ctx, error="Too many attempts. Wait a minute and try again."), status_code=429)
        if not ctx.auth.check_password(password):
            ctx.auth.record_failure(ip)
            import time

            time.sleep(0.5)
            return HTMLResponse(render("login.html", request, ctx, error="Incorrect password."), status_code=401)
        cookie, _ = ctx.auth.issue_session()
        resp = RedirectResponse("/", status_code=303)
        _set_cookie(resp, cookie)
        return resp

    @pub.post("/api/auth/login")
    def api_login(body: dict, request: Request):
        ip = _client_ip(request)
        pw = body.get("password") if isinstance(body, dict) else None
        if ctx.auth.throttled(ip):
            raise HTTPException(429, {"code": "THROTTLED", "message": "too many failed attempts"})
        if not isinstance(pw, str) or not ctx.auth.check_password(pw):
            ctx.auth.record_failure(ip)
            import time

            time.sleep(0.5)
            raise HTTPException(401, {"code": "BAD_CREDENTIALS", "message": "incorrect password"})
        cookie, csrf = ctx.auth.issue_session()
        resp = JSONResponse({"ok": True, "csrf_token": csrf})
        _set_cookie(resp, cookie)
        return resp

    @pub.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    app.include_router(pub)

    # -- everything else requires sign-in -----------------------------------------------------------------
    def require_auth(request: Request) -> Principal:
        p = ctx.auth.authenticate(request)
        ctx.auth.enforce_csrf(request, p)
        request.state.principal = p
        return p

    secure = APIRouter(dependencies=[Depends(require_auth)])
    secure.include_router(build_api_router(ctx))
    secure.include_router(build_ui_router(ctx))
    app.include_router(secure)
    return app
