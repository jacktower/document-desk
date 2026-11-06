"""Server-rendered pages (Jinja2, autoescaped). Values come from SQL; text comes from the artifact SQL points at."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..adapters.blobstore import BlobNotFound, BlobUnavailable
from ..cu import registry as R
from ..db import repo_read
from .api import _uuid, collect_diagnostics

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEXT_PREVIEW_CHARS = 20_000

TEMPLATES.env.globals.update(category_label=R.category_label, field_label=R.field_label)
TEMPLATES.env.filters["fmtsize"] = lambda n: "-" if n is None else (f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB")
TEMPLATES.env.filters["tojson_pretty"] = lambda v: __import__("json").dumps(v, indent=2, ensure_ascii=False, default=str)
TEMPLATES.env.filters["short_ts"] = lambda v: (v.replace("T", " ").replace("Z", "") if isinstance(v, str) else "-")

STATUS_LABEL = {"uploading": "Uploading", "processing": "Processing", "completed": "Completed", "failed": "Failed",
                "rejected": "Rejected", "upload_failed": "Upload failed"}
TEMPLATES.env.globals["status_label"] = lambda s: STATUS_LABEL.get(s, s)
CLASS_LABEL = {"classified": "Classified", "other": "Other / unknown", "unrecognized_category": "Unrecognized category",
               "not_classified": "Not classified"}
TEMPLATES.env.globals["class_label"] = lambda s: CLASS_LABEL.get(s, s)


def render(name: str, request: Request, ctx, **kw) -> str:
    principal = getattr(request.state, "principal", None)
    csrf = ctx.auth.csrf_for(principal.nonce) if principal is not None and principal.kind == "cookie" else ""
    return TEMPLATES.get_template(name).render(request=request, csrf=csrf, signed_in=principal is not None, **kw)


def build_ui_router(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/", response_class=HTMLResponse)
    def index(request: Request):
        data = repo_read.list_documents(ctx.db, limit=50)
        return render("index.html", request, ctx, documents=data["items"], max_mb=ctx.settings.max_upload_mb)

    @r.get("/documents/{document_id}", response_class=HTMLResponse)
    def document(request: Request, document_id: str):
        doc = repo_read.get_document(ctx.db, _uuid(document_id))
        if doc is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Document not found."})
        text, text_note, text_artifact = None, None, None
        res = doc["result"]
        if res:
            text_artifact = next((a for a in doc["artifacts"] if a["kind"] == "extracted_text" and _artifact_in_chain(doc, a)), None)
            if text_artifact:
                row = repo_read.get_artifact(ctx.db, doc["document_id"], text_artifact["artifact_id"])
                try:
                    raw = ctx.blob.get(row["container"], row["blob_name"]).decode("utf-8", "replace")
                    text = raw[:TEXT_PREVIEW_CHARS]
                    if len(raw) > TEXT_PREVIEW_CHARS:
                        text_note = f"Showing the first {TEXT_PREVIEW_CHARS:,} of {len(raw):,} characters. Download the artifact for the full text."
                except (BlobNotFound, BlobUnavailable):
                    text_note = "The extracted-text artifact could not be read from storage right now."
        artifacts = [a for a in doc["artifacts"]]
        return render("detail.html", request, ctx, d=doc, text=text, text_note=text_note, artifacts=artifacts,
                      stages=("intake", "analyze", "index", "result"))

    @r.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        return render("status.html", request, ctx, diag=collect_diagnostics(ctx))

    return r


def _artifact_in_chain(doc: dict, artifact: dict) -> bool:
    """The text shown belongs to the attempt that produced the current result (own try, or carried from an earlier attempt)."""
    res = doc["result"]
    cur = next((a for a in doc["attempts"] if a["attempt_id"] == res["attempt_id"]), None)
    if cur is None:
        return False
    analyze = next((s for s in cur["stages"] if s["stage"] == "analyze"), None)
    producer = analyze["carried_from_attempt_id"] if analyze and analyze["status"] == "carried" else cur["attempt_id"]
    return artifact["attempt_id"] == producer
