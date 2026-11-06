# Document Desk — Technical Design

Status: design for implementation. Contract: `/project/PRJ-001.md`; requirements REQ-001…REQ-018.
Verification tier: **standard** (see §14).

## 1. Summary of decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | One Python 3.12 code base, one container image, four commands: `web`, `worker`, `migrate`, `setup-analyzers` (+ `smoke`). Deployed as two Container Apps (`web`, `worker`) and optional jobs (`migrate`, `setup-analyzers`) from the same image. | Smallest thing that satisfies the contract; shared domain code; one Dockerfile. |
| D2 | **Sync Python** (FastAPI endpoints as plain `def`, worker with threads, `pyodbc`, `azure-servicebus` sync client, `azure-storage-blob`, `azure-identity`). Raw T-SQL via a thin repository layer (no ORM). | The correctness-critical parts are specific T-SQL (`UPDATE … OUTPUT`, `UPDLOCK/READPAST`, unique constraints). An ORM hides exactly what must be explicit. Sync avoids async/ODBC impedance. |
| D3 | **SQL is the only authority.** Every stage transition is a single SQL transaction that also writes the next-stage **outbox** row. A separate dispatcher publishes outbox rows to Service Bus. Messages carry only identifiers. | REQ-003, REQ-011. |
| D4 | **Choreography by queues**: Event Grid → `blob-events` queue → *arrival* → `stage-intake` → `stage-analyze` → `stage-index` → `stage-result`. Each handler only commits its own outcome + an outbox row for the next stage. No orchestrator, no stage calls another. | REQ-004. Per-stage queues stop slow Content Understanding (CU) calls from starving cheap stages and allow per-stage concurrency. |
| D5 | **Leases + fencing in SQL** (not broker locks) decide who may run a stage try; bounded retries are SQL-recorded and re-enqueued through the outbox with a scheduled time; Service Bus `MaxDeliveryCount` is only a poison-message safety net. | Broker delivery is at-least-once and unordered; SQL must hold the truth (REQ-009/010/012). |
| D6 | **Attempts**: an attempt is one run of a document through the pipeline. Replay creates attempt *n+1* (optionally carrying forward already-succeeded early stages by reference). Nothing from attempt *n* is updated or deleted. | REQ-010, REQ-013. |
| D7 | **CU design: one classifier analyzer routing to 5 extractor analyzers**, all `prebuilt-document` based, GA API `2025-11-01`. Config is generated from one versioned registry in code. `other` has no extractor. | §6. Balances per-type field quality against the number of analyzers to validate/keep. |
| D8 | CU input is sent **inline as base64** (the app reads the blob with its managed identity). Default upload cap 20 MB (configurable). | No SAS/blob-access plumbing from the CU resource; keyless. Inline-body ceiling unverified → open question Q2. |
| D9 | Own **versioned normalized result** (`docdesk.result/1`): typed values, explicit `present/absent/invalid` per field, provider confidence/source only if returned (else `null`). Raw provider response stored byte-for-byte as its own artifact. | REQ-005/006/007. |
| D10 | Auth: single operator password (Key Vault) → signed, HttpOnly, `Secure`, `SameSite=Strict` session cookie; the same password is also accepted as `Authorization: Bearer` for scripted/smoke use. Only `/healthz` is unauthenticated (returns a constant). | REQ-017. |
| D11 | Server-rendered Jinja2 UI + a little vanilla JS (polling). No SPA build chain. | "Small, professional, usable" without a Node toolchain. |
| D12 | Local verification uses a **real SQL Server 2022** (verified installable in this sandbox, §14.1) plus in-process fakes for Blob, Service Bus and CU that preserve at-least-once semantics. Fakes live in `tests/` / `devtools/`, never in the runtime image's default path. | REQ-009/015; honest distinction between simulated and Azure. |

## 2. Context and component view

```
 browser ──HTTPS──► web (Container App, external ingress)
                      │  SQL: documents row (status=uploading)
                      │  Blob: originals/{document_id}/source.{pdf|tif}
                      ▼
   Blob Storage ──BlobCreated──► Event Grid (system topic) ──► Service Bus queue  blob-events
                                                                         │
 worker (Container App, ≥1 replica, scales on queue length)              ▼
   ├─ arrival handler  : SQL tx {document+attempt 1+stage_state(intake)+outbox(intake)}
   ├─ intake handler   : validate original, hash, page count         ─► outbox(analyze)
   ├─ analyze handler  : call CU, store raw response + text blobs    ─► outbox(index)
   ├─ index handler    : normalize+validate, write result + SQL fields─► outbox(result)
   ├─ result handler   : verify artifacts, mark document completed
   ├─ outbox dispatcher: SQL outbox ─► Service Bus (stage-* queues)
   ├─ reaper           : expired leases, stuck uploads, lost events
   └─ heartbeat/probes : worker_heartbeats, dependency_health
 Azure SQL Database = authority      Blob = originals + artifacts      CU (Foundry) = classify/extract
```

Queues (names configurable by prefix, default `docdesk-`): `blob-events`, `stage-intake`, `stage-analyze`, `stage-index`, `stage-result`.
Service Bus tier **Standard or Premium** (scheduled messages and duplicate detection are not in Basic). Queue settings: lock 5 min, `MaxDeliveryCount` 10, duplicate detection on (10 min; defence in depth only, never relied on), dead-lettering on expiry, TTL 14 days.

Blob containers: `originals` (written by web; Event Grid filtered to this container, `BlobCreated` only) and `artifacts` (written by worker; no events, so no feedback loop).

## 3. Repository layout

```
Dockerfile  pyproject.toml  README.md
docs/            design.md  deployment.md  result-schema.md  test-documents.md  operations.md
migrations/      0001_init.sql …            (plain T-SQL, forward-only)
src/docdesk/
  config.py          env + Key Vault settings (pydantic-settings), validated at start
  cli.py             web | worker | migrate | setup-analyzers | smoke
  db/                connection.py (pool, MI token), migrate.py, repo_*.py (one function = one tx)
  domain/            states.py, stages.py, errors.py (error catalog + retryability), retry.py
  adapters/          blobstore.py (Protocol + Azure + local-FS), broker.py (Protocol + Azure SB), keyvault.py
  cu/                registry.py (categories+fields, single source of truth), analyzer_config.py,
                     client.py (REST, polling, error mapping), normalize.py, result_schema.py
  pipeline/          messages.py, runner.py (claim/run/complete), handlers/{arrival,intake,analyze,index,result}.py,
                     outbox.py (dispatcher), reaper.py, worker.py (receiver loops), health.py
  web/               app.py, auth.py, api.py, ui.py, templates/, static/
tests/  unit/ integration/ live/         tools/gen_testdocs.py        devtools/ (simulated CU server, local stack)
```

## 4. Data model (Azure SQL — the authority)

All times `DATETIME2(3)` UTC, defaulted by the **database clock** (`SYSUTCDATETIME()`), including lease arithmetic, so worker clock skew cannot matter. All ids are `UNIQUEIDENTIFIER` unless noted. Statuses are `VARCHAR` with `CHECK` constraints. Forward-only migrations tracked in `schema_migrations`. The migration creates database **roles** `docdesk_web_role` and `docdesk_worker_role` with table-level grants. Web role: `SELECT` on all tables, `INSERT`/`UPDATE` on `documents` only (upload row; guarded `upload_failed` update), and `EXECUTE` on `dbo.usp_replay_document`. Worker role: `SELECT`/`INSERT`/`UPDATE` on all tables, no DDL. **Replay is a stored procedure** (§5.7) owned by `dbo` in the same schema as the tables, so ownership chaining lets the web role perform its inserts into `processing_attempts`, `stage_states` and `outbox` without holding any direct write permission on them. Database default `READ_COMMITTED_SNAPSHOT` is ON in Azure SQL; all SQL in this design is written for that setting (§5.5, §14.1). Users for managed identities are created by the deployment project and named after the identity, never after the roles (deployment knowledge K-018).

