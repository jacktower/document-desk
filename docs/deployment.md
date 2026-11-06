# Deployment guide

This guide tells whoever provisions Azure what Document Desk needs. **No infrastructure-as-code or pipeline is delivered here.**
Nothing in this guide has been executed against Azure; the steps are derived from the application's code and the Microsoft
documentation it uses, and the list of unverified items is in [section 9](#9-what-is-not-verified).

## 1. Azure resources

| Resource | Requirements |
|---|---|
| **Storage account** (Blob) | Containers `originals` (written by `web`) and `artifacts` (written by `worker`). HTTPS only, TLS 1.2+, shared-key access can be disabled (the app uses Entra identities). Recommended: blob versioning and soft delete, and lifecycle rules chosen by the owner (see section 8). |
| **Event Grid system topic** on the storage account | One event subscription: event type `Microsoft.Storage.BlobCreated` only; subject begins with `/blobServices/default/containers/originals/`; destination = Service Bus queue `docdesk-blob-events`, delivered **with the system topic's managed identity**. The app accepts the Event Grid and CloudEvents schemas, as a single event or an array. Do not subscribe the `artifacts` container (it would feed the pipeline back to itself). If a dead-letter container is used, the topic identity needs Storage Blob Data Contributor **at storage-account scope** (container scope is not enough). |
| **Service Bus namespace** (Standard or Premium) | Five queues with the prefix `docdesk-` (configurable by `QUEUE_PREFIX`): `blob-events`, `stage-intake`, `stage-analyze`, `stage-index`, `stage-result`. For each: lock duration 5 min, max delivery count 10, dead-lettering on message expiry, TTL 14 days, duplicate detection on (10 min; defence in depth only, the app never relies on it), sessions/partitioning off. The application does **not** use scheduled messages: retry delays are held in the SQL outbox. |
| **Azure SQL Database** | One database. Microsoft Entra authentication for the application identities. Apply the schema with the `migrate` command (section 6). Pick any region where SQL provisioning is allowed for the subscription; it need not be the same region as the other resources. |
| **Key Vault** | One secret holding the operator password (long and random). RBAC authorization model. |
| **Microsoft Foundry resource** (Cognitive Services / AI Services account) | In a region where **Azure Content Understanding** is available (check Microsoft Learn, "Content Understanding language and region support", for the current list). Two model deployments in that resource: a chat/completion model (default `gpt-4.1`) and an embedding model (default `text-embedding-3-large`). Capacity must cover the expected analysis volume. |
| **Container Apps environment** + Log Analytics | `web` app, `worker` app, and two on-demand jobs (`migrate`, `setup-analyzers`), all from the one image built by `Dockerfile`. |
| **Managed identities** | Recommended: one user-assigned identity each for `web`, `worker` and the jobs (set `AZURE_CLIENT_ID` on each app). |

### Container Apps settings

* **web**: external ingress, target port **8000**, `allowInsecure=false` (TLS is terminated by the ingress; the app sets `Secure` cookies and honours `X-Forwarded-*`), liveness/readiness probe `GET /healthz` (the only unauthenticated route, returns a constant). Any number of replicas.
* **worker**: **no ingress**, **minimum replicas >= 1** (the outbox dispatcher, reaper and health probes live in the worker). Any number of replicas may run; they coordinate only through SQL. Optional scale rule on the `docdesk-stage-analyze` queue length. Container command: `worker`.
* **jobs**: same image, command `migrate` and `setup-analyzers` (arguments below), manual trigger.
* Resource sizing is not certified (no load test was run); one small replica each is enough for functional use.

## 2. Identities and roles

| Identity | Needs |
|---|---|
| `web` | **Storage Blob Data Contributor** on the storage account (uploads originals, streams originals/artifacts). **Key Vault Secrets User** on the operator-password secret (only if the password is read from Key Vault by the app; with a Container Apps Key Vault secret reference the *app's identity* needs this role instead). **SQL user** that is a member of `docdesk_web_role` (read all tables; insert/update on `documents` only; EXECUTE on `dbo.usp_replay_document`). It needs no Service Bus, Content Understanding or direct write access to attempts/stages/outbox. |
| `worker` | **Storage Blob Data Contributor**; **Azure Service Bus Data Receiver and Data Sender** on the namespace; **Cognitive Services User** on the Foundry resource (analyze calls); **SQL user** in `docdesk_worker_role` (select/insert/update on all tables, no DDL, no delete). |
| `migrate` / `setup-analyzers` jobs | `migrate`: SQL rights to create tables, procedures and roles (for example `db_owner` on this database). `setup-analyzers`: an Entra role on the Foundry resource that permits **authoring** (creating analyzers and setting model defaults); the exact built-in role was not confirmed (see section 9) - `Cognitive Services User` is documented for analysis only. |
| Event Grid system topic identity | **Azure Service Bus Data Sender** on the `docdesk-blob-events` queue (or namespace). |
| Operator (human) | Key Vault Secrets User, only to read the password (copy it, do not display it). **Do not make a person the Entra administrator of the SQL server** to connect from a tool: the setup identity then stops being administrator and the next `migrate` fails with `Login failed`. Use a read-only database user for inspection. |

SQL users for managed identities are created **after** `migrate` (the roles are created by the migration) and must be named after
the *identity*, never after the roles (users and roles share one name space in SQL):

```sql
CREATE USER [<web identity resource name>] FROM EXTERNAL PROVIDER;
ALTER ROLE docdesk_web_role ADD MEMBER [<web identity resource name>];
CREATE USER [<worker identity resource name>] FROM EXTERNAL PROVIDER;
ALTER ROLE docdesk_worker_role ADD MEMBER [<worker identity resource name>];
```

## 3. Content Understanding and Foundry prerequisites

* API version **`2025-11-01`** (GA), set by `CU_API_VERSION` (default).
* Analyzers are generated from `src/docdesk/cu/registry.py`: **one classifier** (`docdesk-classifier-v<N>`, `contentCategories` for the 12 business types plus `other`, segmentation off) that routes each category to one of **five extractors** (`docdesk-transaction|statement|agreement|tax|insurance-v<N>`), all based on `prebuilt-document`. `other` has no extractor. `N` is `ANALYZER_CONFIG_VERSION` (default `1`).
* **An analyzer definition under an existing id is never changed.** To change the registry, bump `ANALYZER_CONFIG_VERSION`; `setup-analyzers` then registers a new set under new ids. Results record the analyzer ids, API version, config version and a hash of all definitions.
* **Model defaults must exist before any analyzer can be registered.** Content Understanding rejects `prebuilt-document` based analyzers with `DefaultDeploymentModelNotFound` until the resource has default deployments for the completion model and for the embedding alias **`prebuilt-analyzer-embedding`** (even though the application does not use embeddings). `setup-analyzers` does this in a fixed order:
  1. **defaults phase** (no analyzer request is made before it passes): reads `GET /contentunderstanding/defaults` (a `400` with inner code `DefaultsNotSet` means "none yet"); if `gpt-4.1` (or `CU_COMPLETION_MODEL`) or `prebuilt-analyzer-embedding` is missing it **stops with a diagnosis** unless `--set-defaults` is given, in which case it PATCHes them from `CU_MODEL_DEPLOYMENTS` (which must contain the embedding alias) and reads them back;
  2. **analyzer phase**: for each analyzer (extractors, then classifier) it reads the existing one; absent -> creates (`allowReplace=false`) and waits for `ready`; present and equal to the authored definition (an authored empty object equals its absence) -> skipped; present but different -> fails and tells you to bump `ANALYZER_CONFIG_VERSION`.
  `--check` never writes. The command is safe to repeat.
* Example: `CU_MODEL_DEPLOYMENTS='{"gpt-4.1":"<your gpt-4.1 deployment name>","prebuilt-analyzer-embedding":"<your embedding deployment name>"}'`.
* **Billing** (no estimate is made here): Content Understanding bills per analysed page for the document analyzers, plus token consumption on your completion and embedding deployments, plus contextualization tokens when field source/confidence estimation is on (`CU_ESTIMATE_SOURCE_CONFIDENCE`, default on). A retry after a timeout starts a new, billable analysis; retries are bounded (5 tries for `analyze`).
* **Input size**: originals are sent to the service inline as base64. The default upload limit is 20 MB (`MAX_UPLOAD_MB`) and 100 pages (`MAX_PAGES`). The service's own size/page limits vary by tier and API; whether the request-body ceiling is above 20 MB was not verified. Files above the limits are rejected clearly at upload or intake; nothing is promised beyond them.

## 4. Environment variables

Everything is configured through environment variables; there are no config files. Secrets are never baked into the image.

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `SQL_SERVER` | all | - | e.g. `<server>.database.windows.net` |
| `SQL_DATABASE` | all | - | database name |
| `STORAGE_ACCOUNT_URL` | web, worker | - | `https://<account>.blob.core.windows.net` |
| `SERVICEBUS_NAMESPACE` | worker | - | fully qualified namespace, `<ns>.servicebus.windows.net` |
| `CU_ENDPOINT` | worker, setup | - | Foundry resource endpoint |
| `OPERATOR_PASSWORD` | web | - | the operator password, injected as a Container Apps secret that references Key Vault |
| `KEY_VAULT_URL`, `OPERATOR_PASSWORD_SECRET_NAME` | web | - | alternative: the app reads the secret from Key Vault with its identity (cached 5 min). One of the two sources is required; startup fails otherwise |
| `AZURE_CLIENT_ID` | all | - | client id of the user-assigned identity |
| `ORIGINALS_CONTAINER`, `ARTIFACTS_CONTAINER` | web, worker | `originals`, `artifacts` | |
| `QUEUE_PREFIX` | worker | `docdesk-` | |
| `ANALYZER_CONFIG_VERSION` | worker, setup | `1` | suffix of the analyzer ids |
| `CU_API_VERSION` | worker, setup | `2025-11-01` | |
| `CU_COMPLETION_MODEL`, `CU_EMBEDDING_MODEL` | worker, setup | `gpt-4.1`, `text-embedding-3-large` | model names used in the analyzer definitions |
| `CU_MODEL_DEPLOYMENTS` | setup | - | JSON `{model-or-alias: deployment name}` for `--set-defaults` |
| `CU_POLL_TIMEOUT_S` / `CU_MAX_CONCURRENCY` | worker | `600` / `4` | per-analysis timeout / concurrent analyses per worker replica |
| `CU_RETURN_DETAILS` / `CU_ESTIMATE_SOURCE_CONFIDENCE` | worker, setup | `false` / `true` | part of the analyzer definition and its hash; changing them needs a config-version bump |
| `MAX_UPLOAD_MB` / `MAX_PAGES` | web, worker | `20` / `100` | limits |
| `RETENTION_DAYS` | web, worker | `2555` | informational `retain_until`; **nothing is purged** (see section 8) |
| `LOW_CONFIDENCE_THRESHOLD` | worker | `0.5` | display heuristic for an informational flag, not a calibrated score |
| `MAX_TRIES_INTAKE/ANALYZE/INDEX/RESULT` | worker | `3/5/3/3` | bounded retries per stage |
| `LEASE_SECONDS` / `LEASE_SECONDS_ANALYZE` | worker | `300` / `600` | stage lease length (renewed while polling) |
| `ARRIVAL_GRACE_S` / `UPLOAD_TIMEOUT_S` / `STAGE_RECOVERY_S` | worker | `300` / `900` / `900` | reaper thresholds |
| `DISPATCH_POLL_S`, `REAPER_POLL_S`, `HEALTH_POLL_S` | worker | `1`, `30`, `60` | loop intervals |
| `WORKER_CONC_ARRIVAL/INTAKE/ANALYZE/INDEX/RESULT` | worker | `2/4/4/4/2` | consumer threads per queue per replica |
| `SESSION_HOURS` | web | `8` | session lifetime |
| `LOG_LEVEL` | all | `INFO` | |
| `APP_ENV` | all | `prod` | `dev` only for local runs; with `prod` the dev-only options below are rejected at startup |
| `SQL_AUTH=sql_password`, `SQL_USER`, `SQL_PASSWORD`, `SQL_TRUST_SERVER_CERTIFICATE`, `CU_AUTH=none`, `COOKIE_SECURE=false` | dev only | | local runs only |

## 5. Transport security and sign-in

TLS terminates at the Container Apps ingress. SQL connections use ODBC Driver 18 with `Encrypt=yes` and certificate validation;
Storage, Service Bus, Key Vault and Content Understanding are HTTPS/AMQP-over-TLS. All Azure services are reached with managed
identities (SQL via an Entra access token, no keys, no connection strings). The web interface and every API route require the
operator password: the browser signs in at `/login` (signed, `HttpOnly`, `Secure`, `SameSite=Strict` cookie, 8 h; CSRF header
required for state changes) and scripts send `Authorization: Bearer <password>`. Rotating the secret invalidates all sessions
(within the 5-minute cache if the app reads Key Vault directly; immediately on restart if the value is injected as an environment
variable). A per-process failure throttle slows guessing; with several replicas it is only best-effort, so use a long random password.

## 6. Order of operations

1. Provision the resources and identities in sections 1-2 (Terraform or otherwise - outside this repository).
2. Run the **`migrate`** job (`docdesk migrate`). It is idempotent and takes a SQL application lock, so concurrent runs are safe.
3. Create the SQL users for the `web` and `worker` identities (section 2).
4. Run the **`setup-analyzers`** job: first `docdesk setup-analyzers --check` (read-only), then `docdesk setup-analyzers --set-defaults` (with `CU_MODEL_DEPLOYMENTS` set) the first time, plain `docdesk setup-analyzers` afterwards. Exit code 0 = ready, 2 = a prerequisite (model defaults / configuration) is missing, 1 = an analyzer problem. The output names the missing item and the fix.
5. Start `worker`, then `web` (either order works; the outbox holds work until the worker is up).
6. Run the smoke test (section 7).

## 7. Azure smoke test

Run from any machine that can reach the web app (the documents come from `tools/gen_testdocs.py`):

```bash
python tools/gen_testdocs.py testdocs
SMOKE_OPERATOR_PASSWORD='<password>' docdesk smoke --base-url https://<web fqdn> --docs testdocs
# optional: call the classifier analyzer directly, bypassing the pipeline (needs CU_ENDPOINT and an identity allowed to analyze)
CU_ENDPOINT=https://<foundry endpoint> docdesk smoke --direct-cu --docs testdocs
```

It uploads an invoice, an NDA, an unrelated flyer and an invalid file, polls until each is terminal, and prints for each: status,
category, provider/API/analyzer/config versions, the fields found and not found, the size of the retained raw provider response, and the stage timeline. It **fails** if any
result is marked simulated, if a valid document does not complete, if the raw response was not retained, or if anonymous access to the
API is not refused. It never asserts a particular classification: results are model-dependent. The same check is available as
`DOCDESK_LIVE=1 LIVE_BASE_URL=... LIVE_OPERATOR_PASSWORD=... pytest tests/live`. Then inspect the documents in the web UI and the `artifacts` container.

## 8. Retention

`RETENTION_DAYS` fills `documents.retain_until` (a date, informational). **The application never deletes anything**: failed inputs,
every attempt, every try and every artifact are kept. The default of 2555 days is a placeholder, not a compliance determination.
Recommended for the infrastructure owner: enable blob soft delete and versioning, define lifecycle rules for `originals` and
`artifacts` that match the owner's own retention policy, and back up the database with the service's point-in-time restore.

## 9. What is not verified

* Everything in this guide against a real Azure subscription: identity role assignments, Event Grid delivery to Service Bus (the exact body Event Grid writes - single object vs array, Event Grid vs CloudEvents schema - was not observed; the parser accepts all four and the smoke test confirms it), SQL Entra-token login, Key Vault secret references, Container Apps probes and scale rules.
* Content Understanding behaviour: the analyzer definitions were validated locally against a model of the documented schema only; the first live `setup-analyzers` run is the authoritative check. Not observed live: the routed-result shape for multi-page documents and for `other` (the normalizer is defensive about both); whether source/confidence estimation requires `returnDetails`; how large raw responses get; the maximum inline request size.
* The exact built-in role that permits analyzer authoring and defaults `PATCH` on the Foundry resource.
* The Docker image build (no Docker in the development sandbox).

## 10. Known limitations

One category per file (mixed packets are classified as a single document; segmentation is off); 20 MB / 100 page defaults; the login throttle is per replica and keyed on the client address the ingress forwards (the web container trusts `X-Forwarded-For`, so it must only be reachable through the Container Apps ingress); replay is offered only for `failed` documents (not `completed` or `rejected`); contract clause fields are quoted text only and informational, never legal interpretation; taxpayer identifiers (SSN/EIN) are deliberately not extracted; Service Bus lock renewal is not used (the SQL lease is authoritative, so a message whose lock expires mid-analysis is redelivered and discarded harmlessly).
