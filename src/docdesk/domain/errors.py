"""Error catalogue: every failure a stage can report has a stable code and a retry policy."""
from __future__ import annotations

# code -> (retryable, rejects_document)
CATALOG: dict[str, tuple[bool, bool]] = {
    "PROVIDER_THROTTLED": (True, False),
    "PROVIDER_UNAVAILABLE": (True, False),
    "PROVIDER_TIMEOUT": (True, False),
    "PROVIDER_OPERATION_FAILED": (False, False),  # may be overridden per inner code
    "PROVIDER_REQUEST_REJECTED": (False, False),
    "PROVIDER_CONFIGURATION": (False, False),
    "PROVIDER_RESPONSE_MALFORMED": (True, False),
    "PROVIDER_RESULT_UNUSABLE": (False, False),
    "INPUT_UNSUPPORTED": (False, True),
    "INPUT_CORRUPT": (False, True),
    "INPUT_TOO_LARGE": (False, True),
    "INPUT_TOO_MANY_PAGES": (False, True),
    "STORAGE_TRANSIENT": (True, False),
    "STORAGE_MISSING": (True, False),
    "SQL_TRANSIENT": (True, False),
    "ARTIFACT_MISSING": (False, False),
    "INTERNAL_ERROR": (True, False),
    "CRASH_LOOP_EXHAUSTED": (False, False),
}


class StageError(Exception):
    """Raised by a stage handler. Carries a catalogue code; messages must be safe to store/log."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool | None = None,
        http_status: int | None = None,
        retry_after_s: float | None = None,
        provider_request_id: str | None = None,
        raw_response: bytes | None = None,
    ):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = (message or "")[:500]
        default_retry, self.rejects_document = CATALOG.get(code, (False, False))
        self.retryable = default_retry if retryable is None else retryable
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        self.provider_request_id = provider_request_id
        self.raw_response = raw_response          # final provider bytes received before the failure, if any
        self.pending_artifacts: list[dict] = []   # same, but the blob write failed: bytes are kept in SQL for later persistence
        self.artifacts: list[dict] = []           # descriptors of artifacts stored for this failure (filled by the stage handler)


class LeaseLost(Exception):
    """The stage try no longer owns its lease; its effects must be discarded."""


class UploadRejection(Exception):
    """An upload that is refused before any row is created. `status` is the HTTP status the API returns."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status