| Table | Key columns / purpose |
|---|---|
| `documents` | `document_id` PK; `original_filename` NVARCHAR(260); `content_type`; `size_bytes`; `sha256` (set at intake); `blob_container`, `blob_name`, `blob_etag`; `source` (`upload`/`blob_event`); `status`; `current_attempt_no` (**0** until arrival registers attempt 1); `category` (denormalised from current result); `current_result_id`; `needs_review` BIT; `retain_until` DATE; `created_at`, `updated_at`. This row **is** the durable arrival record, created *before* the blob is written. |
| `processing_attempts` | `attempt_id` PK; `document_id` FK; `attempt_no`; `UNIQUE(document_id, attempt_no)`; `trigger` (`initial`/`replay`); `replay_from_stage`; `replay_reason`; `prior_attempt_id`; `status` (`active`/`succeeded`/`failed`); `created_at`; `completed_at`. |
| `stage_states` | PK `(attempt_id, stage)`; `document_id`; `status` (`pending`/`running`/`retry_wait`/`succeeded`/`failed`/`carried`); `tries`; `max_tries`; `lease_owner`; `lease_expires_at`; `next_run_at`; `first_started_at`; `last_started_at`; `completed_at`; `carried_from_attempt_id`; `last_error_code`; `last_error_message`. |
| `stage_tries` | one row per try, append-only history: `try_id` identity PK; `(attempt_id, stage, try_no)` unique; `worker_id`; `message_id`; `started_at`; `finished_at`; `outcome` (`succeeded`/`retryable_error`/`terminal_error`/`lease_lost`/`lease_expired`); `error_code`; `error_message` (sanitised, ≤500 chars); `http_status`; `provider_request_id`. |
| `artifacts` | `artifact_id` PK; `document_id`; `attempt_id` (NULL for `original`); `stage`; `try_no`; `kind` (`original`/`provider_response`/`extracted_text`/`normalized_result`); `container`; `blob_name`; `sha256`; `size_bytes`; `content_type`; `created_at`. Rows are never updated or deleted. |
| `document_results` | `result_id` PK; `document_id`; `attempt_id` **UNIQUE** (at most one result per attempt); `result_schema_version`; `category`; `classification_status`; `category_provider_raw`; `provider_name`; `api_version`; `analyzer_id`; `analyzer_config_version`; `analyzer_config_hash`; `provider_operation_id`; `raw_artifact_id`; `text_artifact_id`; `normalized_artifact_id`; `page_count`; `text_char_count`; `flags_json`; `created_at`. |
| `document_fields` | PK `(result_id, field_name)`; `value_type`; `status` (`present`/`absent`/`invalid`); `value_text` NVARCHAR(MAX) (the **complete** normalized string value, never truncated); `value_string` NVARCHAR(400) (bounded *search key*: the first 400 characters of `value_text`, or the whole value if shorter) with `value_string_truncated` BIT; `value_number` DECIMAL(19,4); `value_date` DATE; `value_bool` BIT; `value_json` NVARCHAR(MAX) (arrays/objects, `ISJSON` check); `confidence` REAL NULL; `source` NVARCHAR(500) NULL. One row per field *defined for the category*, including `absent` ones, so "not found" is explicit. Indexes on `(field_name, value_date)`, `(field_name, value_number)` and `(field_name, value_string)` — the last key is at most 64·2 + 400·2 = 928 bytes, inside SQL Server's 1,700-byte nonclustered-key limit; `value_text`/`value_json` are never index keys. `source` is truncated to 500 characters in SQL (the full value stays in the normalized artifact). No normalized value can make the index stage fail on length: SQL columns are either `MAX` or a bounded derivative, and the normalized-result artifact always keeps the untruncated original. |
| `outbox` | `outbox_id` PK (= Service Bus `MessageId`); `queue`; `body` NVARCHAR(MAX) JSON; `not_before` (scheduled enqueue); `created_at`; `claimed_by`; `claimed_until`; `dispatched_at` NULL; `dispatch_attempts`; `last_error`. Filtered index `ix_outbox_pending (created_at) WHERE dispatched_at IS NULL` — keyed on `created_at` so the dispatcher's `ORDER BY created_at` is index-supported (verified necessary for `READPAST` to skip locked rows and return the next one, §5.5). |
| `worker_heartbeats` | `worker_id`, `role`, `version`, `started_at`, `last_seen_at`. |
| `dependency_health` | `name` (`servicebus`/`blob`/`content_understanding`/`analyzer`), `status`, `checked_at`, `error_class`, `detail` (non-sensitive), written by the worker's probe loop. |
| `schema_migrations` | applied migration ids. |

Document `status` values: `uploading` (row exists, waiting for the blob-arrival event) → `processing` → `completed` | `failed` (retries exhausted or terminal provider failure; replayable) | `rejected` (intake validation: unsupported/corrupt/oversize/too many pages; terminal, not replayable) | `upload_failed` (blob never arrived). `needs_review` is separate from status.

## 5. Processing model

### 5.1 Messages
All stage messages: `{"v":1,"document_id":…,"attempt_id":…,"stage":"analyze"}` (JSON, `MessageId = outbox_id`, `subject = stage`). A message **directs work; it never asserts state**. Handlers re-read SQL.

### 5.2 Upload and arrival
1. `POST /api/documents`: authenticate; stream to a spooled temp file enforcing `MAX_UPLOAD_MB` while reading; check extension + magic bytes (`%PDF-`, `II*\0` / `MM\0*`); on failure return a precise 4xx (`UNSUPPORTED_TYPE`, `TOO_LARGE`, `EMPTY_FILE`, `CORRUPT_HEADER`) and create **no** row.
2. Insert `documents` (`status=uploading`, `current_attempt_no=0`, `source=upload`, `retain_until` from `RETENTION_DAYS`). Upload blob `originals/{document_id}/source{ext}` with `overwrite=False`. **An exception from the blob client is not proof that the blob was not stored** (a committed write with a lost acknowledgement is possible). On exception the web does a `HEAD`: blob exists with the expected size ⇒ treat as stored (return 202; arrival proceeds as normal); blob definitely absent (404) ⇒ `UPDATE documents SET status='upload_failed', last_error… WHERE document_id=@d AND status='uploading'` (guarded: if arrival already advanced the document the update affects 0 rows and the document stays as arrival left it), return 502 with the document id; `HEAD` itself fails ⇒ leave `uploading`, return 502 "outcome unknown"; the reaper (§5.5) reconciles it against blob existence.
3. Event Grid emits `BlobCreated`; its Service Bus subscription lands on `blob-events`.
4. **Arrival handler** (`register_arrival`, one tx). Parse the event (tolerate object or array bodies, Event Grid or CloudEvents schema); derive `document_id` from the blob name (`{guid}/…`; non-matching names are logged `ARRIVAL_IGNORED` and acked). Then:
   1. If no `documents` row exists, insert one (`status=uploading`, `current_attempt_no=0`, `source=blob_event`); a unique-key violation means it already exists — continue.
   2. **Winner election**, a single guarded statement: `UPDATE documents SET status='processing', current_attempt_no=1, blob_etag=@etag, updated_at=… WHERE document_id=@d AND current_attempt_no=0 AND status IN ('uploading','upload_failed')` (a late blob after a mistaken `upload_failed` is accepted as arrival).
   3. **Only if that statement affected 1 row**, in the same tx, insert attempt 1 (`UNIQUE(document_id, attempt_no)` as a second guard), `stage_states(intake, pending)` and `outbox(stage-intake)`. If it affected 0 rows the arrival is a duplicate or late: the tx changes nothing and the message is acked.
   Consequently a repeated event after `processing`, `completed`, `failed`, or `rejected` can never move status backwards or create a second attempt.

