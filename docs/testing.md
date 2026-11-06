# Testing and evidence

Verification tier: **standard**. This page states what each kind of test exercised, so nothing simulated is mistaken for a live Azure run.

## What is real and what is simulated

| Dependency | In the automated tests | In a real deployment |
|---|---|---|
| SQL | **Real SQL Server 2022 Developer** (`READ_COMMITTED_SNAPSHOT ON`, as in Azure SQL Database), one throw-away database per test session. The T-SQL used (`UPDATE ... OUTPUT`, `UPDLOCK/READPAST/READCOMMITTEDLOCK`, filtered indexes, ownership chaining) is the same on both engines. | Azure SQL Database with Entra tokens - **not exercised** (token login and service-tier behaviour are untested) |
| Blob Storage | Filesystem fake (`devtools/fake_blob.py`) for pipeline tests; the **real `azure-storage-blob` adapter against the Azurite emulator** (1 test, optional, needs `AZURITE_BLOB_URL`) | Azure Blob with managed identity - **not exercised** |
| Event Grid | **Simulated**: schema-correct `BlobCreated` JSON injected into the in-memory `blob-events` queue (`devtools/events.py`) | **not exercised** |
| Service Bus | **Simulated** in memory (`devtools/fake_broker.py`): at-least-once delivery, visibility lock, redelivery on abandon, delivery counting and dead-lettering, hooks for outage, send failure, duplicate delivery. The Azure SDK adapter (`adapters/broker.py`) is **not** exercised | **not exercised** |
| Content Understanding | **Simulated** WSGI service (`devtools/sim_cu.py`) shaped like the documented/observed 2025-11-01 API (202 + `Operation-Location`, polling, routed result items without `mimeType`, 429/5xx/Failed/empty/malformed/never-finishing responses). Output is keyword-rule based and stamped `_simulated` | **not exercised** - no live classification or extraction has been observed |
| Key Vault | A fetch callable (`PasswordProvider`) is faked | **not exercised** |
| Container image / Container Apps | wheel built and rendered from a clean venv; Dockerfile linted with hadolint | image **not built**, apps **not run** |

## Test suite

`python -m pytest` (see README for the environment): 157 pass (84 unit, 72 integration on real SQL, 1 Azurite), 1 skipped (the live Azure smoke test, which needs `DOCDESK_LIVE=1`).

| File | Proves |
|---|---|
| `unit/test_registry_and_analyzers.py` | registry -> analyzer definitions: valid against a local model of the documented schema, `other` present and unrouted, routing targets exist, ids match the regex, hash stable and version-sensitive, a version bump never reuses ids, empty-object comparison rule |
| `unit/test_normalize.py`, `test_result_schema.py` | normalizer against synthetic provider responses: present/absent/invalid, routed vs root items, empty/unusable/malformed responses, unknown or missing category, text fallback, confidence/source passthrough or `null`, long values kept whole, schema validity |
| `unit/test_cu_client_and_setup.py` | analyzers that match but are not `ready` fail setup and `--check` (in-progress ones are waited for only outside `--check`); client error mapping (429, 503, 400, 403, Failed content/transient, empty body, malformed JSON, timeout, network); setup command order: defaults before any analyzer request, `--check` never writes, repeat is a no-op, drift fails |
| `unit/test_auth_routes.py` | every route except `/healthz` and sign-in refuses anonymous access (enumerated from the route table); cookie hardening; session tamper/expiry/password rotation; CSRF and Origin; throttle; password sources |
| `unit/test_core_units.py` | back-off, error catalogue, Event Grid/CloudEvents parsing, upload validation, config guards (dev-only options rejected in prod), log allow-list |
| `integration/test_pipeline_*.py` | analysis provenance (analyzer ids, config version/hash) is the configuration that made the request, even when the indexing worker runs a newer one or an old response is re-indexed; end-to-end flows on real SQL: several types, `other`, TIFF, artifacts addressable and hashed, raw response byte-exact, intake rejections |
| `integration/test_idempotency_concurrency.py` | **REQ-009/011/012**: duplicate and concurrent arrival -> one attempt; duplicate/late events never move a settled document; the same stage message delivered twice or to eight workers at once -> one effect; stale-lease completion rejected; dispatcher claims disjoint rows on an RCSI database; send failure, broker outage, crash between publish and mark -> eventually delivered, state unchanged |
| `integration/test_failures_and_recovery.py` | the final Failed/unreadable provider response is retained byte-exact as an artifact; **REQ-007/010**: transient errors retried then succeed; exhaustion -> `failed` with full try history and no further calls; terminal errors not retried; handler bug recorded and bounded; worker crash -> lease expiry -> reaper -> re-run; crash loop exhaustion; lost stage message recovered; lost arrival event recovered from the blob; blob write with lost acknowledgement / failure / unknown outcome |
| `integration/test_replay.py` | replay of a `result`-stage failure (default and explicit) completes via the carried `index` stage with no new analysis; **REQ-010/013**: replay creates attempt 2 and leaves attempt 1 byte-for-byte intact; replay from `index` makes no provider call; concurrent replays have one winner; replay works through the restricted web SQL role while direct writes by that role are denied |
| `integration/test_workers_and_privacy.py` | **REQ-002/012/018**: three real `Worker` instances with threads over 13 documents, one permanently throttled - all others complete exactly once, also under duplicate delivery; health probes record a dependency failure; logs contain no filenames, text or field values |
| `integration/test_web_api.py` | upload errors (415/400/413/502), list/filter/detail, cursor pagination that enumerates every row exactly once when timestamps tie from SQL, original and artifact streaming only when authenticated, replay endpoint statuses, diagnostics, HTML escaping |
| `integration/test_migrate_and_long_values.py` | migration on an empty database and re-run no-op; roles, procedure and no `DELETE` for the worker role; 900/1300/5000-character clauses persist in full with a bounded index key |
| `integration/test_blob_adapter_azurite.py` | Azure Blob SDK adapter vs the emulator: create-only put, get/head/stream, missing blob, overwrite |
| `unit/test_smoke_checker.py` | the smoke checker itself (stub HTTP): fails on missing test files, on a completed document without a result, on simulated or unretained provider evidence, and with fewer than two valid documents |
| `live/test_azure_live.py` | the Azure smoke test (skipped unless configured) |

## Not covered (outside the tier, or impossible here)

Load/stress, repeated process-kill experiments, multi-configuration matrices, field-level accuracy against real documents,
anything requiring Azure, Docker or a browser (the UI was checked by rendering its HTML server-side and by exercising the same
API it calls; its JavaScript, drag-and-drop and polling were **not** executed in a browser).
