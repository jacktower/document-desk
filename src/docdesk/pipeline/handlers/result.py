"""result (result availability): verify the result row and its artifacts exist, then make the document `completed`."""
from __future__ import annotations

from ...adapters.blobstore import BlobNotFound, BlobUnavailable
from ...db import repo_pipeline as repo
from ...domain.errors import StageError
from ..runtime import Runtime


def run(rt: Runtime, claim: repo.Claim):
    # The result belongs to the attempt that ran `index`: this one, or (after a replay from `result`) the attempt whose index
    # stage was carried forward. Earlier attempts' rows are referenced, never copied or changed.
    rows = rt.db.query(
        "SELECT result_id, raw_artifact_id, text_artifact_id, normalized_artifact_id FROM dbo.document_results WHERE attempt_id = "
        "COALESCE((SELECT carried_from_attempt_id FROM dbo.stage_states WHERE attempt_id=? AND stage='index' AND status='carried'), ?)",
        claim.attempt_id, claim.attempt_id)
    if not rows:
        raise StageError("ARTIFACT_MISSING", "no result row for this attempt", retryable=False)
    r = rows[0]
    for key in ("raw_artifact_id", "text_artifact_id", "normalized_artifact_id"):
        if r[key] is None:
            continue
        art = rt.db.query("SELECT container, blob_name, size_bytes FROM dbo.artifacts WHERE artifact_id=?", r[key])
        if not art:
            raise StageError("ARTIFACT_MISSING", f"{key} is not recorded", retryable=False)
        try:
            info = rt.blob.head(art[0]["container"], art[0]["blob_name"])
        except BlobNotFound:
            raise StageError("ARTIFACT_MISSING", f"{key} blob is missing", retryable=False) from None
        except BlobUnavailable as e:
            raise StageError("STORAGE_TRANSIENT", f"storage unavailable ({e})") from None
        if art[0]["size_bytes"] is not None and info.size != art[0]["size_bytes"]:
            raise StageError("ARTIFACT_MISSING", f"{key} blob size differs from the recorded size", retryable=False)

    def effects(cur):
        cur.execute("UPDATE dbo.documents SET status='completed', current_result_id=?, last_error_code=NULL, last_error_message=NULL, "
                    "updated_at=SYSUTCDATETIME() WHERE document_id=? AND status='processing' AND current_attempt_no=?",
                    r["result_id"], claim.document_id, claim.attempt_no)
        cur.execute("UPDATE dbo.processing_attempts SET status='succeeded', completed_at=SYSUTCDATETIME() WHERE attempt_id=? AND status='active'",
                    claim.attempt_id)
    return effects
