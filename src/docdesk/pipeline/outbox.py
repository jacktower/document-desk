"""Outbox dispatcher: publishes committed outbox rows to Service Bus. Safe with any number of concurrent dispatchers.

A crash after send but before the mark means the row is claimed again later and the message is sent twice; stage handlers
absorb duplicates. A broker outage leaves rows in SQL until the broker is back."""
from __future__ import annotations

import logging

from ..db import repo_pipeline as repo
from ..domain.logs import log
from .runtime import Runtime


def dispatch_once(rt: Runtime, batch: int = 50) -> int:
    rows = repo.claim_outbox(rt.db, rt.worker_id, batch)
    sent = 0
    for r in rows:
        oid = str(r["outbox_id"]).lower()
        try:
            rt.broker.send(r["queue"], r["body"], oid, r.get("stage"))
        except Exception as e:  # broker outage or throttling: leave the row; its claim expires and it is retried
            repo.mark_dispatch_failed(rt.db, oid, type(e).__name__, r["dispatch_attempts"])
            log("outbox_send_failed", logging.WARNING, outbox_id=oid, queue=r["queue"], error_class=type(e).__name__)
            break
        repo.mark_dispatched(rt.db, oid)
        sent += 1
    return sent
