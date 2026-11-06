"""Stage names, ordering and status vocabularies shared by the whole code base."""
from __future__ import annotations

STAGES: tuple[str, ...] = ("intake", "analyze", "index", "result")
STAGE_QUEUES: dict[str, str] = {s: f"stage-{s}" for s in STAGES}
BLOB_EVENTS_QUEUE = "blob-events"
ALL_QUEUES: tuple[str, ...] = (BLOB_EVENTS_QUEUE, *STAGE_QUEUES.values())

DEFAULT_MAX_TRIES: dict[str, int] = {"intake": 3, "analyze": 5, "index": 3, "result": 3}

DOC_TERMINAL = frozenset({"completed", "failed", "rejected", "upload_failed"})


def next_stage(stage: str) -> str | None:
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def stage_index(stage: str) -> int:
    return STAGES.index(stage)
