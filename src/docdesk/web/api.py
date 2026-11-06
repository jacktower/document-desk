"""JSON API. Every route here sits on the authenticated router."""
from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from ..adapters.blobstore import BlobNotFound, BlobUnavailable
from ..db import repo_pipeline as repo
from ..db import repo_read
from ..domain.errors import UploadRejection
from ..domain.logs import log
from ..domain.stages import STAGES
from .uploads import UploadFailed, save_upload

PREVIEWABLE = {"application/pdf"}


def _content_disposition(kind: str, filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def build_api_router(ctx) -> APIRouter:
    r = APIRouter(prefix="/api")
    s, db, blob = ctx.settings, ctx.db, ctx.blob

    @r.get("/documents")
    def list_documents(status: str | None = None, category: str | None = None, q: str | None = None, limit: int = 50,
                       before: str | None = None):
        return repo_read.list_documents(db, status=status, category=category, q=q, limit=limit, before=before)

    @r.post("/documents", status_code=202)
    def upload(request: Request, file: UploadFile):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > s.max_upload_bytes + 1_000_000:
            raise HTTPException(413, {"code": "TOO_LARGE", "message": f"The file is larger than the {s.max_upload_mb} MB limit."})
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
        try:
            res = save_upload(db, blob, s, file.filename or "upload", file.file, size)
        except UploadRejection as e:
            raise HTTPException(e.status, {"code": e.code, "message": e.message}) from None
        except UploadFailed as e:
            raise HTTPException(502, {"code": "UPLOAD_FAILED" if e.outcome == "failed" else "UPLOAD_OUTCOME_UNKNOWN",
                                      "message": e.message, "document_id": e.document_id}) from None
        log("document_uploaded", document_id=res["document_id"])
        return res

    @r.get("/documents/{document_id}")
    def detail(document_id: str):
        doc = repo_read.get_document(db, _uuid(document_id))
        if doc is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "document not found"})
        return doc

    @r.get("/documents/{document_id}/original")
    def original(document_id: str, download: int = 0):
        ref = repo_read.get_original_ref(db, _uuid(document_id))
        if ref is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "document not found"})
        mime = ref["content_type"] or "application/octet-stream"
        inline = mime in PREVIEWABLE and not download
        return _stream(blob, ref["blob_container"], ref["blob_name"], mime, _content_disposition("inline" if inline else "attachment", ref["original_filename"]))

    @r.get("/documents/{document_id}/artifacts/{artifact_id}")
    def artifact(document_id: str, artifact_id: str):
        a = repo_read.get_artifact(db, _uuid(document_id), _uuid(artifact_id))
        if a is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "artifact not found"})
        ext = {"provider_response": "provider_response.json", "extracted_text": "extracted_text.md", "normalized_result": "normalized_result.json"}.get(a["kind"], "artifact")
        return _stream(blob, a["container"], a["blob_name"], a["content_type"] or "application/octet-stream",
                       _content_disposition("attachment", f"{str(a['document_id']).lower()}-a{a['try_no'] or 0}-{ext}"))

    @r.post("/documents/{document_id}/replay", status_code=202)
    def replay(document_id: str, body: dict | None = None):
        did = _uuid(document_id)
        body = body or {}
        from_stage = body.get("from_stage")
        if from_stage is not None and from_stage not in STAGES:
            raise HTTPException(422, {"code": "BAD_STAGE", "message": f"from_stage must be one of {', '.join(STAGES)}"})
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise HTTPException(422, {"code": "BAD_REASON", "message": "reason must be text"})
        rows = db.query("SELECT status, current_attempt_no FROM dbo.documents WHERE document_id=?", did)
        if not rows:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "document not found"})
        if rows[0]["status"] != "failed":
            raise HTTPException(409, {"code": "NOT_REPLAYABLE", "message": "Only failed documents can be replayed."})
        seen = body.get("attempt_no") if isinstance(body.get("attempt_no"), int) else rows[0]["current_attempt_no"]
        res = repo.replay_document(db, did, seen, from_stage, reason)
        if res["outcome"] == "CONFLICT":
            raise HTTPException(409, {"code": "REPLAY_CONFLICT", "message": "The document changed or is already being replayed."})
        if res["outcome"] == "BAD_STAGE":
            raise HTTPException(422, {"code": "BAD_STAGE", "message": "Replay cannot start after the stage that failed."})
        log("document_replayed", document_id=did, attempt_no=res["attempt_no"])
        return {"document_id": did, "attempt_no": res["attempt_no"], "attempt_id": str(res["attempt_id"]).lower()}

    @r.get("/diagnostics")
    def diagnostics():
        return collect_diagnostics(ctx)

    return r


def collect_diagnostics(ctx) -> dict:
    out: dict = {"web_checks": {}}
    t0 = time.monotonic()
    try:
        ctx.db.scalar("SELECT 1")
        out["web_checks"]["sql"] = {"status": "ok", "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        out["web_checks"]["sql"] = {"status": "error", "error_class": type(e).__name__}
        return out
    try:
        ctx.blob.exists(ctx.settings.artifacts_container, "_probe")
        out["web_checks"]["blob"] = {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        out["web_checks"]["blob"] = {"status": "error", "error_class": type(e).__name__}
    out.update(repo_read.diagnostics(ctx.db))
    return out


def _uuid(v: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(v)).lower()
    except ValueError:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "not found"}) from None


def _stream(blob, container: str, name: str, mime: str, disposition: str) -> StreamingResponse:
    try:
        it = blob.stream(container, name)
        first = next(it, b"")
    except BlobNotFound:
        raise HTTPException(404, {"code": "BLOB_MISSING", "message": "the stored file is missing"}) from None
    except BlobUnavailable:
        raise HTTPException(503, {"code": "STORAGE_UNAVAILABLE", "message": "storage is temporarily unavailable"}) from None

    def gen():
        yield first
        yield from it

    return StreamingResponse(gen(), media_type=mime, headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff",
                                                                 "Cache-Control": "private, no-store"})
