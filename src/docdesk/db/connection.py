"""SQL connectivity: ODBC Driver 18, Entra token auth (managed identity) or SQL password (dev only)."""
from __future__ import annotations

import struct
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

import pyodbc

from ..config import Settings

SQL_COPT_SS_ACCESS_TOKEN = 1256
DRIVER = "ODBC Driver 18 for SQL Server"
T = TypeVar("T")

# deadlock victim, snapshot conflict, transient connectivity errors worth one more try
_TRANSIENT_STATES = {"40001", "40197", "40501", "40613", "08S01", "08001", "HYT00", "HYT01"}
_TRANSIENT_NATIVE = (1205, 40197, 40501, 40613, 49918, 49919, 49920, 4060, 10928, 10929)


def is_transient(exc: BaseException) -> bool:
    if not isinstance(exc, pyodbc.Error):
        return False
    args = exc.args or ()
    if args and str(args[0]) in _TRANSIENT_STATES:
        return True
    text = " ".join(str(a) for a in args)
    return any(f"({n})" in text for n in _TRANSIENT_NATIVE)


def is_unique_violation(exc: BaseException) -> bool:
    return isinstance(exc, pyodbc.IntegrityError) and any(f"({n})" in " ".join(map(str, exc.args)) for n in (2627, 2601))


class Database:
    def __init__(self, settings: Settings, token_provider: Callable[[], tuple[str, float]] | None = None):
        self._s = settings
        self._token_provider = token_provider
        self._token: tuple[bytes, float] | None = None
        self._lock = threading.Lock()
        pyodbc.pooling = True

    # -- connection ---------------------------------------------------------------------------
    def _conn_str(self, database: str | None = None) -> str:
        s = self._s
        parts = [
            f"DRIVER={{{DRIVER}}}", f"SERVER={s.sql_server}", f"DATABASE={database or s.sql_database}",
            "Encrypt=yes", f"TrustServerCertificate={'yes' if s.sql_trust_server_certificate else 'no'}",
            "Connection Timeout=30",
        ]
        if s.sql_auth == "sql_password":
            parts += [f"UID={s.sql_user}", f"PWD={{{s.sql_password.replace('}', '}}')}}}"]
        return ";".join(parts)

    def _access_token_struct(self) -> bytes:
        with self._lock:
            now = time.time()
            if self._token is None or self._token[1] - now < 300:
                if self._token_provider is None:
                    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

                    cid = self._s.azure_client_id or None
                    cred = ManagedIdentityCredential(client_id=cid) if (cid and self._s.app_env == "prod") else DefaultAzureCredential(
                        managed_identity_client_id=cid)
                    tok = cred.get_token("https://database.windows.net/.default")
                    raw, exp = tok.token, float(tok.expires_on)
                else:
                    raw, exp = self._token_provider()
                data = raw.encode("utf-16-le")
                self._token = (struct.pack(f"<I{len(data)}s", len(data), data), exp)
            return self._token[0]

    def connect(self, database: str | None = None, autocommit: bool = False) -> pyodbc.Connection:
        attrs = {}
        if self._s.sql_auth == "managed_identity":
            attrs = {SQL_COPT_SS_ACCESS_TOKEN: self._access_token_struct()}
        return pyodbc.connect(self._conn_str(database), attrs_before=attrs, autocommit=autocommit)

    # -- transactions ---------------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[pyodbc.Cursor]:
        """One transaction. Commits on success, rolls back on any exception."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except pyodbc.Error:
                pass
            raise
        finally:
            conn.close()

    def run(self, fn: Callable[[pyodbc.Cursor], T], retries: int = 3) -> T:
        """Run fn(cursor) in one transaction, retrying the whole transaction when it was a deadlock victim."""
        for i in range(retries + 1):
            try:
                with self.tx() as cur:
                    return fn(cur)
            except pyodbc.Error as e:
                if i < retries and is_transient(e):
                    time.sleep(0.05 * (2 ** i))
                    continue
                raise
        raise AssertionError("unreachable")

    def query(self, sql: str, *params) -> list[dict]:
        def _q(cur: pyodbc.Cursor):
            cur.execute(sql, *params)
            return rows_as_dicts(cur)
        return self.run(_q)

    def scalar(self, sql: str, *params):
        def _q(cur: pyodbc.Cursor):
            cur.execute(sql, *params)
            row = cur.fetchone()
            return row[0] if row else None
        return self.run(_q)


def rows_as_dicts(cur: pyodbc.Cursor) -> list[dict]:
    if cur.description is None:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
