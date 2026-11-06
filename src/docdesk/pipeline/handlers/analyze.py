"""analyze (text/semantic analysis): one Content Understanding call; store the provider response byte for byte plus the text."""
from __future__ import annotations

import uuid

from ...adapters.blobstore import sha256_hex
from ...cu.analyzer_config import classifier_id
from ...cu.normalize import extract_text, parse_operation
from ...db import repo_pipeline as repo
from ...domain.errors import LeaseLost, StageError
from ..runtime import Runtime
from .common import artifact_path, load_document, read_blob, write_blob


def run(rt: Runtime, claim: repo.Claim):
    s = rt.settings
    doc = load_document(rt, claim.document_id)
    data = read_blob(rt, doc["blob_container"], doc["blob_name"])
    mime = doc["content_type"] or ("application/pdf" if data[:5] == b"%PDF-" else "image/tiff")
    request_id = str(uuid.uuid4())
    repo.set_try_provider_request(rt.db, claim, request_id)

    def keep_lease():
        if not repo.renew_lease(rt.db, s, claim):
            raise LeaseLost()

    with rt.cu_slots:  # bounded provider concurrency across this worker's threads
        outcome = rt.cu.analyze(classifier_id(s.analyzer_config_version), data, mime, request_id, on_poll=keep_lease)

    text: str | None = None
    try:
        text = extract_text(parse_operation(outcome.raw))
    except StageError as e:
        if e.code != "PROVIDER_RESULT_UNUSABLE":
            raise  # stored as-is below; the index stage reports an unusable result, without another billable call

    raw_name = artifact_path(claim.document_id, claim.attempt_no, claim.try_no, "provider_response.json")
    write_blob(rt, s.artifacts_container, raw_name, outcome.raw, "application/json")
    text_bytes = text.encode("utf-8") if text else None
    text_name = artifact_path(claim.document_id, claim.attempt_no, claim.try_no, "extracted_text.md")
    if text_bytes:
        write_blob(rt, s.artifacts_container, text_name, text_bytes, "text/markdown; charset=utf-8")

    def effects(cur):
        repo.insert_artifact(cur, document_id=claim.document_id, attempt_id=claim.attempt_id, stage="analyze", try_no=claim.try_no,
                             kind="provider_response", container=s.artifacts_container, blob_name=raw_name,
                             sha256=sha256_hex(outcome.raw), size=len(outcome.raw), content_type="application/json")
        if text_bytes:
            repo.insert_artifact(cur, document_id=claim.document_id, attempt_id=claim.attempt_id, stage="analyze", try_no=claim.try_no,
                                 kind="extracted_text", container=s.artifacts_container, blob_name=text_name,
                                 sha256=sha256_hex(text_bytes), size=len(text_bytes), content_type="text/markdown; charset=utf-8")
    return effects
