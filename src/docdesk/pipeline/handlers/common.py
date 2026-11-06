from __future__ import annotations

import json

from ...adapters.blobstore import BlobNotFound, BlobUnavailable
from ...domain.errors import StageError
from ..runtime import Runtime


def load_document(rt: Runtime, document_id: str) -> dict:
    rows = rt.db.query("SELECT * FROM dbo.documents WHERE document_id=?", document_id)
    if not rows:
        raise StageError("INTERNAL_ERROR", "document row not found", retryable=False)
    return rows[0]


def read_blob(rt: Runtime, container: str, name: str, *, missing_code: str = "STORAGE_MISSING") -> bytes:
    try:
        return rt.blob.get(container, name)
    except BlobNotFound:
        raise StageError(missing_code, "blob not found") from None
    except BlobUnavailable as e:
        raise StageError("STORAGE_TRANSIENT", f"storage unavailable ({e})") from None


def write_blob(rt: Runtime, container: str, name: str, data: bytes, content_type: str) -> None:
    try:
        rt.blob.put(container, name, data, content_type, overwrite=True)
    except BlobUnavailable as e:
        raise StageError("STORAGE_TRANSIENT", f"storage unavailable ({e})") from None


def artifact_path(document_id: str, attempt_no: int, try_no: int, filename: str) -> str:
    return f"{document_id}/a{attempt_no}/try{try_no}/{filename}"


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
