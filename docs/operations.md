# Operations guide

## Where to look

| Question | Where |
|---|---|
| Is the system healthy? | Web UI **System status** (`/status`) or `GET /api/diagnostics`. Shows SQL and Blob checks made by the web app, the worker-reported state of Service Bus, Blob, the Content Understanding endpoint, model defaults and classifier-analyzer readiness (written by the worker every minute), worker heartbeats, document counts by status, failed stages in the last 24 h by error code, the age of the oldest unpublished outbox row and of the oldest waiting stage, and expired leases. |
| Why did this document fail? | The document page: banner with the error code and message, the stage timeline for every attempt, and the per-try history (`outcome`, `error_code`, provider request id). The same data is in SQL (`stage_states`, `stage_tries`). |
| Container logs | JSON lines on stdout with only these keys: `ts, level, event, document_id, attempt_no, stage, try_no, worker_id, error_code, http_status, duration_ms, provider_request_id, queue, outbox_id, count, status, check, error_class, detail_code`. Filenames, text, field values, passwords, tokens and provider response bodies are never logged (enforced by an allow-list filter and a test). Exceptions are logged by class name only. |

A status page with **no workers** or **stale dependency rows** means no worker replica is running (the probes live in the worker).
`oldest_undispatched_outbox_s` growing means events are committed in SQL but not reaching Service Bus (broker outage or missing
Sender role on the worker identity); they are delivered automatically when the broker is back.

## Error codes

| Code | Meaning | Operator action |
|---|---|---|
| `PROVIDER_THROTTLED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_TIMEOUT`, `PROVIDER_RESPONSE_MALFORMED` | Transient provider problems; retried with back-off (`min(300 s, 5 s * 2^(try-1))` +/-20 %, Retry-After honoured) up to the stage's `MAX_TRIES`, then the document is `failed` | If exhausted: fix/wait, then replay |
| `PROVIDER_CONFIGURATION` | 401/403/404 or `DefaultDeploymentModelNotFound`/`DefaultsNotSet`: role, endpoint, analyzer or model-default problem | Fix per [deployment.md](deployment.md) (`docdesk setup-analyzers --check`), then replay |
| `PROVIDER_REQUEST_REJECTED`, `PROVIDER_OPERATION_FAILED` | The service refused the content or the operation failed for a content reason | Inspect the document and the `provider_error` artifact (the service's final response, downloadable from the document page); usually not replayable to success |
| `PROVIDER_RESULT_UNUSABLE` | The provider returned no usable contents (index stage) | Inspect the stored raw response artifact |
| `INPUT_UNSUPPORTED/CORRUPT/TOO_LARGE/TOO_MANY_PAGES` | Intake rejected the file (document status `rejected`, final) | Upload a corrected file |
| `STORAGE_TRANSIENT`, `STORAGE_MISSING`, `SQL_TRANSIENT`, `INTERNAL_ERROR` | Infrastructure or unexpected error; retried, bounded | If exhausted: check dependencies, then replay |
| `ARTIFACT_MISSING` | A stored artifact referenced by SQL is missing or its hash differs | Restore the blob (versioning/soft delete) or replay from an earlier stage |
| `CRASH_LOOP_EXHAUSTED` | A worker died repeatedly while running the stage | Check worker logs/memory, then replay |
| `UPLOAD_TIMEOUT`, `UPLOAD_STORAGE_ERROR` | The original never reached storage (`upload_failed`) | Upload again. A late-arriving blob is still accepted automatically |

## Replay

A document with status `failed` shows a **Replay** control (or `POST /api/documents/{id}/replay`, body `{"from_stage": "...", "reason": "..."}`;
both optional). Replay creates **attempt n+1** (the document list API pages with an opaque `cursor`/`next_cursor`); earlier attempts, tries, results and artifacts are never changed or deleted. Stages
before the replay point are *carried* by reference (for example `index` can be replayed from the stored provider response without a
new, billable analysis). Two simultaneous replays of the same document: one wins, the other gets `409`. `rejected` and
`completed` documents are not replayable in this version.

## Service Bus dead-letter queues

Exhausted *stage* failures are **not** broker dead letters: they are `failed` stages in SQL, which is what replay acts on. The
broker DLQ only receives poison messages (unparseable bodies, or messages exceeding the delivery count). Inspect a DLQ in the portal
or with Service Bus Explorer; the message body holds only identifiers. A dead-lettered stage message is harmless to the document:
the reaper re-enqueues runnable stages that have no pending message after `STAGE_RECOVERY_S`. Unparseable `blob-events` messages
indicate a changed Event Grid subscription (schema/filter); arrival for an uploaded document is also recovered from the blob itself
after `ARRIVAL_GRACE_S`.

## Provider evidence held in SQL

If Blob Storage is unavailable at the moment an analysis fails, the provider's final response is kept in `dbo.pending_artifacts` (committed with the failure) and the worker's reaper writes it to the `artifacts` container every `REAPER_POLL_S`, then records the normal `provider_error` artifact. Rows with `persisted_at IS NULL` that persist for long mean storage is still failing or no worker is running; no new provider call is needed.

## Routine tasks

* **Rotate the operator password**: update the Key Vault secret; restart `web` (or wait 5 minutes if the app reads Key Vault directly). All sessions are invalidated.
* **Change extraction fields or categories**: edit `registry.py`, bump `ANALYZER_CONFIG_VERSION` on `worker` and the setup job, run `setup-analyzers`, roll out. Old results keep the config version and hash they were produced with.
* **Add a worker replica**: just scale out; nothing else changes.
* **Inspect SQL read-only**: create a separate read-only database user (do not make a person the SQL administrator).
