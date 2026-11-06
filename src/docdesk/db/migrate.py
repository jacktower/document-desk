"""Forward-only migrations: plain T-SQL files in /migrations, batches separated by a line containing only GO."""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..domain.logs import log
from .connection import Database


def migrations_dir() -> Path:
    """MIGRATIONS_DIR, else the repository checkout's /migrations, else /app/migrations (the container image)."""
    override = os.environ.get("MIGRATIONS_DIR")
    if override:
        return Path(override)
    checkout = Path(__file__).resolve().parents[3] / "migrations"
    return checkout if checkout.is_dir() else Path("/app/migrations")


def split_batches(sql: str) -> list[str]:
    return [b.strip() for b in re.split(r"^\s*GO\s*$", sql, flags=re.MULTILINE | re.IGNORECASE) if b.strip()]


def migrate(db: Database, directory: Path | None = None) -> list[str]:
    """Apply pending migrations in id order; returns the ids applied (empty when up to date)."""
    directory = directory or migrations_dir()
    applied: list[str] = []
    conn = db.connect(autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "IF OBJECT_ID('dbo.schema_migrations') IS NULL CREATE TABLE dbo.schema_migrations ("
            "migration_id VARCHAR(100) NOT NULL PRIMARY KEY, applied_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME())")
        # one migrator at a time, across replicas
        cur.execute("SELECT 1")
        done = {r[0] for r in cur.execute("SELECT migration_id FROM dbo.schema_migrations").fetchall()}
        for path in sorted(directory.glob("*.sql")):
            if path.stem in done:
                continue
            cur.execute("BEGIN TRAN")
            try:
                cur.execute("EXEC sp_getapplock @Resource='docdesk_migrate', @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=60000")
                if cur.execute("SELECT COUNT(*) FROM dbo.schema_migrations WHERE migration_id=?", path.stem).fetchone()[0]:
                    cur.execute("COMMIT")
                    continue
                for batch in split_batches(path.read_text(encoding="utf-8")):
                    cur.execute(batch)
                    while cur.nextset():
                        pass
                cur.execute("INSERT dbo.schema_migrations (migration_id) VALUES (?)", path.stem)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("IF @@TRANCOUNT > 0 ROLLBACK")
                raise
            applied.append(path.stem)
            log("migration_applied", detail_code=path.stem)
    finally:
        conn.close()
    return applied
