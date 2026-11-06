"""analyze (text/semantic analysis): one Content Understanding call; store the provider response byte for byte plus the text."""
from __future__ import annotations

import uuid

from ...adapters.blobstore import sha256_hex
from ...cu.analyzer_config import classifier_id, config_hash
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

    # what this request is actually made with; stored with the response so later stages never guess it from their own settings
    provenance = {"api_version": s.cu_api_version, "classifier_analyzer_id": classifier_id(s.analyzer_config_version),
                  "config_version": s.analyzer_config_version, "config_hash": config_hash(s)}
    try:
        with rt.cu_slots:  # bounded provider concurrency across this worker's threads
            outcome = rt.cu.analyze(provenance["classifier_analyzer_id"], data, mime, request_id, on_poll=keep_lease)
    except StageError as e:
        _keep_failure_response(rt, claim, e, provenance)
        raise

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
                             sha256=sha256_hex(outcome.raw), size=len(outcome.raw), content_type="application/json",
                             provenance=provenance)
        if text_bytes:
            repo.insert_artifact(cur, document_id=claim.document_id, attempt_id=claim.attempt_id, stage="analyze", try_no=claim.try_no,
                                 kind="extracted_text", container=s.artifacts_container, blob_name=text_name,
                                 sha256=sha256_hex(text_bytes), size=len(text_bytes), content_type="text/markdown; charset=utf-8")
    return effects


def _keep_failure_response(rt: Runtime, claim: repo.Claim, err: StageError, provenance: dict) -> None:
    """A final provider response that ended in failure (or could not be read) is evidence too. The received bytes are stored
    unchanged and the runner records the artifact row in the same transaction as the failure. If Blob Storage cannot take
    the write right now, the bytes travel with the failure into SQL (`pending_artifacts`) and the reaper writes them later:
    received evidence is never dropped, and no new provider call is needed."""
    if not err.raw_response:
        return
    name = artifact_path(claim.document_id, claim.attempt_no, claim.try_no, "provider_error.bin")
    desc = dict(kind="provider_error", container=rt.settings.artifacts_container, blob_name=name,
                sha256=sha256_hex(err.raw_response), size=len(err.raw_response),
                content_type="application/octet-stream", provenance=provenance)
    try:
        write_blob(rt, rt.settings.artifacts_container, name, err.raw_response, "application/octet-stream")
    except Exception:  # any write failure (not only StageError): the received bytes must not be dropped
        err.pending_artifacts.append({**desc, "content": err.raw_response})
        return
    err.artifacts.append(desc)
