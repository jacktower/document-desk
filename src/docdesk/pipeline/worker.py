"""The worker process: queue consumers, outbox dispatcher, reaper and health loop, all as threads in one process.
Any number of replicas may run side by side; nothing here holds state that matters."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from ..domain.logs import log
from ..domain.stages import BLOB_EVENTS_QUEUE, STAGE_QUEUES
from . import health, outbox, reaper
from .runner import ABANDON, COMPLETE, handle_blob_event_message, handle_stage_message
from .runtime import Runtime


class Worker:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.started_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # -- loops ------------------------------------------------------------------------------------
    def _consume(self, queue: str, handler: Callable) -> None:
        rt = self.rt
        while not self.stop.is_set():
            try:
                msg = rt.broker.receive(queue, wait_s=2.0)
            except Exception as e:
                log("receive_failed", logging.WARNING, queue=queue, error_class=type(e).__name__)
                self.stop.wait(2.0)
                continue
            if msg is None:
                continue
            try:
                decision = handler(rt, msg)
            except Exception as e:  # never let one message kill the consumer thread
                log("handler_crashed", logging.ERROR, queue=queue, error_class=type(e).__name__)
                decision = ABANDON
            try:
                if decision == COMPLETE:
                    rt.broker.complete(msg)
                elif decision == ABANDON:
                    rt.broker.abandon(msg)
            except Exception as e:  # lock lost etc.: the message is redelivered and absorbed by the SQL guards
                log("settle_failed", logging.WARNING, queue=queue, error_class=type(e).__name__)

    def _periodic(self, name: str, interval: float, fn: Callable[[], object]) -> None:
        while not self.stop.is_set():
            try:
                fn()
            except Exception as e:
                log("loop_error", logging.WARNING, check=name, error_class=type(e).__name__)
            self.stop.wait(interval)

    def _dispatch_loop(self) -> None:
        s = self.rt.settings
        while not self.stop.is_set():
            try:
                n = outbox.dispatch_once(self.rt)
            except Exception as e:
                log("loop_error", logging.WARNING, check="dispatcher", error_class=type(e).__name__)
                n = 0
            if n == 0:
                self.stop.wait(s.dispatch_poll_s)

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self) -> None:
        s = self.rt.settings
        plan: list[tuple[str, Callable, int]] = [(BLOB_EVENTS_QUEUE, handle_blob_event_message, s.conc("arrival"))]
        plan += [(STAGE_QUEUES[st], handle_stage_message, s.conc(st)) for st in ("intake", "analyze", "index", "result")]
        for queue, handler, n in plan:
            for i in range(max(1, n)):
                self._spawn(f"consume-{queue}-{i}", self._consume, queue, handler)
        self._spawn("dispatcher", self._dispatch_loop)
        self._spawn("reaper", self._periodic, "reaper", s.reaper_poll_s, lambda: reaper.reap_once(self.rt))
        self._spawn("heartbeat", self._periodic, "heartbeat", 15.0, lambda: health.heartbeat(self.rt, self.started_at))
        self._spawn("probes", self._periodic, "probes", s.health_poll_s, lambda: health.probe_all(self.rt))
        log("worker_started", detail_code="ok", worker_id=self.rt.worker_id, count=len(self.threads))

    def _spawn(self, name: str, fn: Callable, *args) -> None:
        t = threading.Thread(target=fn, args=args, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    def shutdown(self, timeout: float = 15.0) -> None:
        self.stop.set()
        deadline = time.monotonic() + timeout
        for t in self.threads:
            t.join(max(0.1, deadline - time.monotonic()))
        log("worker_stopped", worker_id=self.rt.worker_id)
