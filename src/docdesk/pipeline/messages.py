"""Parsing of broker message bodies. Stage messages carry identifiers only; blob events come from Event Grid."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from ..domain.stages import STAGES

_BLOB_SUBJECT = re.compile(r"/containers/(?P<container>[^/]+)/blobs/(?P<name>.+)$")
_DOC_PREFIX = re.compile(r"^(?P<doc>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/")


class BadMessage(Exception):
    pass


@dataclass(frozen=True)
class StageMessage:
    document_id: str
    attempt_id: str
    stage: str


def _uuid(v) -> str:
    try:
        return str(uuid.UUID(str(v))).lower()
    except ValueError:
        raise BadMessage("bad identifier") from None


def parse_stage_message(body: str) -> StageMessage:
    try:
        d = json.loads(body)
    except ValueError:
        raise BadMessage("not JSON") from None
    if not isinstance(d, dict) or d.get("v") != 1 or d.get("stage") not in STAGES:
        raise BadMessage("unrecognised stage message")
    return StageMessage(_uuid(d.get("document_id")), _uuid(d.get("attempt_id")), d["stage"])


@dataclass(frozen=True)
class BlobEvent:
    container: str
    blob_name: str
    document_id: str
    etag: str | None
    size: int | None
    content_type: str | None


def parse_blob_events(body: str) -> tuple[list[BlobEvent], int]:
    """Returns (created-events for document blobs, count of ignored events). Accepts Event Grid or CloudEvents schema,
    as a single object or an array. Raises BadMessage for a body that is not JSON."""
    try:
        data = json.loads(body)
    except ValueError:
        raise BadMessage("not JSON") from None
    items = data if isinstance(data, list) else [data]
    out: list[BlobEvent] = []
    ignored = 0
    for ev in items:
        if not isinstance(ev, dict):
            ignored += 1
            continue
        etype = ev.get("eventType") or ev.get("type") or ""
        subj = ev.get("subject") or ""
        m = _BLOB_SUBJECT.search(subj)
        if not etype.endswith("BlobCreated") or not m:
            ignored += 1
            continue
        d = _DOC_PREFIX.match(m["name"])
        if not d:
            ignored += 1
            continue
        payload = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        etag = payload.get("eTag") or payload.get("etag")
        size = payload.get("contentLength")
        out.append(BlobEvent(m["container"], m["name"], str(uuid.UUID(d["doc"])).lower(),
                             str(etag).strip('"') if etag else None, size if isinstance(size, int) else None,
                             payload.get("contentType")))
    return out, ignored
