"""Content Understanding REST client (GA API 2025-11-01): analyze + poll, and the setup/management calls.

Provider error bodies are reduced to HTTP status plus the provider's outer and inner error codes; free-text provider
messages are never persisted or logged."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from ..config import Settings
from ..domain.errors import StageError

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
CONFIG_INNER_CODES = {"DefaultDeploymentModelNotFound", "DefaultsNotSet", "ModelDeploymentNotFound"}
RETRYABLE_OPERATION_CODES = {"InternalServerError", "ServiceUnavailable", "Timeout", "TooManyRequests", "RateLimitExceeded", "ServerBusy"}
RUNNING = {"notstarted", "running"}


@dataclass(frozen=True)
class AnalyzeOutcome:
    raw: bytes  # the final operation JSON, byte for byte
    operation_id: str | None
    request_id: str


class CuSetupError(Exception):
    """Setup/management failure with a secret-free message that includes the provider's inner error code."""


def error_codes(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        err = resp.json().get("error", {})
        inner = err.get("innererror") or err.get("innerError") or {}
        return err.get("code"), inner.get("code") if isinstance(inner, dict) else None
    except (ValueError, AttributeError):
        return None, None


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def token_provider_for(settings: Settings) -> Callable[[], str | None]:
    if settings.cu_auth == "none":
        return lambda: None
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    cid = settings.azure_client_id or None
    cred = (ManagedIdentityCredential(client_id=cid) if cid and settings.app_env == "prod"
            else DefaultAzureCredential(managed_identity_client_id=cid))
    return lambda: cred.get_token(TOKEN_SCOPE).token


class CuClient:
    def __init__(self, settings: Settings, token_provider: Callable[[], str | None] | None = None,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 poll_intervals: tuple[float, float] = (2.0, 10.0)):
        self._s = settings
        self._base = settings.cu_endpoint.rstrip("/")
        self._token = token_provider or token_provider_for(settings)
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self._sleep = sleep
        self._poll_min, self._poll_max = poll_intervals
        self.api_version = settings.cu_api_version

    # -- plumbing -----------------------------------------------------------------------------------
    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        tok = self._token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if request_id:
            h["x-ms-client-request-id"] = request_id
        return h

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self._base}/contentunderstanding/{path}{sep}api-version={self.api_version}"

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _http_error(resp: httpx.Response, request_id: str | None) -> StageError:
        status = resp.status_code
        code, inner = error_codes(resp)
        detail = f"HTTP {status} {code or '-'}/{inner or '-'}"
        kw = dict(http_status=status, provider_request_id=request_id)
        if status == 429:
            return StageError("PROVIDER_THROTTLED", detail, retry_after_s=_retry_after(resp), **kw)
        if status >= 500:
            return StageError("PROVIDER_UNAVAILABLE", detail, retry_after_s=_retry_after(resp), **kw)
        if status in (401, 403, 404) or inner in CONFIG_INNER_CODES or code in CONFIG_INNER_CODES:
            return StageError("PROVIDER_CONFIGURATION", detail, **kw)
        return StageError("PROVIDER_REQUEST_REJECTED", detail, **kw)

    def _send(self, method: str, url: str, request_id: str | None, **kw) -> httpx.Response:
        try:
            return self._http.request(method, url, headers=self._headers(request_id), **kw)
        except httpx.TimeoutException as e:
            raise StageError("PROVIDER_UNAVAILABLE", f"network timeout ({type(e).__name__})", provider_request_id=request_id) from None
        except httpx.TransportError as e:
            raise StageError("PROVIDER_UNAVAILABLE", f"network error ({type(e).__name__})", provider_request_id=request_id) from None

    # -- analyze ------------------------------------------------------------------------------------
    def analyze(self, analyzer_id: str, data: bytes, mime_type: str, request_id: str,
                on_poll: Callable[[], None] | None = None) -> AnalyzeOutcome:
        body = {"inputs": [{"data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type}]}
        resp = self._send("POST", self._url(f"analyzers/{analyzer_id}:analyze"), request_id, content=json.dumps(body))
        if resp.status_code != 202:
            if resp.status_code >= 400:
                raise self._http_error(resp, request_id)
            raise StageError("PROVIDER_RESPONSE_MALFORMED", f"unexpected HTTP {resp.status_code} on submit",
                             http_status=resp.status_code, provider_request_id=request_id)
        op_url = resp.headers.get("operation-location")
        if not op_url:
            raise StageError("PROVIDER_RESPONSE_MALFORMED", "submit response lacks Operation-Location",
                             http_status=202, provider_request_id=request_id)
        return self._poll(op_url, request_id, on_poll)

    def _poll(self, op_url: str, request_id: str, on_poll: Callable[[], None] | None) -> AnalyzeOutcome:
        deadline = time.monotonic() + self._s.cu_poll_timeout_s
        interval = self._poll_min
        poll_errors = 0
        while True:
            if on_poll:
                on_poll()
            try:
                resp = self._send("GET", op_url, request_id)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise self._http_error(resp, request_id)
                if resp.status_code >= 400:
                    raise self._http_error(resp, request_id)
                poll_errors = 0
            except StageError as e:
                poll_errors += 1
                if not e.retryable or poll_errors > 3:
                    raise
                resp = None
            if resp is not None:
                raw = resp.content
                try:
                    doc = json.loads(raw)
                    status = str(doc["status"]).lower()
                except (ValueError, KeyError, TypeError):
                    raise StageError("PROVIDER_RESPONSE_MALFORMED", "poll response is not an operation document",
                                     http_status=resp.status_code, provider_request_id=request_id, raw_response=raw or None) from None
                if status == "succeeded":
                    return AnalyzeOutcome(raw, doc.get("id"), request_id)
                if status in ("failed", "canceled", "cancelled"):
                    err = doc.get("error") or {}
                    inner = (err.get("innererror") or err.get("innerError") or {}) if isinstance(err, dict) else {}
                    code = err.get("code") if isinstance(err, dict) else None
                    icode = inner.get("code") if isinstance(inner, dict) else None
                    retry = status != "failed" or (icode in RETRYABLE_OPERATION_CODES or code in RETRYABLE_OPERATION_CODES)
                    raise StageError("PROVIDER_OPERATION_FAILED", f"operation {status}: {code or '-'}/{icode or '-'}",
                                     retryable=retry, provider_request_id=request_id, raw_response=raw)
                if status not in RUNNING:
                    raise StageError("PROVIDER_RESPONSE_MALFORMED", "unknown operation status", provider_request_id=request_id,
                                     raw_response=raw)
            if time.monotonic() > deadline:
                raise StageError("PROVIDER_TIMEOUT", f"operation not finished after {self._s.cu_poll_timeout_s}s",
                                 provider_request_id=request_id)
            self._sleep(interval)
            interval = min(self._poll_max, interval * 1.5)

    # -- setup / management -------------------------------------------------------------------------
    def _mgmt(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return self._send(method, self._url(path), None, **kw)
        except StageError as e:
            raise CuSetupError(f"{method} {path.split('?')[0]}: {e.message}") from None

    @staticmethod
    def _fail(what: str, resp: httpx.Response) -> CuSetupError:
        code, inner = error_codes(resp)
        return CuSetupError(f"{what}: HTTP {resp.status_code} {code or '-'}/{inner or '-'}")

    def pause(self) -> None:
        self._sleep(self._poll_min)

    def get_defaults(self) -> dict | None:
        """Model defaults, or None when none were ever set (a 400 with inner code DefaultsNotSet; K-011)."""
        resp = self._mgmt("GET", "defaults")
        if resp.status_code == 200:
            return resp.json()
        code, inner = error_codes(resp)
        if resp.status_code == 400 and "DefaultsNotSet" in (code, inner):
            return None
        raise self._fail("read defaults", resp)

    def patch_defaults(self, model_deployments: dict[str, str]) -> dict:
        resp = self._mgmt("PATCH", "defaults", content=json.dumps({"modelDeployments": model_deployments}),
                          )
        if resp.status_code >= 300:
            raise self._fail("patch defaults", resp)
        return resp.json() if resp.content else {}

    def get_analyzer(self, analyzer_id: str) -> dict | None:
        resp = self._mgmt("GET", f"analyzers/{analyzer_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise self._fail(f"read analyzer {analyzer_id}", resp)
        return resp.json()

    def put_analyzer(self, analyzer_id: str, definition: dict, allow_replace: bool = False, timeout_s: float = 300.0) -> None:
        resp = self._mgmt("PUT", f"analyzers/{analyzer_id}?allowReplace={'true' if allow_replace else 'false'}",
                          content=json.dumps(definition))
        if resp.status_code >= 300:
            raise self._fail(f"register analyzer {analyzer_id}", resp)
        op = resp.headers.get("operation-location")
        deadline = time.monotonic() + timeout_s
        while op:  # long-running create: wait for the operation, then confirm readiness
            r = self._send("GET", op, None)
            if r.status_code >= 400:
                raise self._fail(f"register analyzer {analyzer_id}", r)
            status = str(r.json().get("status", "")).lower() if r.content else "succeeded"
            if status == "succeeded":
                break
            if status in ("failed", "canceled"):
                code, inner = error_codes(r)
                err = (r.json().get("error") or {})
                raise CuSetupError(f"register analyzer {analyzer_id}: operation {status} {err.get('code') or '-'}/"
                                   f"{(err.get('innererror') or {}).get('code') or '-'}")
            if time.monotonic() > deadline:
                raise CuSetupError(f"register analyzer {analyzer_id}: timed out")
            self._sleep(self._poll_min)
        a = self.get_analyzer(analyzer_id)
        if a is None or str(a.get("status", "ready")).lower() not in ("ready", "succeeded"):
            raise CuSetupError(f"analyzer {analyzer_id} is not ready after registration")
