"""index (classification and metadata indexing): normalize the stored response and write the indexed result. Offline: no provider call."""
from __future__ import annotations

import json

from ...adapters.blobstore import sha256_hex
from ...cu.normalize import normalize
from ...db import repo_pipeline as repo
from ...domain.errors import StageError
from ..runtime import Runtime
from .common import artifact_path, dumps, load_document, read_blob, write_blob


def run(rt: Runtime, claim: repo.Claim):
    s = rt.settings
    doc = load_document(rt, claim.document_id)
    raw_art = repo.resolve_artifact(rt.db, claim.attempt_id, "provider_response")
    if raw_art is None:
        raise StageError("ARTIFACT_MISSING", "provider response artifact is not recorded", retryable=False)
    raw = read_blob(rt, raw_art["container"], raw_art["blob_name"], missing_code="ARTIFACT_MISSING")
    if raw_art["sha256"] and sha256_hex(raw) != raw_art["sha256"].lower():
        raise StageError("ARTIFACT_MISSING", "provider response artifact hash mismatch", retryable=False)
    text_art = repo.resolve_artifact(rt.db, claim.attempt_id, "extracted_text")
    result_id = repo.new_id()

    n = normalize(raw, document_id=claim.document_id, attempt_no=claim.attempt_no, settings=s,
                  intake_page_count=doc.get("page_count"),
                  raw_artifact_id=str(raw_art["artifact_id"]).lower(),
                  text_artifact_id=str(text_art["artifact_id"]).lower() if text_art else None)
    n.document["result_id"] = result_id
    body = dumps(n.document)
    name = artifact_path(claim.document_id, claim.attempt_no, claim.try_no, "normalized_result.json")
    write_blob(rt, s.artifacts_container, name, body, "application/json")
    def effects(cur):
        norm_id = repo.insert_artifact(cur, document_id=claim.document_id, attempt_id=claim.attempt_id, stage="index", try_no=claim.try_no,
                                       kind="normalized_result", container=s.artifacts_container, blob_name=name,
                                       sha256=sha256_hex(body), size=len(body), content_type="application/json")
        p = n.document["provider"]
        cur.execute(
            "INSERT dbo.document_results (result_id, document_id, attempt_id, result_schema_version, category, classification_status, "
            "category_provider_raw, provider_name, api_version, analyzer_id, extractor_analyzer_id, analyzer_config_version, "
            "analyzer_config_hash, provider_operation_id, simulated, raw_artifact_id, text_artifact_id, normalized_artifact_id, "
            "page_count, text_char_count, needs_review, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            result_id, claim.document_id, claim.attempt_id, n.document["schema_version"], n.category, n.classification_status,
            (n.provider_category_raw or None) and n.provider_category_raw[:200], p["name"], p["api_version"], p["classifier_analyzer_id"],
            p["extractor_analyzer_id"], p["config_version"], p["config_hash"], p["operation_id"], 1 if n.simulated else 0,
            raw_art["artifact_id"], text_art["artifact_id"] if text_art else None, norm_id, n.page_count,
            len(n.text) if n.text else 0, 1 if n.needs_review else 0, json.dumps(n.flags, ensure_ascii=False))
        for f in n.fields:
            cur.execute(
                "INSERT dbo.document_fields (result_id, field_name, value_type, status, value_text, value_string, value_string_truncated, "
                "value_number, value_date, value_bool, value_json, confidence, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                result_id, f.name, f.value_type, f.status, f.value_text, f.value_string, 1 if f.value_string_truncated else 0,
                f.value_number, f.value_date, f.value_bool, f.value_json, f.confidence, f.source)
        cur.execute("UPDATE dbo.documents SET category=?, current_result_id=?, needs_review=?, updated_at=SYSUTCDATETIME() "
                    "WHERE document_id=? AND status='processing' AND current_attempt_no=?",
                    n.category, result_id, 1 if n.needs_review else 0, claim.document_id, claim.attempt_no)
    return effects
