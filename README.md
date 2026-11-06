# Document Desk

An event-driven document processing application for small-business paperwork. An operator signs in, uploads a
nonconfidential **PDF or TIFF**, and the system stores it, processes it asynchronously, classifies it and extracts
metadata with **Azure Content Understanding**, and shows the result, the extracted text and the full processing history in a
web interface backed by **Azure SQL Database**. It records document metadata only: it does not post transactions,
approve agreements or make financial or legal determinations.

## What this repository delivers, and what is only for local use

| Part | Status |
|---|---|
| Container image (`Dockerfile`): `web`, `worker`, `migrate`, `setup-analyzers`, `smoke` commands | **Deployable.** Built and run on Azure Container Apps by a separate infrastructure/pipeline project (none is included here). The image build itself was **not** run (no Docker in the development sandbox). |
| SQL schema (`migrations/`), Content Understanding analyzer definitions (generated from `src/docdesk/cu/registry.py`), the web UI/API, the workers | Deployable code, verified locally as described below. |
| `devtools/` (in-memory Service Bus, filesystem Blob, **simulated** Content Understanding, local stack) and `tests/` | **Local trial and testing only.** Never part of the image. |
| `Dockerfile.dev` | Recreates the *development and test environment* (SQL Server, Python 3.12, Azurite). It is not a deployment image. |

**Hard requirements** for a real deployment (details in [`docs/deployment.md`](docs/deployment.md)): Azure Blob Storage, Event Grid,
Service Bus (Standard or Premium), Azure SQL Database, Key Vault, a Microsoft Foundry resource in a region that supports
Content Understanding with `gpt-4.1` and `text-embedding-3-large` deployments, and Azure Container Apps. Local runs need a SQL Server
2022 instance and the ODBC Driver 18; nothing else.

## Verification status (read this before relying on it)

| Evidence | Result |
|---|---|
| Unit tests (no services) | 74 passing |
| Integration tests on a **real SQL Server 2022** (RCSI on, as in Azure SQL), with simulated Blob, Service Bus, Event Grid and Content Understanding | 66 passing: duplicate delivery, concurrent workers, stale leases, outbox crash windows, bounded retries, lease expiry/reaper, replay, restricted web SQL role, long values, privacy of logs |
| Azure Blob adapter against the **Azurite emulator** | 1 passing (emulator only; managed-identity auth is not exercised) |
| Local stack + `docdesk smoke` against it | Works; the smoke test correctly *fails* on simulated results |
| **Real Azure Content Understanding, Event Grid, Service Bus, Blob (managed identity), Azure SQL (Entra token), Container Apps** | **Not exercised.** No Azure access was available. The live smoke test (`docdesk smoke`, `tests/live/`) exists and is documented but has never been run against Azure. The analyzer definitions have been validated only by a local model of the documented schema; the authoritative check is the first `docdesk setup-analyzers` run. |
| Docker image build | **Not run** (no Docker). Dockerfile syntax was linted with hadolint; the Python package was built as a wheel and imported/rendered from a clean virtual environment. |

Classification and extraction quality is **model-dependent and unmeasured**; no accuracy is claimed. The simulated provider only
applies keyword rules to generated documents and exists to test the plumbing. See [`docs/testing.md`](docs/testing.md).

## Map of the repository

```
Dockerfile            runtime image (web | worker | migrate | setup-analyzers | smoke)
Dockerfile.dev        development/test environment (not for deployment)
pyproject.toml        package and test configuration
migrations/           forward-only T-SQL schema (tables, roles, replay stored procedure)
src/docdesk/          application: config, db/, domain/, adapters/, cu/ (Content Understanding), pipeline/ (workers), web/
devtools/             local fakes + `docdesk_devtools.stack` (local simulated stack) - not shipped
tests/                unit/, integration/ (real SQL), live/ (Azure smoke, skipped by default)
tools/gen_testdocs.py reproducible synthetic test documents and invalid inputs
docs/                 design.md, deployment.md, operations.md, result-schema.md, test-documents.md, testing.md
```

## Install, configure, run locally (ordered path from an empty machine)

Verified on Ubuntu 22.04 unless noted.

1. **Prerequisites.** Python 3.12; ODBC Driver 18 for SQL Server (`msodbcsql18`) and `unixodbc-dev`; a SQL Server 2022 instance you
   can reach as an administrator (the integration tests and the local stack create their own databases). Recipe used here: install
   the Microsoft `mssql-server` package and run `sqlservr` directly (see the comments in `Dockerfile.dev`).
2. **Install the project.**
   ```bash
   python3.12 -m venv .venv && . .venv/bin/activate
   pip install -e '.[dev]'
   ```
3. **Run the tests.**
   ```bash
   export TEST_SQL_SERVER=127.0.0.1,1433 TEST_SQL_PASSWORD='<sa password>'      # real SQL Server for integration tests
   # optional: the blob-adapter test against Azurite (blob emulator)
   # azurite-blob --skipApiVersionCheck --blobPort 10010 --location /tmp/azurite-data --silent &
   # export AZURITE_BLOB_URL=http://127.0.0.1:10010
   python -m pytest
   ```
   Without `TEST_SQL_SERVER`/`TEST_SQL_PASSWORD` the integration tests are *skipped with a reason*, not passed.
4. **Try it locally (simulated provider).**
   ```bash
   python tools/gen_testdocs.py testdocs
   PYTHONPATH=devtools python -m docdesk_devtools.stack --port 8000      # needs TEST_SQL_SERVER / TEST_SQL_PASSWORD
   ```
   Open <http://127.0.0.1:8000>, sign in with the printed operator password (`dev-password` unless `DEV_OPERATOR_PASSWORD` is set), and
   upload files from `testdocs/`. Every result is labelled **SIMULATED PROVIDER**.
5. **Deploy to Azure.** Follow [`docs/deployment.md`](docs/deployment.md) (resources, identities and roles, environment variables, the
   `migrate` -> `setup-analyzers` order, and the smoke test). Provisioning is outside this repository.

## Configuration

All configuration is environment variables; secrets come from Key Vault. The full table is in
[`docs/deployment.md`](docs/deployment.md#4-environment-variables). Required: `SQL_SERVER`, `SQL_DATABASE`, `STORAGE_ACCOUNT_URL`,
`SERVICEBUS_NAMESPACE` (worker), `CU_ENDPOINT`, and either `OPERATOR_PASSWORD` (a Container Apps Key Vault secret reference) or
`KEY_VAULT_URL` + `OPERATOR_PASSWORD_SECRET_NAME`; plus `AZURE_CLIENT_ID` when a user-assigned identity is used.

## Design in one paragraph

SQL is the only authority. Every stage transition is one SQL transaction that also writes the next stage's **outbox** row; a
dispatcher publishes outbox rows to Service Bus, so a crash or broker outage can never lose a committed next-stage event.
Stage ownership is a SQL lease with fencing, so duplicate or concurrent deliveries cannot duplicate effects. Replay is a stored
procedure that adds a new attempt and never changes earlier ones. See [`docs/design.md`](docs/design.md) (including section 16,
which records where the implementation differs from the original design).
