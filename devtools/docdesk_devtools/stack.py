"""Local development stack: web + worker in one process on a REAL SQL Server, with SIMULATED Blob/Event Grid/Service Bus/
Content Understanding. Nothing here talks to Azure. Every result is stamped simulated and the UI says so.

    TEST_SQL_SERVER=127.0.0.1,1433 TEST_SQL_PASSWORD=... python -m docdesk_devtools.stack [--port 8000]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
from pathlib import Path
from wsgiref.simple_server import WSGIServer, make_server, WSGIRequestHandler
from socketserver import ThreadingMixIn

import uvicorn

from docdesk.config import Settings
from docdesk.cu.client import CuClient
from docdesk.cu.setup import setup_analyzers
from docdesk.db.connection import Database
from docdesk.db.migrate import migrate
from docdesk.domain.logs import setup_logging
from docdesk.domain.stages import BLOB_EVENTS_QUEUE
from docdesk.pipeline.runtime import Runtime
from docdesk.pipeline.worker import Worker
from docdesk.web.app import create_app

from .events import blob_created_event
from .fake_blob import FsBlobStore
from .fake_broker import InMemoryBroker
from .sim_cu import SimulatedCu


class EventingBlobStore(FsBlobStore):
    """After a successful put into the originals container, emit the BlobCreated event Event Grid would deliver."""

    def __init__(self, root, broker, originals: str):
        super().__init__(root)
        self._broker, self._originals = broker, originals

    def put(self, container, name, data, content_type, overwrite=False):
        super().put(container, name, data, content_type, overwrite)
        if container == self._originals:
            info = self.head(container, name)
            self._broker.inject(BLOB_EVENTS_QUEUE, blob_created_event(container, name, info.etag or "", info.size, content_type))


class _Quiet(WSGIRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass


class _Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cu-port", type=int, default=8788)
    ap.add_argument("--db", default="docdesk_dev")
    ap.add_argument("--data", default=str(Path(tempfile.gettempdir()) / "docdesk-dev"))
    args = ap.parse_args()
    host, pw = os.environ.get("TEST_SQL_SERVER"), os.environ.get("TEST_SQL_PASSWORD")
    if not host or not pw:
        sys.exit("set TEST_SQL_SERVER (e.g. 127.0.0.1,1433) and TEST_SQL_PASSWORD for a local SQL Server")
    base = dict(app_env="dev", sql_server=host, sql_auth="sql_password", sql_user=os.environ.get("TEST_SQL_USER", "sa"), sql_password=pw,
                sql_trust_server_certificate=True, cookie_secure=False, operator_password=os.environ.get("DEV_OPERATOR_PASSWORD", "dev-password"),
                cu_endpoint=f"http://127.0.0.1:{args.cu_port}", cu_auth="none", log_level="INFO",
                cu_model_deployments='{"gpt-4.1":"sim","prebuilt-analyzer-embedding":"sim"}')
    setup_logging("INFO")
    admin = Database(Settings(**base, sql_database="master")).connect(autocommit=True)
    admin.execute(f"IF DB_ID('{args.db}') IS NULL BEGIN CREATE DATABASE [{args.db}]; ALTER DATABASE [{args.db}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE; END")
    admin.close()
    s = Settings(**base, sql_database=args.db)
    db = Database(s)
    print("migrations applied:", migrate(db) or "none (up to date)")

    sim = SimulatedCu()
    srv = make_server("127.0.0.1", args.cu_port, sim, server_class=_Threaded, handler_class=_Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cu = CuClient(s)
    rc = setup_analyzers(cu, s, set_defaults=True)
    assert rc == 0, "analyzer setup against the simulated service failed"

    broker = InMemoryBroker()
    blob = EventingBlobStore(Path(args.data) / "blobs", broker, s.originals_container)
    rt = Runtime(settings=s, db=db, blob=blob, broker=broker, cu=cu)
    Worker(rt).start()
    app = create_app(s, db, blob)
    print(f"\nDocument Desk (LOCAL SIMULATED STACK) on http://127.0.0.1:{args.port}   operator password: {s.operator_password}\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