**General rule for every `documents.status` write** (stage success/failure, reaper, replay, upload): it is a single `UPDATE` whose `WHERE` names the expected prior status and, where an attempt is involved, `current_attempt_no=@attempt_no`; zero rows affected means "someone else already moved it", never an error to overwrite. Stage-completion and failure transactions also fence on `stage_states` (§5.3), so a stale or duplicate worker cannot change the document.

### 5.3 The stage runner (one implementation used by all four stages)
```
handle(msg):
  claim = repo.claim_stage(attempt_id, stage, worker_id)       # tx 1
  if claim is None: ack; return                                # duplicate / in flight / terminal / not due
  try:   result = handler.run(claim)                           # outside any tx; renews lease
  except StageError as e: repo.record_failure(claim, e); ack   # tx: retry_wait+outbox(scheduled) OR failed
  else:  repo.complete_stage(claim, result)                    # tx 2, fenced; includes outbox(next)
  unexpected exception → abandon message (broker redelivers; lease/reaper cover the rest)
```
**Claim** (single statement, atomic):
```sql
UPDATE stage_states
   SET status='running', tries=tries+1, lease_owner=@w,
       lease_expires_at=DATEADD(SECOND,@lease_s,SYSUTCDATETIME()),
       last_started_at=SYSUTCDATETIME(), first_started_at=COALESCE(first_started_at,SYSUTCDATETIME())
OUTPUT inserted.tries
 WHERE attempt_id=@a AND stage=@s AND tries<max_tries
   AND ( status='pending'
      OR (status='retry_wait' AND next_run_at<=SYSUTCDATETIME())
      OR (status='running'    AND lease_expires_at<SYSUTCDATETIME()) );
```
Same tx inserts the `stage_tries` row; if it stole an expired lease it first closes the dead try as `lease_expired`. Zero rows ⇒ ack and move on: the stage is terminal, already leased by a live worker (the reaper re-enqueues if that worker dies), or a scheduled retry is not yet due (the scheduled message exists). Acking duplicates is safe *because* every path that makes a stage runnable also writes an outbox row (see 5.5).

**Fenced completion** (same tx as all effects):
```sql
UPDATE stage_states SET status='succeeded', completed_at=SYSUTCDATETIME(), lease_owner=NULL, lease_expires_at=NULL
 WHERE attempt_id=@a AND stage=@s AND status='running' AND lease_owner=@w AND tries=@n;   -- must affect 1 row
```
0 rows ⇒ the lease was lost: roll back, record `lease_lost`, discard (artifacts written by that try are orphans under their own `try{n}` path, kept, harmless). The same tx inserts `artifacts`/`document_results`/`document_fields` and the next stage's `stage_states` + `outbox`. `UNIQUE(attempt_id)` on results and `UNIQUE(attempt_id,stage,try_no)` on tries are a second line of defence against duplicate effects.

**Lease renewal**: long calls (CU polling) renew with `UPDATE … SET lease_expires_at=… WHERE lease_owner=@w AND tries=@n AND status='running'`; failure to renew aborts the try. Defaults: lease 5 min (analyze 10 min), renewed each poll.

### 5.4 Stages
| Stage | Work | Produces |
|---|---|---|
| `intake` | Read original; verify magic bytes, size ≤ limit, PDF/TIFF parse (`pypdf`, `Pillow`), encrypted/corrupt detection, page count ≤ `MAX_PAGES` (default 100; CU async limit is 2000); compute SHA-256, record etag. Validation failures are **terminal rejections**: `documents.status=rejected`, error code recorded, no retry. Transient blob/SQL errors retry. | `documents.sha256/size/content_type`, `artifacts(kind=original)`; outbox → analyze |
| `analyze` (text/semantic analysis) | `POST …/analyzers/{classifier}:analyze` with base64 input, poll `Operation-Location` (backoff, `CU_POLL_TIMEOUT` default 10 min). Store the **final operation JSON byte-for-byte** (`provider_response`). Extract text (see §7.3) to `extracted_text` (UTF-8 markdown). | two artifacts + `stage_tries.provider_request_id`; outbox → index |
| `index` (classification and metadata indexing) | Read raw artifact, run `normalize()` (pure function), validate, write `document_results`, `document_fields`, `normalized_result` artifact; update `documents.category/current_result_id/needs_review`. Re-runnable offline from the stored raw response (no CU cost). | result rows; outbox → result |
| `result` (result availability) | Verify result row + the three artifacts exist (`HEAD` on blobs, hashes recorded), then set `documents.status=completed`, attempt `succeeded`. | final state |

Attempt/document terminal transitions happen in the same tx as the stage failure/success that causes them.

### 5.5 Outbox and recoverable publication (REQ-011)
* Producer side: `INSERT outbox` is part of the same tx as the state change; so a SQL commit without a publishable message is impossible, and a rollback leaves no message.
* **Dispatcher** (thread in the worker, poll 1 s, batch 50, any number of replicas):
  ```sql
  ;WITH c AS (SELECT TOP (@n) * FROM outbox WITH (UPDLOCK, READPAST, READCOMMITTEDLOCK)
               WHERE dispatched_at IS NULL AND not_before<=SYSUTCDATETIME()
                 AND (claimed_until IS NULL OR claimed_until<SYSUTCDATETIME()) ORDER BY created_at)
  UPDATE c SET claimed_by=@w, claimed_until=DATEADD(SECOND,60,SYSUTCDATETIME()), dispatch_attempts=dispatch_attempts+1
  OUTPUT inserted.*;
  ```
  `READCOMMITTEDLOCK` makes the read take real locks even when the database has `READ_COMMITTED_SNAPSHOT` ON (the Azure SQL default), as Microsoft's `READPAST` documentation requires; no `ROWLOCK` hint is given (the engine chooses granularity). The architect ran this exact shape on SQL Server 2022 with the option ON: with the hint pair above or with `ROWLOCK` it executed without error and two concurrent transactions claimed different rows — **but only once the filtered index was keyed on `created_at`**; without that supporting index the second transaction's `TOP (1) … ORDER BY created_at` returned no row at all. The `READCOMMITTEDLOCK` form is kept because it is the documented-safe spelling. Then send (`MessageId=outbox_id`, `scheduled_enqueue_time` when `not_before` is in the future, so retry back-off is broker-timed but SQL-recorded), then `UPDATE … SET dispatched_at=SYSUTCDATETIME()`. If the send fails or the process dies at any point, the claim expires and another dispatcher resends. Crash after send but before the mark ⇒ **duplicate message**, which §5.3 absorbs. Broker outage ⇒ rows accumulate and drain on recovery; the diagnostics page shows oldest undispatched age.
