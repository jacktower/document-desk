"""Shared fixtures. Integration tests use a REAL SQL Server (TEST_SQL_SERVER etc.); Blob, Service Bus, Event Grid and
Content Understanding are simulated (see devtools/). Tests skip with a reason, never pass silently, when SQL is absent."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest

from docdesk.config import Settings
from docdesk.cu import analyzer_config as AC
from docdesk.cu.client import CuClient
from docdesk.db.connection import Database
from docdesk.db.migrate import migrate
from docdesk.pipeline.runtime import Runtime
from docdesk_devtools.fake_blob import FsBlobStore
from docdesk_devtools.fake_broker import InMemoryBroker
from docdesk_devtools.sim_cu import SimulatedCu

SQL_HOST = os.environ.get("TEST_SQL_SERVER", "")
SQL_USER = os.environ.get("TEST_SQL_USER", "sa")
SQL_PASSWORD = os.environ.get("TEST_SQL_PASSWORD", "")
ROOT = Path(__file__).resolve().parents[1]


def dev_settings(**over) -> Settings:
    base = dict(app_env="dev", sql_server=SQL_HOST, sql_database="master", sql_auth="sql_password", sql_user=SQL_USER,
                sql_password=SQL_PASSWORD, sql_trust_server_certificate=True, cookie_secure=False, operator_password="test-operator-password",
                cu_endpoint="http://cu.invalid", cu_auth="none", log_level="WARNING", dispatch_poll_s=0.05, reaper_poll_s=0.2,
                health_poll_s=0.5, cu_poll_timeout_s=30)
    base.update(over)
    return Settings(**base)


@pytest.fixture(scope="session")
def sql_database():
    if not SQL_HOST or not SQL_PASSWORD:
        pytest.skip("TEST_SQL_SERVER / TEST_SQL_PASSWORD not set: real-SQL integration tests skipped (see README)")
    admin = Database(dev_settings())
    name = f"docdesk_test_{uuid.uuid4().hex[:8]}"
    try:
        conn = admin.connect(autocommit=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"SQL Server at {SQL_HOST} is not reachable: {type(e).__name__}")
    conn.execute(f"CREATE DATABASE [{name}]")
    # match Azure SQL Database's default so locking behaviour under test is the deployed behaviour
    conn.execute(f"ALTER DATABASE [{name}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE")
    conn.close()
    db = Database(dev_settings(sql_database=name))
    migrate(db)
    yield name
    c = admin.connect(autocommit=True)
    c.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
    c.execute(f"DROP DATABASE [{name}]")
    c.close()


TABLES_IN_DELETE_ORDER = ["document_fields", "document_results", "artifacts", "stage_tries", "outbox", "stage_states",
                          "processing_attempts", "documents", "worker_heartbeats", "dependency_health"]


@pytest.fixture
def settings(sql_database) -> Settings:
    return dev_settings(sql_database=sql_database)


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings)
    c = d.connect(autocommit=True)
    for t in TABLES_IN_DELETE_ORDER:
        c.execute(f"DELETE FROM dbo.{t}")
    c.close()
    return d


@pytest.fixture
def sim() -> SimulatedCu:
    s = SimulatedCu()
    s.polls_before_done = 1
    return s


def register_sim_analyzers(sim: SimulatedCu, settings: Settings) -> None:
    sim.defaults = {settings.cu_completion_model: "gpt-4.1", "prebuilt-analyzer-embedding": "emb"}
    sim.analyzers.update(AC.all_definitions(settings))


@pytest.fixture
def rt(settings, db, sim, tmp_path) -> Runtime:
    register_sim_analyzers(sim, settings)
    blob = FsBlobStore(tmp_path / "blobs")
    http = httpx.Client(transport=httpx.WSGITransport(app=sim), base_url="http://cu.invalid")
    cu = CuClient(settings, http=http, sleep=lambda s: None)
    return Runtime(settings=settings, db=db, blob=blob, broker=InMemoryBroker(lock_s=5.0), cu=cu, worker_id="test-worker")


@pytest.fixture(scope="session")
def testdocs(tmp_path_factory) -> Path:
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_testdocs", ROOT / "tools" / "gen_testdocs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path_factory.mktemp("testdocs")
    mod.main(out)
    return out
