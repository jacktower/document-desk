"""Synchronous driver and helpers for pipeline tests (no threads, fully deterministic)."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

from docdesk.domain.stages import BLOB_EVENTS_QUEUE, STAGE_QUEUES
from docdesk.pipeline import outbox
from docdesk.pipeline.runner import ABANDON, COMPLETE, handle_blob_event_message, handle_stage_message
from docdesk.pipeline.runtime import Runtime
from docdesk.web.uploads import save_upload
from docdesk_devtools.events import blob_created_event

QUEUE_HANDLERS = [(BLOB_EVENTS_QUEUE, handle_blob_event_message)] + [(q, handle_stage_message) for q in STAGE_QUEUES.values()]


def upload(rt: Runtime, data: bytes | Path, filename: str = "doc.pdf", *, emit_event: bool = True) -> str:
    """What the web does for an upload, plus a simulated Event Grid delivery."""
    raw = data.read_bytes() if isinstance(data, Path) else data
    res = save_upload(rt.db, rt.blob, rt.settings, filename, io.BytesIO(raw), len(raw))
    if emit_event:
        emit_blob_event(rt, res["document_id"], filename, len(raw))
    return res["document_id"]


def emit_blob_event(rt: Runtime, document_id: str, filename: str = "doc.pdf", size: int = 0) -> None:
    row = rt.db.query("SELECT blob_container, blob_name, content_type FROM dbo.documents WHERE document_id=?", document_id)[0]
    rt.broker.inject(BLOB_EVENTS_QUEUE, blob_created_event(row["blob_container"], row["blob_name"], size=size,
                                                            content_type=row["content_type"] or "application/pdf"))


def pump(rt: Runtime, max_rounds: int = 200, only: set[str] | None = None) -> int:
    """Dispatch outbox rows and handle every available message (optionally only from `only` queues) until quiet."""
    handled = 0
    for _ in range(max_rounds):
        progress = outbox.dispatch_once(rt)
        for queue, handler in QUEUE_HANDLERS:
            if only is not None and queue not in only:
                continue
            while (msg := rt.broker.receive(queue, wait_s=0)) is not None:
                decision = handler(rt, msg)
                if decision == COMPLETE:
                    rt.broker.complete(msg)
                elif decision == ABANDON:
                    rt.broker.abandon(msg)
                handled += 1
                progress += 1
        if progress == 0:
            break
    return handled


def status_of(db, document_id: str) -> str:
    return db.scalar("SELECT status FROM dbo.documents WHERE document_id=?", document_id)


def stage_rows(db, document_id: str) -> list[dict]:
    return db.query("SELECT a.attempt_no, s.stage, s.status, s.tries, s.last_error_code FROM dbo.stage_states s "
                    "JOIN dbo.processing_attempts a ON a.attempt_id=s.attempt_id WHERE s.document_id=? ORDER BY a.attempt_no, s.created_at",
                    document_id)


def counts(db, document_id: str) -> dict:
    q = lambda t: db.scalar(f"SELECT COUNT(*) FROM dbo.{t} WHERE document_id=?", document_id)
    return {t: q(t) for t in ("processing_attempts", "stage_states", "stage_tries", "artifacts", "document_results", "outbox")}


def wait_until(fn, timeout: float = 20.0, interval: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def fast_forward(db) -> None:
    """Make every scheduled retry due now (the SQL clock is the authority, so tests move the rows, not the clock)."""
    db.run(lambda cur: cur.execute("UPDATE dbo.stage_states SET next_run_at=DATEADD(SECOND,-1,SYSUTCDATETIME()) WHERE status='retry_wait'"))
    db.run(lambda cur: cur.execute("UPDATE dbo.outbox SET not_before=DATEADD(SECOND,-1,SYSUTCDATETIME()) WHERE dispatched_at IS NULL"))


def pump_until_quiet(rt, rounds: int = 30) -> None:
    for _ in range(rounds):
        fast_forward(rt.db)
        if pump(rt) == 0:
            break


def make_pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, invariant=1)
    y = 740
    for ln in lines:
        c.drawString(72, y, ln)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def invoice_pdf(tag: str = "A") -> bytes:
    return make_pdf(["INVOICE", f"Invoice No: INV-{tag}", "From: Test Vendor", "Total: 10.00"])


def tries(db, document_id: str, stage: str | None = None) -> list[dict]:
    sql = "SELECT a.attempt_no, t.stage, t.try_no, t.outcome, t.error_code FROM dbo.stage_tries t JOIN dbo.processing_attempts a ON a.attempt_id=t.attempt_id WHERE t.document_id=?"
    params = [document_id]
    if stage:
        sql += " AND t.stage=?"
        params.append(stage)
    return db.query(sql + " ORDER BY a.attempt_no, t.stage, t.try_no", *params)


def outbox_rows(db, document_id: str) -> list[dict]:
    return db.query("SELECT queue, stage, dispatched_at, dispatch_attempts FROM dbo.outbox WHERE document_id=? ORDER BY created_at", document_id)
