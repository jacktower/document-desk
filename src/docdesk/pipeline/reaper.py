"""Recovery of work SQL says is runnable but nothing is running: expired leases, lost events, stranded uploads."""
from __future__ import annotations

import logging

from ..adapters.blobstore import BlobNotFound, BlobUnavailable
from ..db import repo_pipeline as repo
from ..domain.logs import log
from .runtime import Runtime


def reap_uploads(rt: Runtime) -> int:
    s = rt.settings
    n = 0
    for d in repo.stale_uploads(rt.db, s.arrival_grace_s):
        did = str(d["document_id"]).lower()
        try:
            info = rt.blob.head(d["blob_container"], d["blob_name"])
        except BlobNotFound:
            if d["age_s"] >= s.upload_timeout_s and repo.mark_upload_failed(
                    rt.db, did, "UPLOAD_TIMEOUT", "the original never arrived in storage"):
                log("upload_failed", logging.WARNING, document_id=did, error_code="UPLOAD_TIMEOUT")
                n += 1
            continue
        except BlobUnavailable:
            continue
        if repo.register_arrival(rt.db, s, did, d["blob_container"], d["blob_name"], info.etag, info.size, info.content_type):
            log("arrival_recovered", logging.WARNING, document_id=did)
            n += 1
    return n


def persist_pending_artifacts(rt: Runtime) -> int:
    """Write provider evidence that could not be stored at failure time (held in SQL) to Blob and record its artifact row."""
    n = 0
    for p in repo.open_pending_artifacts(rt.db):
        try:
            rt.blob.put(p["container"], p["blob_name"], bytes(p["content"]), p["content_type"] or "application/octet-stream", overwrite=True)
        except Exception as e:  # storage still unavailable: the bytes stay in SQL, try again next sweep
            repo.note_pending_attempt(rt.db, p["pending_id"])
            log("pending_artifact_write_failed", logging.WARNING, document_id=str(p["document_id"]).lower(), error_class=type(e).__name__)
            continue
        if repo.finish_pending_artifact(rt.db, p):
            n += 1
            log("pending_artifact_persisted", document_id=str(p["document_id"]).lower())
    return n


def reap_once(rt: Runtime) -> dict[str, int]:
    return {
        "leases": repo.reap_expired_leases(rt.db),
        "lost_events": repo.reap_lost_stage_events(rt.db, rt.settings.stage_recovery_s),
        "uploads": reap_uploads(rt),
        "pending_artifacts": persist_pending_artifacts(rt),
    }
