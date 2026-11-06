"""In-memory Service Bus stand-in preserving the semantics the pipeline relies on: at-least-once delivery, visibility
lock with redelivery, delivery counting with dead-lettering, no ordering guarantee. It is a SIMULATION, not Azure."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from docdesk.adapters.broker import Received


class BrokerDown(Exception):
    pass


@dataclass
class _Msg:
    queue: str
    message_id: str
    body: str
    delivery_count: int = 0
    locked_until: float = 0.0
    token: str = ""


class InMemoryBroker:
    def __init__(self, max_delivery: int = 10, lock_s: float = 30.0):
        self.max_delivery = max_delivery
        self.lock_s = lock_s
        self._cv = threading.Condition()
        self._q: dict[str, list[_Msg]] = {}
        self.dlq: list[tuple[_Msg, str]] = []
        self.down = False                 # simulate a broker outage (send/receive/probe raise)
        self.send_failures = 0            # fail the next N sends
        self.duplicate_sends = False      # deliver every sent message twice (at-least-once duplicate)
        self.sent: list[tuple[str, str, str]] = []  # (queue, message_id, body) of every accepted send

    # -- producer -----------------------------------------------------------------------------
    def send(self, queue, body, message_id, subject=None):
        with self._cv:
            if self.down:
                raise BrokerDown()
            if self.send_failures > 0:
                self.send_failures -= 1
                raise BrokerDown()
            copies = 2 if self.duplicate_sends else 1
            for _ in range(copies):
                self._q.setdefault(queue, []).append(_Msg(queue, message_id, body))
            self.sent.append((queue, message_id, body))
            self._cv.notify_all()

    # -- consumer -----------------------------------------------------------------------------
    def receive(self, queue, wait_s=5.0):
        deadline = time.monotonic() + wait_s
        with self._cv:
            while True:
                if self.down:
                    raise BrokerDown()
                now = time.monotonic()
                for m in list(self._q.get(queue, [])):
                    if m.locked_until > now:
                        continue
                    if m.delivery_count >= self.max_delivery:
                        self._q[queue].remove(m)
                        self.dlq.append((m, "MaxDeliveryCountExceeded"))
                        continue
                    m.delivery_count += 1
                    m.locked_until = now + self.lock_s
                    m.token = uuid.uuid4().hex
                    return Received(queue, m.message_id, m.body, m.delivery_count, (m, m.token))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.2))

    def _owned(self, msg: Received) -> _Msg:
        m, token = msg.handle
        if m.token != token or m not in self._q.get(m.queue, []):
            raise RuntimeError("lock lost")
        return m

    def complete(self, msg):
        with self._cv:
            m = self._owned(msg)
            self._q[m.queue].remove(m)

    def abandon(self, msg):
        with self._cv:
            m = self._owned(msg)
            m.locked_until = 0.0
            m.token = ""
            self._cv.notify_all()

    def dead_letter(self, msg, reason):
        with self._cv:
            m = self._owned(msg)
            self._q[m.queue].remove(m)
            self.dlq.append((m, reason))

    def probe(self, queue):
        if self.down:
            raise BrokerDown()

    def close(self):
        pass

    # -- test helpers -------------------------------------------------------------------------
    def depth(self, queue: str) -> int:
        with self._cv:
            return len(self._q.get(queue, []))

    def total_depth(self) -> int:
        with self._cv:
            return sum(len(v) for v in self._q.values())

    def inject(self, queue: str, body: str, message_id: str | None = None) -> None:
        """Deliver a message that did not come from the outbox (e.g. a simulated Event Grid event)."""
        self.send(queue, body, message_id or uuid.uuid4().hex)

    def duplicate_pending(self, queue: str) -> None:
        with self._cv:
            for m in list(self._q.get(queue, [])):
                self._q[queue].append(_Msg(m.queue, m.message_id, m.body))
            self._cv.notify_all()
