"""Everything a worker needs, injected once. Tests and the local dev stack pass fakes behind the same Protocols."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from ..adapters.blobstore import BlobStore
from ..adapters.broker import Broker
from ..config import Settings
from ..cu.client import CuClient
from ..db.connection import Database


@dataclass
class Runtime:
    settings: Settings
    db: Database
    blob: BlobStore
    broker: Broker
    cu: CuClient
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    cu_slots: threading.BoundedSemaphore = field(init=False)

    def __post_init__(self) -> None:
        self.cu_slots = threading.BoundedSemaphore(max(1, self.settings.cu_max_concurrency))
