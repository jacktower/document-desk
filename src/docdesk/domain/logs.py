"""Structured, allow-listed JSON logging. Document content, filenames, field values and tokens never reach a log."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

ALLOWED_KEYS = frozenset({
    "document_id", "attempt_no", "stage", "try_no", "worker_id", "error_code", "http_status",
    "duration_ms", "provider_request_id", "queue", "outbox_id", "count", "status", "check",
    "error_class", "detail_code",
})
LOGGER_NAME = "docdesk"
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in ALLOWED_KEYS and k not in _RESERVED and v is not None:
                out[k] = v if isinstance(v, (int, float, bool)) else str(v)
        if record.exc_info and record.exc_info[0] is not None:
            out["error_class"] = record.exc_info[0].__name__  # class only, never the message
        return json.dumps(out, separators=(",", ":"))


def setup_logging(level: str = "INFO", stream=None) -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(level.upper())
    log.handlers.clear()
    h = logging.StreamHandler(stream or sys.stdout)
    h.setFormatter(JsonFormatter())
    log.addHandler(h)
    log.propagate = False
    for noisy in ("azure", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log(event: str, level: int = logging.INFO, **fields) -> None:
    """Log `event` with allow-listed fields only; any other key is dropped here, before it can reach a record."""
    get_logger().log(level, event, extra={k: v for k, v in fields.items() if k in ALLOWED_KEYS})
