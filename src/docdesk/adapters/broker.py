"""Message broker port plus the Azure Service Bus adapter (managed identity).

Semantics the pipeline relies on (and an in-memory fake in devtools/ reproduces): at-least-once delivery, no ordering,
visibility lock, redelivery on abandon. Messages carry identifiers only."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings


@dataclass
class Received:
    queue: str
    message_id: str
    body: str
    delivery_count: int = 1
    handle: Any = field(default=None, repr=False)


class Broker(Protocol):
    def send(self, queue: str, body: str, message_id: str, subject: str | None = None) -> None: ...
    def receive(self, queue: str, wait_s: float = 5.0) -> Received | None: ...
    def complete(self, msg: Received) -> None: ...
    def abandon(self, msg: Received) -> None: ...
    def dead_letter(self, msg: Received, reason: str) -> None: ...
    def probe(self, queue: str) -> None: ...
    def close(self) -> None: ...


class AzureServiceBusBroker:
    """Senders are shared behind a lock; receivers are thread-local (the SDK receiver is not thread-safe)."""

    def __init__(self, settings: Settings, credential=None, client=None):
        from azure.servicebus import ServiceBusClient

        self._prefix = settings.queue_prefix
        if client is not None:
            self._client = client
        else:
            if credential is None:
                from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

                cid = settings.azure_client_id or None
                credential = (ManagedIdentityCredential(client_id=cid) if cid and settings.app_env == "prod"
                              else DefaultAzureCredential(managed_identity_client_id=cid))
            self._client = ServiceBusClient(settings.servicebus_namespace, credential)
        self._lock = threading.Lock()
        self._senders: dict[str, Any] = {}
        self._local = threading.local()

    def _name(self, queue: str) -> str:
        return f"{self._prefix}{queue}"

    def send(self, queue, body, message_id, subject=None):
        from azure.servicebus import ServiceBusMessage

        msg = ServiceBusMessage(body, message_id=message_id, subject=subject, content_type="application/json")
        with self._lock:
            sender = self._senders.get(queue)
            if sender is None:
                sender = self._senders[queue] = self._client.get_queue_sender(self._name(queue))
            sender.send_messages(msg)

    def _receiver(self, queue: str):
        recvs = getattr(self._local, "recvs", None)
        if recvs is None:
            recvs = self._local.recvs = {}
        r = recvs.get(queue)
        if r is None:
            r = recvs[queue] = self._client.get_queue_receiver(self._name(queue), max_wait_time=5)
        return r

    def receive(self, queue, wait_s=5.0):
        msgs = self._receiver(queue).receive_messages(max_message_count=1, max_wait_time=wait_s)
        if not msgs:
            return None
        m = msgs[0]
        body = b"".join(m.body) if not isinstance(m.body, (bytes, str)) else m.body
        return Received(queue, str(m.message_id), body.decode("utf-8") if isinstance(body, bytes) else body,
                        m.delivery_count or 1, m)

    def complete(self, msg):
        self._receiver(msg.queue).complete_message(msg.handle)

    def abandon(self, msg):
        self._receiver(msg.queue).abandon_message(msg.handle)

    def dead_letter(self, msg, reason):
        self._receiver(msg.queue).dead_letter_message(msg.handle, reason=reason[:100], error_description=reason[:500])

    def probe(self, queue):
        with self._client.get_queue_receiver(self._name(queue), max_wait_time=1) as r:
            r.peek_messages(max_message_count=1)

    def close(self):
        with self._lock:
            for s in self._senders.values():
                s.close()
            self._senders.clear()
        self._client.close()
