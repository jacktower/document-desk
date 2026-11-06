"""Entry point: docdesk web | worker | migrate | setup-analyzers | smoke."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .config import ConfigError, get_settings
from .domain.logs import setup_logging


def _web(args) -> int:
    import uvicorn

    from .adapters.blobstore import AzureBlobStore
    from .db.connection import Database
    from .web.app import create_app

    s = get_settings()
    s.require("sql_server", "sql_database", "storage_account_url")
    app = create_app(s, Database(s), AzureBlobStore(s))
    uvicorn.run(app, host="0.0.0.0", port=args.port, proxy_headers=True, forwarded_allow_ips="*", log_level="warning", access_log=False)
    return 0


def _worker(args) -> int:
    from .pipeline.factory import build_runtime
    from .pipeline.worker import Worker

    rt = build_runtime(get_settings())
    w = Worker(rt)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    w.start()
    stop.wait()
    w.shutdown()
    return 0


def _migrate(args) -> int:
    from .db.connection import Database
    from .db.migrate import migrate

    s = get_settings()
    s.require("sql_server", "sql_database")
    applied = migrate(Database(s))
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


def _setup(args) -> int:
    from .cu.client import CuClient
    from .cu.setup import setup_analyzers

    s = get_settings()
    s.require("cu_endpoint")
    return setup_analyzers(CuClient(s), s, check=args.check, set_defaults=args.set_defaults)


def _smoke(args) -> int:
    import os

    from .smoke import run_direct_cu, run_smoke

    docs = Path(args.docs)
    if args.direct_cu:
        return run_direct_cu(docs)
    password = os.environ.get("SMOKE_OPERATOR_PASSWORD", "")
    if not password:
        import getpass

        password = getpass.getpass("Operator password: ")
    return run_smoke(args.base_url, password, docs, args.files or None, args.timeout)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="docdesk")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("web", help="run the web UI and API")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(fn=_web)
    sub.add_parser("worker", help="run the event-driven workers").set_defaults(fn=_worker)
    sub.add_parser("migrate", help="apply SQL migrations").set_defaults(fn=_migrate)
    sa = sub.add_parser("setup-analyzers", help="set Content Understanding model defaults and register analyzers")
    sa.add_argument("--check", action="store_true", help="read-only: report what is missing, change nothing")
    sa.add_argument("--set-defaults", action="store_true", help="PATCH model defaults from CU_MODEL_DEPLOYMENTS when missing")
    sa.set_defaults(fn=_setup)
    sm = sub.add_parser("smoke", help="Azure smoke test against a deployed web app")
    sm.add_argument("--base-url", default="", help="https://<web app FQDN>")
    sm.add_argument("--docs", default="testdocs", help="directory produced by tools/gen_testdocs.py")
    sm.add_argument("--files", nargs="*", help="file names inside --docs (default: a small mixed set)")
    sm.add_argument("--timeout", type=int, default=600)
    sm.add_argument("--direct-cu", action="store_true", help="call the classifier analyzer directly, bypassing the pipeline")
    sm.set_defaults(fn=_smoke)
    args = p.parse_args(argv)
    if args.cmd == "smoke" and not args.direct_cu and not args.base_url:
        p.error("smoke needs --base-url (or --direct-cu)")
    try:
        s = get_settings()
        setup_logging(s.log_level)
        return args.fn(args)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
