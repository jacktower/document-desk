"""REQ-002 / REQ-012 / REQ-018: real worker threads, several workers, one stuck document; logs stay free of content."""
import io
import json
import time

import pytest

from docdesk.domain.logs import setup_logging
from docdesk.pipeline.runtime import Runtime
from docdesk.pipeline.worker import Worker
from helpers import counts, invoice_pdf, make_pdf, pump, status_of, tries, upload, wait_until


def start_workers(rt, n):
    workers = []
    for i in range(n):
        r = Runtime(settings=rt.settings, db=rt.db, blob=rt.blob, broker=rt.broker, cu=rt.cu, worker_id=f"w{i}")
        w = Worker(r)
        w.start()
        workers.append(w)
    return workers


def stop(workers):
    for w in workers:
        w.stop.set()
    for w in workers:
        w.shutdown(timeout=20)


@pytest.mark.parametrize("duplicate_delivery", [False, True])
def test_many_documents_three_workers_one_stuck_provider_failure(rt, sim, duplicate_delivery):
    """A throttled document must not delay or damage any other, whatever the delivery duplication."""
    sim.rules = [lambda text: "throttle" if "STUCK-MARKER" in text else None]
    rt.broker.duplicate_sends = duplicate_delivery
    stuck = upload(rt, make_pdf(["INVOICE", "Invoice No: STUCK-MARKER", "Total: 1.00"]), "stuck.pdf")
    good = [upload(rt, invoice_pdf(str(i)), f"{i}.pdf") for i in range(12)]
    workers = start_workers(rt, 3)
    try:
        wait_until(lambda: all(status_of(rt.db, d) == "completed" for d in good), timeout=60)
        # the stuck document is still being retried or waiting; it never completed and never blocked the rest
        assert status_of(rt.db, stuck) in ("processing", "failed")
        for d in good:
            c = counts(rt.db, d)
            assert c["document_results"] == 1 and c["processing_attempts"] == 1 and c["artifacts"] == 4
            assert [t["try_no"] for t in tries(rt.db, d, "analyze")] == [1]
            assert rt.db.scalar("SELECT COUNT(*) FROM dbo.outbox WHERE document_id=? AND stage='index'", d) == 1
        assert rt.db.scalar("SELECT COUNT(*) FROM dbo.worker_heartbeats") == 3
        wait_until(lambda: rt.db.scalar("SELECT COUNT(*) FROM dbo.dependency_health") == 4, timeout=15)
    finally:
        stop(workers)
    # every worker's threads exited
    assert all(not t.is_alive() for w in workers for t in w.threads)


def test_health_probes_report_dependency_failures_without_secrets(rt, sim):
    rt.broker.down = True
    workers = start_workers(rt, 1)
    try:
        wait_until(lambda: rt.db.scalar("SELECT COUNT(*) FROM dbo.dependency_health WHERE name='servicebus' AND status='error'") == 1, timeout=15)
    finally:
        stop(workers)
    row = rt.db.query("SELECT * FROM dbo.dependency_health WHERE name='servicebus'")[0]
    assert row["error_class"] == "BrokerDown"


def test_logs_contain_no_filenames_text_or_field_values(rt, sim):
    buf = io.StringIO()
    setup_logging("DEBUG", buf)
    sim.script = ["unavailable"]                                      # exercise the failure/retry log paths as well
    doc = upload(rt, make_pdf(["INVOICE", "Invoice No: SECRETREF-4471", "From: SECRETVENDOR Ltd", "Total: 987.65"]), "SECRETNAME-7731.pdf")
    from helpers import pump_until_quiet
    pump_until_quiet(rt)
    assert status_of(rt.db, doc) == "completed"
    logs = buf.getvalue()
    assert logs, "expected operational log lines"
    for secret in ("SECRETNAME", "SECRETREF", "SECRETVENDOR", "987.65", "test-operator-password"):
        assert secret not in logs, secret
    lines = [json.loads(l) for l in logs.strip().splitlines()]
    assert {"stage_started", "stage_succeeded", "stage_retry"} <= {l["event"] for l in lines}
    allowed = {"ts", "level", "event", "document_id", "attempt_no", "stage", "try_no", "worker_id", "error_code", "http_status", "duration_ms",
               "provider_request_id", "queue", "outbox_id", "count", "status", "check", "error_class", "detail_code"}
    assert all(set(l) <= allowed for l in lines)