* **Reaper** (worker thread, every 30 s, safe under concurrent replicas because each action is a guarded `UPDATE`):
  1. `running` stages with expired lease: close the try as `lease_expired`; if `tries<max_tries` set `pending` + new outbox row, else mark the stage/attempt/document `failed` (`CRASH_LOOP_EXHAUSTED`).
  2. `pending`/`retry_wait` stages with no undispatched outbox row and `next_run_at`/`last change` older than 2× poll (should not occur; belt-and-braces) → new outbox row.
  3. Documents `uploading` longer than `ARRIVAL_GRACE` (default 5 min): if the blob exists → run `register_arrival` (recovers lost/late events; logged `ARRIVAL_RECOVERED`); if not, after `UPLOAD_TIMEOUT` (15 min) → `upload_failed`.
* Service Bus DLQ catches only poison messages (unparseable body, repeated unexpected exceptions). The worker dead-letters unparseable messages explicitly with a reason. DLQ inspection is an operator procedure (`operations.md`); exhausted *stage* failures are not broker DLQ items — they are SQL `failed` stages, which is the "equivalent exhausted-failure handling" and is what replay acts on.

### 5.6 Retries (REQ-010)
`max_tries` per stage (env-overridable): intake 3, analyze 5, index 3, result 3. Back-off `min(300 s, 5 s · 2^(try-1))` ± 20 % jitter; for CU 429/503 use `Retry-After` as a floor. A retryable error: tx sets `retry_wait`, `next_run_at`, `last_error_*`, closes the try (`retryable_error`), inserts outbox with `not_before=next_run_at`. Exhausted or terminal error: stage `failed`, attempt `failed`, document `failed`, no outbox row. Error catalog in `domain/errors.py` (code → retryable?):

| Code | Retry | Notes |
|---|---|---|
| `PROVIDER_THROTTLED` (429) | yes | honours `Retry-After` |
| `PROVIDER_UNAVAILABLE` (5xx, network) | yes | |
| `PROVIDER_TIMEOUT` (poll timeout) | yes | a retry starts a new, billable analysis; bounded by `max_tries` |
| `PROVIDER_OPERATION_FAILED` (`status=Failed`) | by inner code | content errors terminal; others retry |
| `PROVIDER_REQUEST_REJECTED` (400/413/415/422) | no | |
| `PROVIDER_CONFIGURATION` (401/403/404 analyzer, `DefaultDeploymentModelNotFound`, `DefaultsNotSet`) | no | visible in diagnostics; operator fixes then replays |
| `PROVIDER_RESPONSE_MALFORMED` (analyze stage: not JSON / no `status`) | yes (bounded) | |
| `PROVIDER_RESULT_UNUSABLE` (index stage: no `contents`) | no | re-normalising the same bytes cannot change |
| `INPUT_UNSUPPORTED` / `INPUT_CORRUPT` / `INPUT_TOO_LARGE` / `INPUT_TOO_MANY_PAGES` | no → `rejected` | intake |
| `STORAGE_TRANSIENT`, `SQL_TRANSIENT` | yes | |
| `CRASH_LOOP_EXHAUSTED`, `LEASE_LOST` | reaper-driven / discard | |

### 5.7 Replay (REQ-010)
`POST /api/documents/{id}/replay {from_stage?, reason?}` (allowed only when `status=failed`; `from_stage` defaults to the first failed stage, and may be any stage up to it). The web calls **`EXEC dbo.usp_replay_document @document_id, @seen_attempt_no, @from_stage, @reason`** and gives the web role no other write access (§4); the procedure (declare `SET NOCOUNT ON` so the caller sees the result set first) contains this transaction and returns the new attempt id or a conflict code (mapped to 409). The architect verified on SQL Server 2022 that a user holding only `SELECT` plus `EXECUTE` on a `dbo` procedure can run its inserts into a `dbo` table, while a direct `INSERT` by the same user is denied:
```sql
BEGIN TRAN;
UPDATE documents SET status='processing', current_attempt_no=current_attempt_no+1, updated_at=SYSUTCDATETIME()
 WHERE document_id=@d AND status='failed' AND current_attempt_no=@seen_attempt_no;     -- 0 rows ⇒ 409 (concurrent replay)
INSERT processing_attempts (attempt_no=@seen+1, trigger='replay', prior_attempt_id, replay_from_stage, replay_reason …);
INSERT stage_states …  -- stages before @from_stage: status='carried', carried_from_attempt_id = the attempt that produced them
                       -- @from_stage: 'pending'; later stages: absent until produced
INSERT outbox (stage message for @from_stage);
COMMIT;
```
Carried stages let `index` be replayed from the stored raw response without a new CU call. `resolve_artifact(attempt, kind)` follows `carried_from_attempt_id`. The old attempt's rows, tries and artifacts are untouched, and artifact blob paths include the attempt number, so nothing is overwritten. Replay of a `rejected` or `completed` document is not offered in v1 (documented extension point).

