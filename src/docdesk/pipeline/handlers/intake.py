"""intake: verify the stored original really is a supported, parseable, in-limits PDF/TIFF and record its facts."""
from __future__ import annotations

import io

from ...adapters.blobstore import BlobNotFound, BlobUnavailable, sha256_hex
from ...db import repo_pipeline as repo
from ...domain.errors import StageError
from ..runtime import Runtime
from .common import load_document, read_blob

PDF_MAGIC = b"%PDF-"
TIFF_MAGICS = (b"II*\x00", b"MM\x00*")


def sniff(data: bytes) -> str | None:
    if PDF_MAGIC in data[:1024]:
        return "application/pdf"
    if data[:4] in TIFF_MAGICS:
        return "image/tiff"
    return None


def count_pages(data: bytes, mime: str) -> int:
    if mime == "application/pdf":
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise StageError("INPUT_UNSUPPORTED", "encrypted PDF is not supported")
            return len(reader.pages)
        except StageError:
            raise
        except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError):
            raise StageError("INPUT_CORRUPT", "PDF could not be parsed") from None
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            return int(getattr(im, "n_frames", 1))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise StageError("INPUT_CORRUPT", "TIFF could not be parsed") from None


def run(rt: Runtime, claim: repo.Claim):
    s = rt.settings
    doc = load_document(rt, claim.document_id)
    try:
        info = rt.blob.head(doc["blob_container"], doc["blob_name"])
    except BlobNotFound:
        raise StageError("STORAGE_MISSING", "original blob not found") from None
    except BlobUnavailable as e:
        raise StageError("STORAGE_TRANSIENT", f"storage unavailable ({e})") from None
    if info.size == 0:
        raise StageError("INPUT_CORRUPT", "empty file")
    if info.size > s.max_upload_bytes:
        raise StageError("INPUT_TOO_LARGE", f"file is {info.size} bytes; the limit is {s.max_upload_bytes}")
    data = read_blob(rt, doc["blob_container"], doc["blob_name"])
    mime = sniff(data)
    if mime is None:
        raise StageError("INPUT_UNSUPPORTED", "content is neither a PDF nor a TIFF")
    pages = count_pages(data, mime)
    if pages < 1:
        raise StageError("INPUT_CORRUPT", "no pages")
    if pages > s.max_pages:
        raise StageError("INPUT_TOO_MANY_PAGES", f"{pages} pages; the limit is {s.max_pages}")
    digest = sha256_hex(data)

    def effects(cur):
        cur.execute("UPDATE dbo.documents SET sha256=?, size_bytes=?, content_type=?, page_count=?, blob_etag=COALESCE(?, blob_etag), "
                    "updated_at=SYSUTCDATETIME() WHERE document_id=?", digest, len(data), mime, pages, info.etag, claim.document_id)
        cur.execute("SELECT COUNT(*) FROM dbo.artifacts WHERE document_id=? AND kind='original'", claim.document_id)
        if cur.fetchone()[0] == 0:
            repo.insert_artifact(cur, document_id=claim.document_id, attempt_id=None, stage=None, try_no=None, kind="original",
                                 container=doc["blob_container"], blob_name=doc["blob_name"], sha256=digest, size=len(data),
                                 content_type=mime)
    return effects
