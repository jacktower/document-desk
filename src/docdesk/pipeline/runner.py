"""The stage runner: one implementation for all stages. claim (SQL) -> work (no transaction) -> fenced completion (SQL)."""
from __future__ import annotations

import logging
import time
from typing import Callable

import pyodbc

from ..db import repo_pipeline as repo
from ..domain.errors import LeaseLost, StageError
from ..domain.logs import log
from ..domain.retry import backoff_seconds
from .handlers import HANDLERS
from .messages import BadMessage, parse_blob_events, parse_stage_message
from .runtime import Runtime
from ..adapters.broker import Received

COMPLETE, ABANDON = "complete", "abandon"


def run_claim(rt: Runtime, claim: repo.Claim) -> str:
    """Executes one claimed stage try. Returns 'succeeded' | 'retry' | 'failed' | 'rejected' | 'lease_lost'."""
    t0 = time.monotonic()
    ctx = dict(document_id=claim.document_id, attempt_no=claim.attempt_no, stage=claim.stage, try_no=claim.try_no, worker_id=rt.worker_id)
    log("stage_started", **ctx)
    try:
        effects = HANDLERS[claim.stage](rt, claim)
        repo.complete_stage(rt.db, rt.settings, claim, effects)
        log("stage_succeeded", duration_ms=int((time.monotonic() - t0) * 1000), **ctx)
        return "succeeded"
    except LeaseLost:
        log("stage_lease_lost", logging.WARNING, **ctx)
        return "lease_lost"
    except StageError as err:
        return _record(rt, claim, err, ctx, t0)
    except pyodbc.Error as e:
        if _is_sql_unreachable(e):
            raise  # SQL itself is down: nothing can be recorded; the message is abandoned and the lease/reaper cover it
        return _record(rt, claim, StageError("SQL_TRANSIENT", f"sql error {type(e).__name__}"), ctx, t0)
    except Exception as e:  # a bug in a handler must not strand the stage
        log("stage_unexpected_error", logging.ERROR, error_class=type(e).__name__, **ctx)
        return _record(rt, claim, StageError("INTERNAL_ERROR", f"unexpected {type(e).__name__}"), ctx, t0)


def _is_sql_unreachable(e: pyodbc.Error) -> bool:
    return bool(e.args) and str(e.args[0]) in {"08001", "08S01", "HYT00", "HYT01", "28000"}


def _record(rt: Runtime, claim: repo.Claim, err: StageError, ctx: dict, t0: float) -> str:
    delay = backoff_seconds(claim.try_no, retry_after_s=err.retry_after_s)
    outcome = repo.record_failure(rt.db, claim, err, delay)
    level = logging.WARNING if outcome == "retry" else logging.ERROR
    log(f"stage_{outcome}", level, error_code=err.code, http_status=err.http_status,
        provider_request_id=err.provider_request_id, duration_ms=int((time.monotonic() - t0) * 1000), **ctx)
    return outcome


def handle_stage_message(rt: Runtime, msg: Received) -> str:
    """Settlement decision for one stage message. Messages direct work; SQL decides whether there is any."""
    try:
        sm = parse_stage_message(msg.body)
    except BadMessage:
        rt.broker.dead_letter(msg, "unparseable stage message")
        log("message_dead_lettered", logging.ERROR, queue=msg.queue, detail_code="UNPARSEABLE")
        return "settled"
    try:
        claim = repo.claim_stage(rt.db, rt.settings, sm.attempt_id, sm.stage, rt.worker_id, msg.message_id)
    except pyodbc.Error as e:
        log("claim_sql_error", logging.ERROR, error_class=type(e).__name__, queue=msg.queue)
        return ABANDON
    if claim is None:
        log("stage_message_ignored", logging.DEBUG, document_id=sm.document_id, stage=sm.stage, queue=msg.queue)
        return COMPLETE
    try:
        run_claim(rt, claim)
    except Exception as e:
        log("stage_run_aborted", logging.ERROR, error_class=type(e).__name__, document_id=claim.document_id, stage=claim.stage)
        return ABANDON
    return COMPLETE


def handle_blob_event_message(rt: Runtime, msg: Received) -> str:
    try:
        events, ignored = parse_blob_events(msg.body)
    except BadMessage:
        rt.broker.dead_letter(msg, "unparseable blob event")
        log("message_dead_lettered", logging.ERROR, queue=msg.queue, detail_code="UNPARSEABLE")
        return "settled"
    for _ in range(ignored):
        log("arrival_ignored", logging.INFO, queue=msg.queue)
    try:
        for ev in events:
            if ev.container != rt.settings.originals_container:
                log("arrival_ignored", queue=msg.queue)
                continue
            won = repo.register_arrival(rt.db, rt.settings, ev.document_id, ev.container, ev.blob_name, ev.etag, ev.size, ev.content_type)
            log("arrival_registered" if won else "arrival_duplicate", document_id=ev.document_id)
    except pyodbc.Error as e:
        log("arrival_sql_error", logging.ERROR, error_class=type(e).__name__)
        return ABANDON
    return COMPLETE


Handler = Callable[[Runtime, Received], str]
