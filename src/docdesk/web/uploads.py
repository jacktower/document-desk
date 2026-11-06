"""Upload intake: validate, create the SQL arrival record, then store the original. Event Grid takes it from there."""
from __future__ import annotations

import logging
import os
import uuid
from typing import BinaryIO

from ..adapters.blobstore import BlobNotFound, BlobStore
from ..config import Settings
from ..db import repo_pipeline as repo
from ..db.connection import Database
from ..domain.errors import UploadRejection
from ..domain.logs import log

EXT_MIME = {".pdf": "application/pdf", ".tif": "image/tiff", ".tiff": "image/tiff"}
TIFF_MAGICS = (b"II*\x00", b"MM\x00*")


def clean_filename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable())
    return base[:260] or "upload"


def validate_header(filename: str, head: bytes, size: int, max_bytes: int) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if ext not in EXT_MIME:
        raise UploadRejection("UNSUPPORTED_TYPE", "Only PDF and TIFF files are accepted (.pdf, .tif, .tiff).", 415)
    if size == 0:
        raise UploadRejection("EMPTY_FILE", "The file is empty.", 400)
    if size > max_bytes:
        raise UploadRejection("TOO_LARGE", f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.", 413)
    mime = EXT_MIME[ext]
    ok = (b"%PDF-" in head[:1024]) if mime == "application/pdf" else head[:4] in TIFF_MAGICS
    if not ok:
        raise UploadRejection("CORRUPT_HEADER", f"The file content does not look like a {ext[1:].upper()} file.", 400)
    return mime


def save_upload(db: Database, blob: BlobStore, settings: Settings, filename: str, fileobj: BinaryIO, size: int) -> dict:
    """Returns {"document_id", "status", "message"?}. Raises UploadRejection (no row created) or UploadFailed."""
    filename = clean_filename(filename)
    head = fileobj.read(1024)
    fileobj.seek(0)
    mime = validate_header(filename, head, size, settings.max_upload_bytes)
    ext = os.path.splitext(filename.lower())[1]
    ext = ".pdf" if mime == "application/pdf" else (".tif" if ext not in (".tif", ".tiff") else ext)
    document_id = str(uuid.uuid4())
    blob_name = f"{document_id}/source{ext}"
    container = settings.originals_container
    repo.create_upload_row(db, settings, document_id, filename, mime, size, container, blob_name)
    try:
        blob.put(container, blob_name, fileobj, mime, overwrite=False)
    except Exception as e:  # an exception is not proof that the blob was not stored
        stored: bool | None
        try:
            stored = blob.head(container, blob_name).size == size
        except BlobNotFound:
            stored = False
        except Exception:
            stored = None
        if stored:
            log("upload_ack_lost_blob_present", logging.WARNING, document_id=document_id)
            return {"document_id": document_id, "status": "uploading"}
        if stored is False:
            repo.mark_upload_failed(db, document_id, "UPLOAD_STORAGE_ERROR", "storing the original failed")
            raise UploadFailed(document_id, "Storing the file failed. Nothing was kept; please upload it again.", outcome="failed") from e
        raise UploadFailed(document_id, "Storing the file may or may not have succeeded; check the document list shortly.",
                           outcome="unknown") from e
    return {"document_id": document_id, "status": "uploading"}


class UploadFailed(Exception):
    def __init__(self, document_id: str, message: str, outcome: str):
        super().__init__(message)
        self.document_id, self.message, self.outcome = document_id, message, outcome
