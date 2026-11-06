"""Worker heartbeat and dependency probes, stored in SQL so the web tier can show them without holding any other role."""
from __future__ import annotations

import logging

from ..cu.analyzer_config import classifier_id
from ..cu.client import CuSetupError
from ..db.connection import is_unique_violation
from ..domain.logs import log
from ..domain.stages import BLOB_EVENTS_QUEUE
from .runtime import Runtime

import pyodbc

VERSION = "1.0.0"


def _upsert(cur, table: str, key: str, key_value: str, cols: dict) -> None:
    sets = ", ".join(f"{c}=?" for c in cols)
    cur.execute(f"UPDATE dbo.{table} SET {sets} WHERE {key}=?", *cols.values(), key_value)
    if cur.rowcount == 0:
        names = ", ".join([key, *cols])
        cur.execute(f"INSERT dbo.{table} ({names}) VALUES ({', '.join('?' * (len(cols) + 1))})", key_value, *cols.values())


def heartbeat(rt: Runtime, started_at) -> None:
    def _do(cur):
        cur.execute("UPDATE dbo.worker_heartbeats SET last_seen_at=SYSUTCDATETIME(), version=? WHERE worker_id=?", VERSION, rt.worker_id)
        if cur.rowcount == 0:
            cur.execute("INSERT dbo.worker_heartbeats (worker_id, role, version, started_at, last_seen_at) VALUES (?,?,?,?,SYSUTCDATETIME())",
                        rt.worker_id, "worker", VERSION, started_at)
    try:
        rt.db.run(_do)
    except pyodbc.Error as e:
        if not is_unique_violation(e):
            raise


def _record(rt: Runtime, name: str, status: str, error_class: str | None = None, detail: str | None = None) -> None:
    def _do(cur):
        cur.execute("UPDATE dbo.dependency_health SET status=?, checked_at=SYSUTCDATETIME(), error_class=?, detail=? WHERE name=?",
                    status, error_class, detail, name)
        if cur.rowcount == 0:
            cur.execute("INSERT dbo.dependency_health (name, status, checked_at, error_class, detail) VALUES (?,?,SYSUTCDATETIME(),?,?)",
                        name, status, error_class, detail)
    try:
        rt.db.run(_do)
    except pyodbc.Error as e:
        if not is_unique_violation(e):
            raise


def probe_all(rt: Runtime) -> dict[str, str]:
    s = rt.settings
    out: dict[str, str] = {}

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            _record(rt, name, "ok", None, detail)
            out[name] = "ok"
        except CuSetupError as e:
            _record(rt, name, "error", "CuSetupError", str(e)[:300])
            out[name] = "error"
        except pyodbc.Error:
            raise
        except Exception as e:
            _record(rt, name, "error", type(e).__name__, None)
            out[name] = "error"
            log("dependency_probe_failed", logging.WARNING, check=name, error_class=type(e).__name__)

    check("servicebus", lambda: rt.broker.probe(BLOB_EVENTS_QUEUE) or None)
    check("blob", lambda: (rt.blob.exists(s.artifacts_container, "_probe") and None) or None)

    def cu_defaults():
        d = rt.cu.get_defaults()
        if d is None:
            raise CuSetupError("model defaults are not set (DefaultsNotSet): run setup-analyzers --set-defaults")
        return None
    check("content_understanding", cu_defaults)

    def analyzer():
        a = rt.cu.get_analyzer(classifier_id(s.analyzer_config_version))
        if a is None:
            raise CuSetupError("classifier analyzer is not registered: run setup-analyzers")
        st = str(a.get("status", "ready")).lower()
        if st not in ("ready", "succeeded"):
            raise CuSetupError(f"classifier analyzer status is {st}")
        return None
    check("analyzer", analyzer)
    return out