### 5.8 Concurrency and scale (REQ-012)
No in-process state matters: any number of `worker` replicas may consume the same queues and run dispatcher/reaper. Correctness rests on: guarded claim, fenced completion, `READPAST` dispatch, unique constraints, and idempotent arrival. Per-queue receiver concurrency: `WORKER_CONC_{INTAKE,ANALYZE,INDEX,RESULT}` (defaults 4/4/4/2; analyze additionally bounded by `CU_MAX_CONCURRENCY`). `worker` min replicas must be ≥ 1 (dispatcher/reaper must run); scale out on queue length (deployment's concern; documented).

## 6. Content Understanding design (GA `2025-11-01`)

Sources checked against Microsoft Learn on the design date: Analyze and Create-Or-Replace REST reference for `2025-11-01`, the classifier concept page and service limits. Everything below that was **not** observed against a live resource is listed in §15.

### 6.1 Analyzer topology
**Decision: one classifier analyzer with `contentCategories`, each category routed via `analyzerId` to one of five type-family extractors; `other` has no `analyzerId`.** Alternatives rejected: (a) *single analyzer, classify as a `classify` field plus all fields* — a ~120-field schema, all fields attempted for all documents, hurting precision, and no native `other` handling; (b) *13 analyzers, one per type* — more config to validate and version for little gain since several families share their fields; (c) *app-side two-call flow* (classify, then call the extractor) — works but forfeits the service's built-in routing and doubles the stage logic. Routing keeps one analyze call per document. Multi-document segmentation (`enableSegment`) is **off** in v1: one category per file; mixed packets are classified as a single document (documented limitation).

| Analyzer id (v1) | Serves categories |
|---|---|
| `docdesk-classifier-v1` | routes all |
| `docdesk-transaction-v1` | `invoice`, `receipt`, `purchase_order`, `expense_report` |
| `docdesk-statement-v1` | `bank_statement`, `credit_card_statement` |
| `docdesk-agreement-v1` | `msa`, `nda`, `sow`, `vendor_agreement` |
| `docdesk-tax-v1` | `tax_document` |
| `docdesk-insurance-v1` | `certificate_of_insurance` |

Analyzer ids are `docdesk-<family>-v<N>` (regex `^[a-zA-Z0-9._-]{1,64}$`). The numeric suffix is `ANALYZER_CONFIG_VERSION` (default `1`). **A definition under an existing id is never changed** (deployment knowledge K-015); changing the registry requires a version bump, which creates new ids.

### 6.2 Classifier definition (sketch; generated by `analyzer_config.py`)
```json
{
  "description": "Document Desk classifier v1",
  "baseAnalyzerId": "prebuilt-document",
  "models": { "completion": "gpt-4.1", "embedding": "text-embedding-3-large" },
  "config": {
    "returnDetails": false,
    "enableSegment": false,
    "contentCategories": {
      "invoice": { "description": "Request for payment from a vendor …", "analyzerId": "docdesk-transaction-v1" },
      "receipt": { "description": "Evidence of a completed purchase or payment …", "analyzerId": "docdesk-transaction-v1" },
      "…": {},
      "other":   { "description": "Anything that is not clearly one of the categories above, or has too little content to classify reliably." }
    }
  },
  "tags": { "app": "docdesk", "configVersion": "1" }
}
```
Category names are the 12 domain types (snake_case) plus `other`; descriptions are written from `document-domain.md` and include discriminating cues (e.g. *msa* vs *vendor_agreement*: "umbrella terms governing a relationship, expects separate SOWs/orders"; *sow*: "project-specific scope, deliverables, milestones"). The registry keeps the descriptions next to the categories. Do not set `omitContent` (the classifier's own content item carries the markdown text used for `other`).

### 6.3 Extractor definition (sketch; `docdesk-transaction-v1`)
```json
{
  "description": "…", "baseAnalyzerId": "prebuilt-document",
  "models": { "completion": "gpt-4.1", "embedding": "text-embedding-3-large" },
  "config": { "returnDetails": false, "estimateFieldSourceAndConfidence": true, "enableOcr": true, "enableLayout": true },
  "fieldSchema": { "name": "transaction", "description": "…",
    "fields": {
      "vendor_name":   { "type": "string", "method": "extract",  "description": "Name of the seller / issuer / merchant as printed." },
      "document_date": { "type": "date",   "method": "extract",  "description": "Issue, transaction or order date." },
      "total_amount":  { "type": "number", "method": "extract",  "description": "Grand total including tax, numeric only." },
      "line_items": { "type": "array", "items": { "type": "object", "properties": {
           "description": {"type":"string"}, "quantity": {"type":"number"}, "unit_price": {"type":"number"}, "amount": {"type":"number"} } } }
    } }
}
```
Rules: every field has a description that says "leave empty if not stated"; `method: extract` for values that appear verbatim, `generate` only for the two explicitly summarising fields (`summary`, `scope_summary`); no field asks the model to decide legal/financial outcomes. `returnDetails` off by default to keep the persisted provider response manageable (config switch `CU_RETURN_DETAILS`; recorded in the config hash).

### 6.4 Field registry (normalized names; `cu/registry.py` is the single source for analyzers, normalizer, SQL indexing and UI labels)
Types: `string`, `date`, `number`, `integer`, `boolean`, `array<string>`, `array<object>`. Money is `number` plus a sibling `currency` string (ISO 4217 as printed). Every field is **optional**; every field defined for the category is reported with an explicit status.

**Common (every category except `other`)** — `document_title` (string), `document_date` (date), `reference_number` (string; invoice/PO/receipt/report/agreement/policy number), `currency` (string), `summary` (string, `generate`, one neutral sentence, informational).

**transaction** (invoice, receipt, PO, expense report): `vendor_name`, `customer_name`, `due_date` (date), `subtotal`, `tax_amount`, `total_amount`, `amount_due` (numbers), `payment_method`, `payment_terms`, `delivery_terms` (strings), `submitted_by` (expense report), `period_start`/`period_end` (dates, expense report), `line_items` (array<object: description, quantity, unit_price, amount>).

**statement** (bank, credit card): `institution_name`, `account_holder_name`, `account_reference` (string *as printed, typically masked; extraction instructed never to reconstruct digits*), `period_start`, `period_end`, `opening_balance`, `closing_balance`, `total_credits`, `total_debits`, `minimum_payment_due`, `payment_due_date`, `credit_limit`, `transactions` (array<object: date, description, amount>).

**agreement** (MSA, NDA, SOW, vendor agreement): `parties` (array<string>), `effective_date`, `expiration_date`, `term_description`, `governing_law`, `confidentiality_duration_text` (NDA, verbatim), `permitted_use_text` (NDA, verbatim), `termination_clause_text` (verbatim), `liability_limitation_clause_text` (verbatim), `scope_summary` (SOW, `generate`), `deliverables` (array<string>), `milestones` (array<object: description, date>), `total_fees` (number), `related_agreement_reference`. Clause fields hold quoted text only; the UI labels them "informational extract — not a legal interpretation".

**tax_form**: `form_type` (e.g. W-9, 1099-NEC), `tax_year` (integer), `entity_name`, `reported_amounts` (array<object: label, amount>). **Taxpayer identifiers (SSN/EIN) are deliberately not extracted.**

**insurance_certificate**: `insured_name`, `insurer_name`, `certificate_holder_name`, `policy_numbers` (array<string>), `policy_types` (array<string>), `effective_date`, `expiration_date`, `coverage_limits` (array<object: coverage, limit_text>).

Per category the registry lists `applicable_fields` (analyzers are shared across a family, so inapplicable fields returned by the provider are dropped from the normalized result but kept in the raw artifact) and `key_fields` (used by the plausibility flag, §7.4).

## 7. Normalized result, validation, uncertainty

### 7.1 Normalized result `docdesk.result/1` (JSON artifact; schema documented in `docs/result-schema.md` with a JSON Schema file, validated in tests)
```json
{
  "schema_version": "docdesk.result/1",
  "document_id": "…", "attempt_no": 1, "generated_at": "…",
  "provider": { "name": "azure-content-understanding", "api_version": "2025-11-01",
                "classifier_analyzer_id": "docdesk-classifier-v1", "extractor_analyzer_id": "docdesk-transaction-v1",
                "config_version": "1", "config_hash": "sha256:…", "operation_id": "…" },
  "classification": { "category": "invoice", "provider_category": "invoice",
                      "status": "classified | other | unrecognized_category | not_classified",
                      "confidence": null },
  "text": { "artifact_id": "…", "status": "present | missing", "char_count": 1234, "page_count": 2 },
  "fields": {
    "total_amount": { "status": "present", "type": "number", "value": 1234.5, "confidence": 0.91, "source": "D(1,…)", "provider_type": "number" },
    "due_date":     { "status": "absent",  "type": "date",   "value": null, "confidence": null, "source": null },
    "line_items":   { "status": "present", "type": "array",  "value": [ {"description": "…", "quantity": 2, "unit_price": 10, "amount": 20} ], "confidence": null, "source": null }
  },
  "flags": [ { "code": "FIELD_INVALID", "severity": "warning", "field": "due_date", "message": "…" } ]
}
```
Rules: `confidence`/`source` are copied only when the provider returned them, else `null`; **no classification confidence is invented** — GA returns none at category level when `enableSegment=false`, so `classification.confidence` is `null` and the UI says "not provided". No scores are aggregated, calibrated or turned into probabilities. Dates parse as ISO `YYYY-MM-DD`; numbers kept as provider doubles and stored as `DECIMAL(19,4)` via `Decimal(str(x))`; strings trimmed, empty ⇒ `absent`; **no length limit is applied to the value** — the full string goes to the artifact and to `document_fields.value_text`, and only the bounded `value_string` search key is truncated (400 chars, `value_string_truncated=1`; nothing is lost, so no flag is raised). A value that fails type validation ⇒ `status=invalid` with the raw value preserved in `value_string` and a `FIELD_INVALID` flag.

### 7.2 Reading the provider response (`normalize.py`)
Defensive, shape-tolerant, based on the documented `AnalysisResult` plus the observed routed shape (deployment knowledge K-006):
* Root item = `contents[]` entry with `mimeType` and no `category` (or path without `/`); require `mimeType` **only** on this item. Routed item = entry with `category`, matched to the root by `path` prefix (`input1/segment1`). Zero routed items is valid for `other`.
* `fields` values are `{type, valueString|valueNumber|valueInteger|valueDate|valueBoolean|valueArray|valueObject, confidence?, source?, spans?}`; a key that is missing, or has no `value*` member, is `absent`. Unknown extra fields: retained in raw only (counted in a `PROVIDER_EXTRA_FIELDS` info flag).
* An empty or invalid `contents`, a non-JSON body, or a missing/unknown `status` ⇒ `PROVIDER_RESULT_UNUSABLE` / `PROVIDER_RESPONSE_MALFORMED`.
* Provider `warnings[]` are copied into flags (`PROVIDER_WARNING`, code only + message truncated).

### 7.3 Text
`extracted_text` = root item `markdown`; if absent, the concatenated routed items' markdown (in path order); if neither, `text.status=missing` + flag `TEXT_MISSING`. Page count from the root item's `endPageNumber`/`pages` when present, else the intake page count.

### 7.4 Uncertainty and flags (REQ-007)
`needs_review = true` when any of: category `other`/`unrecognized_category`/`not_classified`; flag `KEY_FIELDS_ABSENT` (category not `other` but *none* of its `key_fields` is present — a plain, explainable plausibility check, not a probability); `TEXT_MISSING`; any `FIELD_INVALID`; provider `warnings`. Fields with provider confidence below `LOW_CONFIDENCE_THRESHOLD` (config, default 0.5, **a display heuristic, not a calibrated score**) get an informational `FIELD_LOW_CONFIDENCE` flag. The UI shows "Needs review" and the flag list; it never states accuracy.

## 8. CU client behaviour (`cu/client.py`)
* Entra token (`https://cognitiveservices.azure.com/.default`) via `ManagedIdentityCredential` (user-assigned id from `AZURE_CLIENT_ID`; `DefaultAzureCredential` locally). No keys.
* `POST {endpoint}/contentunderstanding/analyzers/{id}:analyze?api-version=2025-11-01`, body `{"inputs":[{"data":"<base64>","mimeType":"application/pdf|image/tiff"}]}`; 202 + `Operation-Location`; poll `GET` with 2 s → 10 s back-off until `Succeeded`/`Failed`/`Canceled`/timeout. `x-ms-client-request-id` = a per-try UUID recorded in `stage_tries.provider_request_id` (correlate with Microsoft support).
* Treat empty/non-JSON bodies on 2xx as `PROVIDER_RESPONSE_MALFORMED` (deployment knowledge K-014), and test with empty/error replies as well as success.
* The raw bytes of the final poll response are written unmodified.
* Error mapping per §5.6; provider inner `error.innererror.code` is recorded in `error_code`/message (not just the outer 400).
* Setup command `docdesk setup-analyzers [--check] [--set-defaults]` runs **two phases in this order** (deployment knowledge K-004: every `prebuilt-document`-based analyzer is rejected with `DefaultDeploymentModelNotFound` until the embedding alias default exists):
  1. **Defaults phase (before any analyzer request).** `GET /contentunderstanding/defaults`; a 400 with inner code `DefaultsNotSet` means "none yet" (K-011). Required: a deployment mapping for the completion model(s) used in `models` and for the embedding alias `prebuilt-analyzer-embedding`. If anything required is missing: with `--set-defaults`, `PATCH` `modelDeployments` from `CU_MODEL_DEPLOYMENTS` (JSON `{model-or-alias: deployment-name}`; setup refuses to start the phase if that variable lacks the embedding alias) and read back to confirm; without `--set-defaults` (or with `--check`), stop **before registering anything** with a diagnosis naming the missing mapping and the exact flag/variable that fixes it. Provider inner error codes are printed (K-011).
  2. **Analyzer phase**, in dependency order (extractors, then classifier): `GET`; if absent `PUT …?allowReplace=false` and wait for `status=ready`; if present compare to the authored definition **treating an authored empty object as equal to its absence** (K-005); equal ⇒ skip and log "skipped"; different ⇒ fail telling the operator to bump `ANALYZER_CONFIG_VERSION`.
  The deployment guide documents the same order (defaults → analyzers) and its failure modes. `--check` never writes.
Local pre-validation: the authored definitions are checked by a pydantic model mirroring the documented request schema (allowed keys/enums, id regex, ≤200 categories, ≤4096-char descriptions, field-name regex, ≤1000 fields) and a unit test runs it; the *authoritative* validation is the live `PUT`, which the smoke test exercises.
* Config hash = SHA-256 of the canonical JSON of all six definitions; recorded per result with analyzer ids, API version and config version (REQ-005 provenance).

## 9. Web/API

Auth (`web/auth.py`): `POST /login` (form) and `POST /api/auth/login` (JSON) compare with `hmac.compare_digest` against the operator password; sleep 0.5 s on failure plus per-IP in-memory failure throttle (5/min; best effort across replicas). Session cookie = `base64(payload{exp, nonce}).HMAC` with key `HKDF(password)`, `HttpOnly; Secure; SameSite=Strict; Path=/`, 8 h; rotating the password invalidates all sessions; no server-side session store. A FastAPI dependency on a router that contains **every** route except `/healthz` and the login endpoints enforces auth; a test enumerates `app.routes` and fails if any other path is reachable anonymously. State-changing requests additionally require a CSRF token (HMAC of the session nonce) in a header/hidden field, plus an `Origin` check. `/docs`, `/openapi.json` disabled. The password is read from env `OPERATOR_PASSWORD` (Container Apps Key Vault secret reference) **or**, if `KEY_VAULT_URL` + `OPERATOR_PASSWORD_SECRET_NAME` are set, from Key Vault via managed identity with a 5-minute cache; startup fails if neither yields a non-empty password. TLS is terminated by Container Apps ingress (`allowInsecure=false`); uvicorn runs with `--proxy-headers`; `COOKIE_SECURE=false` only for `APP_ENV=dev`.

| Route | Purpose |
|---|---|
| `GET /healthz` | **only** anonymous route; constant `{"status":"ok"}`, no dependency detail |
| `GET /login`, `POST /login`, `POST /logout`, `POST /api/auth/login` | sign-in |
| `GET /` | document list + upload form (drag/drop, multi-file; one request per file; per-file error display) |
| `GET /documents/{id}` | detail page (header, status, category, fields incl. explicit "not found", text, flags, stage timeline, attempts & tries, artifacts, replay button when `failed`) |
| `GET /status` | operator diagnostics page |
| `GET /api/documents` (`status`, `category`, `q` filename, `limit`, cursor) | list from SQL |
| `POST /api/documents` | upload → `202 {document_id, status:"uploading"}` or typed 4xx |
| `GET /api/documents/{id}` | full detail JSON (the UI renders the same payload) |
| `GET /api/documents/{id}/original` | streamed through the app (no SAS handed out); `Content-Disposition: inline` for PDF (`?download=1` forces attachment), attachment for TIFF; `X-Content-Type-Options: nosniff` |
| `GET /api/documents/{id}/artifacts/{artifact_id}` | raw provider response / text / normalized result download |
| `POST /api/documents/{id}/replay` | §5.7; `409` if not `failed` or concurrent replay |
| `GET /api/diagnostics` | §10 |

The UI polls `GET /api/documents` every 5 s while any listed document is non-terminal. All displayed values come from SQL (`document_fields`, `stage_*`, `processing_attempts`); text and raw JSON come from artifacts referenced by SQL. Jinja autoescaping on; extracted text rendered in `<pre>` (never as HTML).

## 10. Observability and diagnostics (REQ-018)
* Logs: JSON lines to stdout. Allowed keys only: `ts, level, event, document_id, attempt_no, stage, try_no, worker_id, error_code, http_status, duration_ms, provider_request_id, queue, outbox_id`. **Never** filenames, extracted text, field values, passwords, tokens or provider bodies. A logging filter drops unknown keys; a test asserts that processing a document with a distinctive string in its filename and text leaves no trace of it in captured logs. Exception logging records class name and code, not message text from providers.
* `GET /api/diagnostics` and `/status` page: SQL ping; Blob container probe; **worker-reported** `dependency_health` for Service Bus, Blob, CU endpoint, analyzer readiness (GET classifier = `ready`) and defaults — each with status, last-checked time and error class; worker heartbeats (age); counts of documents by status; stages `failed` in 24 h grouped by `error_code`; oldest undispatched outbox age; oldest `pending`/`retry_wait` stage age; stuck leases. The web identity needs no Service Bus or CU role because the worker's probe loop (60 s) stores these results in SQL. The web probes only SQL and Blob itself.
* Per-document: timeline (`stage_tries` and `stage_states`), `last_error_code/message` (sanitised, ≤500 chars, stored in SQL).

## 11. Configuration (env only; secrets via Key Vault)
Required: `SQL_SERVER`, `SQL_DATABASE`, `STORAGE_ACCOUNT_URL` (`https://<acct>.blob.core.windows.net`), `SERVICEBUS_NAMESPACE` (FQDN; worker), `CU_ENDPOINT`, plus `OPERATOR_PASSWORD` *or* `KEY_VAULT_URL`+`OPERATOR_PASSWORD_SECRET_NAME`; `AZURE_CLIENT_ID` when a user-assigned identity is used.
Optional (defaults): `ORIGINALS_CONTAINER=originals`, `ARTIFACTS_CONTAINER=artifacts`, `QUEUE_PREFIX=docdesk-`, `ANALYZER_CONFIG_VERSION=1`, `CU_COMPLETION_MODEL=gpt-4.1`, `CU_EMBEDDING_MODEL=text-embedding-3-large`, `CU_POLL_TIMEOUT_S=600`, `CU_MAX_CONCURRENCY=4`, `CU_RETURN_DETAILS=false`, `MAX_UPLOAD_MB=20`, `MAX_PAGES=100`, `RETENTION_DAYS=2555`, `LOW_CONFIDENCE_THRESHOLD=0.5`, `MAX_TRIES_*`, `LEASE_SECONDS`, `ARRIVAL_GRACE_S`, `UPLOAD_TIMEOUT_S`, `WORKER_CONC_*`, `LOG_LEVEL`, `APP_ENV` (`prod` default). Dev-only (rejected when `APP_ENV=prod`): `SQL_AUTH=sql_password` + `SQL_USER`/`SQL_PASSWORD`, `BLOB_BACKEND=local_fs`, `BROKER_BACKEND=inmemory`, `CU_ENDPOINT` pointing at a simulated server. SQL via ODBC Driver 18 with `Encrypt=yes`; Entra token passed via `SQL_COPT_SS_ACCESS_TOKEN` (cached, refreshed before expiry). Settings are validated at startup with clear, secret-free errors.

**Retention** (REQ-013): `RETENTION_DAYS` populates `documents.retain_until` (informational; nothing is purged by the app; failed inputs, all attempts and all artifacts are kept). Suggested default 2555 days (~7 years) is a placeholder, not a compliance determination; deployment guide recommends blob versioning/soft-delete and lifecycle rules be set by the infrastructure project.

## 12. Deployment documentation the engineer must write (`docs/deployment.md`)
Contents: resource list (Storage account + `originals`/`artifacts` containers; Event Grid system topic + subscription filtered to `Microsoft.Storage.BlobCreated`, subject prefix `/blobServices/default/containers/originals/`, destination the `blob-events` queue, managed-identity delivery; Service Bus Standard namespace + five queues with the settings in §2; Azure SQL database; Key Vault with the operator-password secret; Foundry resource (Cognitive Services, CU-supported region) with deployments for `gpt-4.1` and `text-embedding-3-large` and the CU defaults; Container Apps environment with `web` (external ingress, TLS only, port 8000, liveness `/healthz`) and `worker` (no ingress, min replicas ≥ 1) plus `migrate`/`setup-analyzers` jobs; Log Analytics), the env table above, and identities/roles:

| Identity | Roles |
|---|---|
| `web` MI | Storage Blob Data Contributor (storage account); Key Vault Secrets User (secret); SQL user in `docdesk_web_role` (read, insert/update `documents`, `EXECUTE` on `usp_replay_document`; no other writes) |
| `worker` MI | Storage Blob Data Contributor; Azure Service Bus Data Receiver **and** Data Sender (namespace); Cognitive Services User (Foundry resource); SQL user in `docdesk_worker_role` |
| `setup`/`migrate` job MI | SQL DDL rights (K-009: do not make a person SQL admin); a CU authoring-capable role on the Foundry resource (exact built-in role to confirm, Q4) |
| Event Grid system-topic MI | Azure Service Bus Data Sender on `blob-events`; Storage Blob Data Contributor at **storage-account scope** if a dead-letter container is used (K-002) |
| Operator (human) | Key Vault Secrets User to read the password (K-010; copy, don't display) |

Also: CU billing (per-page analysis + model tokens in the Foundry deployments + contextualization tokens for confidence/grounding), model-deployment capacity, supported-region check, `migrate` → `setup-analyzers` run order, and within `setup-analyzers` the fixed order **model defaults (embedding alias `prebuilt-analyzer-embedding` + completion deployment) first, analyzers second** with the failure message for each missing prerequisite, smoke-test procedure (§14.4), known limitations (single category per file, 20 MB default cap, no segmentation, single replica-local login throttle). No Terraform/pipelines are delivered.

## 13. Dockerfile
Multi-stage `python:3.12-slim-bookworm`; install `msodbcsql18` (Microsoft apt repo, `ACCEPT_EULA=Y`), `unixodbc`; install the package into a venv; run as non-root; `ENTRYPOINT ["docdesk"]`, `CMD ["web"]` (uvicorn on 8000, `--proxy-headers`). No secrets or tenant ids in the image; the `devtools/` and `tests/` directories are excluded via `.dockerignore`. The build cannot be run in the sandbox (no Docker): the Engineer must state that the image build is unverified unless a builder is available.

## 14. Verification (tier: standard)

### 14.1 Local dependencies (verified by the architect in this sandbox)
SQL Server 2022 Developer installs from the Microsoft apt repo and runs **without Docker or systemd** (`sudo /opt/mssql/bin/sqlservr` as user `mssql` with `MSSQL_SA_PASSWORD`, `ACCEPT_EULA=Y`, `MSSQL_DATA_DIR`, `MSSQL_MEMORY_LIMIT_MB=2048`); `msodbcsql18` + `unixodbc-dev` install via apt; `pyodbc` connected successfully from a venv (`python3-venv` is needed on Ubuntu 22.04). Integration tests use a throwaway database per test session via a pytest fixture reading `TEST_SQL_SERVER/…` (skip with a clear message, not silently pass, if unreachable). **The fixture creates the database with `ALTER DATABASE … SET READ_COMMITTED_SNAPSHOT ON`** so the local engine matches Azure SQL's default; there is no matrix of isolation settings. The T-SQL used (OUTPUT, UPDLOCK/READPAST/READCOMMITTEDLOCK, filtered indexes, stored procedure with ownership chaining, MERGE-free guarded inserts) exists in both engines, but remaining Azure-SQL-specific behaviour (Entra token login, service-tier limits) is not exercised locally. **Azurite** (Blob emulator) *is* installable here without Docker: Node 20 from the official tarball (the distro's Node 12 is too old for current Azurite; `apt` `nodejs` from NodeSource conflicts with the distro package, so unpack the tarball under a throw-away directory and put it on `PATH`), `npm install -g azurite --prefix <dir>`, then `azurite-blob --skipApiVersionCheck --blobPort 10002 --location <dir>` (the flag is needed because the current `azure-storage-blob` SDK sends a newer service API version than the Azurite release supports). The architect verified create-container, upload with `overwrite=False` (second write raises `ResourceExistsError`), properties and download from Python. The Azure Blob adapter's tests therefore run against Azurite using its well-known development connection string — a dev-only auth path; managed-identity/Entra authentication is **not** exercised by Azurite and remains unverified until a real storage account is used. Azurite is an emulator, not Azure: its results must be reported as "emulator", not as live verification. Service Bus has no usable local emulator without Docker (the Service Bus emulator is container-only), and Event Grid and Content Understanding have none, so those are faked behind the Protocols in §3.

Fakes, with required behaviour: **FS blob store** (same Protocol, used for fast broker/pipeline tests and for the dev stack; the Azurite-backed run covers the real SDK adapter; optionally emits a schema-correct `BlobCreated` message into the in-memory `blob-events` queue to *simulate* Event Grid, in dev only). **In-memory broker**: per-queue at-least-once with visibility lock, redelivery on abandon/lock expiry, `delivery_count`, DLQ after max delivery, scheduled enqueue, and test hooks to duplicate/reorder messages and to fail `send`. **Simulated CU server** (`devtools/`, an ASGI app used through `httpx`): analyze → 202 + operation URL → poll → result shaped like the documented/observed response; scriptable 429+`Retry-After`, 5xx, timeout, `Failed`, empty body, malformed JSON. Its responses carry `"_simulated": true` and the UI shows a "SIMULATED PROVIDER" banner whenever a result's provider block says so. These fakes are never selected in `prod`.

### 14.2 What must be verified (and nothing beyond tier)
* **Unit**: registry → analyzer definitions (schema-validity, id regex, `other` present, hash stability); normalizer against synthetic response fixtures (present/absent/invalid, routed vs root items, empty `contents`, unknown category, text fallback, provider confidence passthrough/`null`); retry/back-off and error classification; auth/session/CSRF; log filter.
* **Integration (real SQL Server + fakes)** — the REQ-009/010/011/012 proofs: duplicate `blob-events` delivery ⇒ one document/attempt/outbox row; the same stage message delivered twice and concurrently from several threads/workers ⇒ exactly one successful effect (one result, one next-stage outbox row); stale-lease worker's completion is rejected; lease expiry → reaper → re-run; outbox: crash/`send` failure between commit and publish and between publish and mark ⇒ message eventually delivered (possibly twice) with unchanged final state; bounded retries → `failed` with full `stage_tries` history; replay creates attempt 2, retains attempt 1 rows/artifacts, concurrent replays ⇒ one wins, `from_stage=index` carries forward and makes no CU call; N documents × M concurrent workers, with one document stuck on a throttled/failing provider, all others complete (REQ-002/012); uploading-document recovery (lost event, blob missing); **registration/transition guards**: a duplicate or late `BlobCreated` after `completed`, `failed` and `rejected` leaves status, attempt count and outbox unchanged, and an upload-error path racing arrival cannot overwrite `processing`; **dispatcher on an RCSI-on database** claims disjoint rows for two concurrent claimers; **replay executed through the documented web role** (a contained SQL user added only to `docdesk_web_role`) succeeds via `usp_replay_document`, a direct `INSERT` into `processing_attempts` by that user is denied, and concurrent replays yield one winner; **long-value persistence**: a termination clause of >1,000 characters and one of ~900 characters (the index-key-limit case) both persist in full in `value_text` and the artifact, with a bounded `value_string`.
* **API/UI**: every route except `/healthz` and login requires auth (route-enumeration test); upload validation errors; document list/detail served from SQL; replay endpoint conflicts; originals streamed only when authenticated.
* **Setup-command order** (unit, against the simulated CU server): with defaults unset and no `--set-defaults`, `setup-analyzers` stops before issuing any analyzer `PUT`; with the flag it patches defaults first, then registers.
* **Startup/build**: package imports, `docdesk migrate` on an empty DB (and re-run is a no-op), `docdesk web` boots with fakes, Dockerfile lint/syntax (build if a builder exists).
* **Test data**: `tools/gen_testdocs.py` (reportlab/fpdf2 + Pillow) generates deterministic synthetic fictional documents: invoice PDF, NDA PDF, SOW or MSA PDF, plus optional PO, receipt, bank statement, an "other" (non-business flyer), an invoice TIFF, and invalid inputs (text renamed `.pdf`, a `.docx`, a zero-byte file, an oversized file). `docs/test-documents.md` lists the expected *qualitative* observations and states plainly that results are model-dependent and no accuracy is claimed.

### 14.3 Not required at this tier
Load/stress tests, repeated process-kill experiments, multi-configuration matrices, exhaustive field-level accuracy testing.

### 14.4 Azure smoke test (documented; run only with authorised resources)
`docdesk smoke --base-url https://<web-fqdn>` (password from env/prompt, Bearer auth): uploads at least two generated documents of different types (and the invalid file), polls `/api/documents/{id}` until terminal, prints per document: category, analyzer/API/config versions, which fields were present/absent, `_simulated` absence, the raw-response artifact size, and the stage timeline; fails if any result is marked simulated. Optionally also `docdesk smoke --direct-cu`, which calls the classifier analyzer directly once to prove the CU endpoint/role independent of the pipeline. The Engineer's report must state separately: *ran locally against real SQL Server*, *simulated: Blob/Service Bus/Event Grid/CU*, *ran in Azure* (probably nothing unless credentials are supplied), and *remaining configuration*.

## 15. Open questions and risks (none block implementation)
1. **Region/model availability** for CU in the sponsor's region and the Foundry deployment names are deployment-time facts; not verified here. Document how to check the CU language/region page.
2. **Inline base64 request size**: the documented async file limit is 200 MB, but the maximum JSON body for `data` was not confirmed; hence the 20 MB default. If rejected, switch to the optional URL path (user-delegation SAS + CU network access to Storage) behind the same client interface.
3. Whether `estimateFieldSourceAndConfidence` requires `returnDetails: true` to return `source`/`confidence`, and how large raw responses get with it, must be observed live; the registry makes both switches one-line changes (bump config version).
4. The exact built-in role that allows analyzer authoring and defaults `PATCH` on the Foundry/CU resource (vs. `Cognitive Services User` for analyze only) should be confirmed at deployment.
5. The exact body format Event Grid writes to a Service Bus queue (single object vs array; Event Grid vs CloudEvents schema) was not observed; the arrival parser accepts all four and logs the shape (not content). Worth confirming in the smoke test.
6. The routed-result shape (K-006) was observed for a single one-page PDF; behaviour for multi-page documents or for `other` (no routed item) is assumed from the documented model and handled defensively. Classification confidence is unavailable in GA when segmentation is off; if the sponsor needs it, enabling `enableSegment` is a v2 option.
7. Throttling quota (documented 1000 pages/min, 3000 ops/min per resource) is far above this app's expected use; `CU_MAX_CONCURRENCY` caps it anyway.
8. Single-password auth with a per-process login throttle is brute-forceable across replicas at a slow rate; the Key Vault password should be long/random. Accepted given the contract's no-user-management scope.
9. Replay is offered only for `failed` documents in v1 (contract wording); extending to `completed`/`rejected` is a small change in the replay guard.
