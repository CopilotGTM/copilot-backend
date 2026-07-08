# CRM CSV Upload Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let authenticated CRM users upload one CSV, preview deterministic account/contact/deal mapping, fix the mapping, and run an async import that writes through the existing CRM account/contact/deal services.

**Architecture:** V1 is not a generic ETL builder. It reuses `deal_jobs` as the parent job ledger, adds one import-specific row-outcome table, uses stdlib CSV parsing, direct-to-Azure upload, and deal-worker tasks that call the canonical `AccountWriteService`, `ContactWriteService`, and `DealWriteService`. The implementation must preserve the current folder structure: CRM row models stay under `src/domain/models/organisation_tables/sync/crm/`, CRM import service code stays under `src/service/crm/imports/`, CRM routers stay under `src/api/routers/crm/`, and async work stays in `src/workers/deal_worker/tasks/`.

**Tech Stack:** FastAPI, SQLAlchemy/SQLModel, Alembic org migrations, Azure org blob storage, Celery deal worker, Python stdlib `csv`, existing CRM account/contact/deal write services.

---

## Current-State Review

The current tree has only a disabled deal-import compatibility surface:

- `src/api/routers/crm/deal_import_handler.py` returns 409 "coming soon" for `/users/crm/deals/import/...`.
- `tests/api/routers/crm/test_deal_import_handler.py` verifies the disabled behavior.
- `src/api/fastapi_app.py` mounts that router at `/users/crm/deals`.

The production write paths already exist and should be reused:

- `src/service/crm/accounts/write_service.py` owns account create/update, duplicate protection, owner attribution, mutation events, and account activity projections.
- `src/service/crm/contacts/write_service.py` owns contact create/update, duplicate protection, owner attribution, mutation events, and contact activity projections.
- `src/service/crm/accounts/contracts.py` and `src/service/crm/contacts/contracts.py` are the correct place for tiny command/context additions if the importer needs a service-level API extension.

The upload/storage primitive already exists but needs one importer-specific path helper:

- `src/infra/storage/azure_org_blob_storage.py` has SAS generation and filename sanitization.
- Its current `plan_blob_path(...)` returns `chat_uploads/...`, so CRM import code must not reuse that method directly.

The async job header already exists and should be reused:

- `src/domain/models/organisation_tables/job_models.py` defines `DealJobs`, `DealJobType`, and `JobStatus`.
- `deal_jobs` already supports org-scoped jobs with nullable `deal_id`, JSONB `job_metadata`, JSONB `result_data`, status, retry count, user id, and SSE-compatible status tracking.
- For CRM CSV import, `deal_jobs.job_id` is the import id returned by the API.

## Loopholes To Close Before Implementation

- **Wrong endpoint risk:** The old endpoints are deal-specific and remain disabled. Deal-capable CSV import still uses the generic `/users/crm/imports/csv` endpoint.
- **Wrong blob path risk:** Reusing `plan_blob_path(...)` would store CRM import CSVs under `chat_uploads/...`. Add `plan_crm_import_path(...)` to the storage helper.
- **Wrong account-link risk:** `ContactWriteService.create_contact(...)` currently links accounts from the contact email domain only. CSV rows with explicit account columns need a minimal service-level path to create/resolve that account and link a new contact to it.
- **Duplicate task risk:** `start` and worker tasks must be idempotent. A repeated start or retry cannot insert duplicate `crm_import_rows` or enqueue multiple import processors for the same `deal_jobs.job_id`.
- **Worker ownership lookup risk:** Worker tasks receive only `organisation_id` and `import_job_id`, so worker execution must load the `DealJobs` row by org/import id and then use the stored `job.user_id` as the actor. Do not require the Celery payload to carry user id.
- **API task split risk:** Task 8 wires the import router and core lifecycle only. Row pagination and error CSV are implemented in Task 9, so Task 8 tests must not require final row/error reporting behavior before that service surface exists.
- **Agent overreach risk:** Agent/LLM mapping, if added later, may only suggest mappings. It must never execute imports or receive full CSV contents.
- **Formula injection risk:** Error CSV export must sanitize cells starting with `=`, `+`, `-`, or `@`.
- **PII logging risk:** Logs must contain import id, org id, user id, counts, status, and error classes only. Do not log raw row values.
- **Schema drift risk:** Reuse `DealJobs.status` for the parent lifecycle and use plain text status constants for row outcomes. Avoid Postgres enum types until row statuses are proven stable.
- **Transaction risk:** Existing write services do not own commits. The importer owns commit/rollback per row or small batch.
- **Test confidence risk:** Focused unit tests are not enough for schema work. Run the migration head check before calling this production-ready.

## V1 Product Boundary

Supported:

- One CSV per import.
- Account-only rows.
- Contact-only rows.
- Contact-plus-account rows.
- Deal-only rows.
- Deal-plus-account rows.
- Deal-plus-contact rows.
- Deal-plus-account-plus-contact rows.
- Create missing records.
- Skip existing records by default.
- Optional `update_existing=false` field in the API contract now, but V1 implementation keeps it false-only unless a later task explicitly implements updates.

Not supported in V1:

- Tasks, notes, leads, custom objects, custom fields, formulas, multi-file matching, rollback UI, owner assignment, or pipeline/stage selection by CSV.
- Streaming giant files. V1 caps imports at 25,000 rows and 10 MB.
- Full agent mapping. Keep the interface optional and deterministic-first.

`copilotgtm:` V1 deliberately caps imports at 25k rows or 10 MB. If customers need larger imports, upgrade the parser to stream Azure blob chunks into row batches and add resumable batch checkpoints.

## API Contract

New general CRM import endpoints live under `/users/crm/imports/csv`.

- `POST /users/crm/imports/csv/presign`
- `POST /users/crm/imports/csv/{import_id}/finalize`
- `GET /users/crm/imports/csv/{import_id}`
- `PATCH /users/crm/imports/csv/{import_id}/mapping`
- `POST /users/crm/imports/csv/{import_id}/start`
- `GET /users/crm/imports/csv/{import_id}/rows`
- `GET /users/crm/imports/csv/{import_id}/errors.csv`

Keep `src/api/routers/crm/deal_import_handler.py` disabled in this plan. Do not silently repurpose `/users/crm/deals/import/...`; account/contact/deal CSV import uses `/users/crm/imports/csv`.

## Data Model

Do not create `crm_import_jobs`. Use `deal_jobs` as the import header:

- `deal_jobs.job_id` is the API `import_id`.
- `deal_jobs.organisation_id` and `deal_jobs.user_id` enforce org/user ownership.
- `deal_jobs.job_type = "crm_csv_import"` identifies this feature.
- `deal_jobs.status` tracks parent state: pending, in_progress, completed, failed, cancelled.
- `deal_jobs.job_metadata` stores filename, storage path, mode, mapping, mapping suggestions, options, upload/finalize timestamps, and analysis summary.
- `deal_jobs.result_data` stores final counts: total rows, processed rows, created accounts, created contacts, skipped duplicates, failed rows, and error summary.
- `deal_jobs.error_message` stores a parent-level failure reason only. Row failures belong in `crm_import_rows`.

Add org migration `migrations/org_migrations/versions/3c7f79637725_074_add_crm_csv_import_rows.py` with `down_revision = "3c7f79637726"`. Revision `3c7f79637726` already belongs to the meeting-recording video artifact migration.

Create `crm_import_rows`:

- `row_id uuid primary key`
- `import_job_id uuid not null references deal_jobs(job_id) on delete cascade`
- `organisation_id uuid not null`
- `row_number int not null`
- `raw_values jsonb not null`
- `normalized_values jsonb not null default '{}'`
- `status text not null`
- `target_type text null`
- `account_id uuid null`
- `contact_id uuid null`
- `deal_id uuid null`
- `error_code text null`
- `error_message text null`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Indexes:

- `ux_crm_import_rows_import_row_number` unique on `(organisation_id, import_job_id, row_number)`
- `ix_crm_import_rows_import_status` on `(organisation_id, import_job_id, status)`
- `ix_crm_import_rows_org_status` on `(organisation_id, status)`

## File Structure

- Create: `src/domain/models/organisation_tables/sync/crm/imports.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/__init__.py`
- Modify: `src/domain/models/organisation_tables/job_models.py`
- Create: `migrations/org_migrations/versions/3c7f79637725_074_add_crm_csv_import_rows.py`
- Create: `src/api/schemas/crm_import_models.py`
- Create: `src/api/routers/crm/imports_handler.py`
- Modify: `src/api/routers/crm/__init__.py`
- Modify: `src/api/fastapi_app.py`
- Modify: `src/infra/storage/azure_org_blob_storage.py`
- Create: `src/service/crm/imports/__init__.py`
- Create: `src/service/crm/imports/csv_parser.py`
- Create: `src/service/crm/imports/csv_mapping.py`
- Create: `src/service/crm/imports/import_service.py`
- Create: `src/service/crm/imports/agent_mapping.py` only if the frontend already needs the field in the response; otherwise defer it.
- Modify: `src/service/crm/contacts/contracts.py`
- Modify: `src/service/crm/contacts/write_service.py`
- Create: `src/workers/deal_worker/tasks/crm_import_tasks.py`
- Modify: `src/workers/deal_worker/tasks/__init__.py`
- Modify: `src/workers/deal_worker/config.py`
- Create: `tests/service/crm/imports/test_csv_parser.py`
- Create: `tests/service/crm/imports/test_csv_mapping.py`
- Create: `tests/service/crm/imports/test_import_service.py`
- Create: `tests/api/routers/crm/test_imports_handler.py`
- Create: `tests/workers/deal_worker/tasks/test_crm_import_tasks.py`
- Create: `tests/migrations/org/test_074_add_crm_csv_import_rows.py`
- Create: `docs/API Documentation/deals/crm-imports-api.md`

## Task 1: Import Row Table And DealJobs Header

**Files:**

- Create: `src/domain/models/organisation_tables/sync/crm/imports.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/__init__.py`
- Modify: `src/domain/models/organisation_tables/job_models.py`
- Create: `migrations/org_migrations/versions/3c7f79637725_074_add_crm_csv_import_rows.py`
- Create: `tests/migrations/org/test_074_add_crm_csv_import_rows.py`

- [x] Write `tests/migrations/org/test_074_add_crm_csv_import_rows.py` to read the migration file and assert it creates only `crm_import_rows`, does not create `crm_import_jobs`, includes the foreign key to `deal_jobs(job_id)`, includes all index names above, includes JSONB defaults, and sets `down_revision = "3c7f79637726"`.
- [x] Run: `uv run pytest tests/migrations/org/test_074_add_crm_csv_import_rows.py -q`
- [x] Expected: fail because the migration file does not exist.
- [x] Add `CRM_CSV_IMPORT = "crm_csv_import"` to `DealJobType` in `src/domain/models/organisation_tables/job_models.py`.
- [x] Add only `CRMImportRow` SQLModel plus row status constants in `src/domain/models/organisation_tables/sync/crm/imports.py`.
- [x] Export `CRMImportRow` from `src/domain/models/organisation_tables/sync/crm/__init__.py`.
- [x] Add the Alembic migration with only the row table and index names listed in Data Model.
- [x] Run: `uv run pytest tests/migrations/org/test_074_add_crm_csv_import_rows.py -q`
- [x] Expected: pass.
- [x] Run: `uv run alembic -c migrations/org_migrations/alembic.ini heads`
- [x] Expected: one org migration head including `3c7f79637725`.
- [ ] Commit: `feat: add crm csv import row ledger`

## Task 2: CSV Parser

**Files:**

- Create: `src/service/crm/imports/__init__.py`
- Create: `src/service/crm/imports/csv_parser.py`
- Create: `tests/service/crm/imports/test_csv_parser.py`

- [x] Write tests for UTF-8 BOM headers, blank rows, duplicate headers, invalid CSV, 10 MB file cap, 25k row cap, 200 column cap, and formula-looking values preserved for preview but sanitized by an exported helper.
- [x] Run: `uv run pytest tests/service/crm/imports/test_csv_parser.py -q`
- [x] Expected: fail because `csv_parser.py` does not exist.
- [x] Implement with Python stdlib `csv.DictReader`, `io.StringIO`, and no new dependency.
- [x] Normalize duplicate headers as `Header`, `Header (2)`, `Header (3)`.
- [x] Return a small parse result object containing headers, sampled rows, total row count, and blocking errors.
- [x] Add `sanitize_csv_cell_for_export(value: object) -> str` for error export.
- [x] Add the `copilotgtm:` cap comment beside the file/row cap constants.
- [x] Run: `uv run pytest tests/service/crm/imports/test_csv_parser.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: parse crm import csv`

## Task 3: Deterministic Mapping

**Files:**

- Create: `src/service/crm/imports/csv_mapping.py`
- Create: `tests/service/crm/imports/test_csv_mapping.py`

- [x] Write tests for aliases: `Company`, `Account Name`, `Website`, `Domain`, `First Name`, `Last Name`, `Full Name`, `Email`, `Title`, `LinkedIn`, `Phone`, `Company LinkedIn`, `Account LinkedIn`.
- [x] Write tests for sample heuristics: email-looking values map to `contact.email`, domain/website values map to account identity, person LinkedIn maps to `contact.linkedin_url`, company LinkedIn maps to `account.linkedin_url`.
- [x] Run: `uv run pytest tests/service/crm/imports/test_csv_mapping.py -q`
- [x] Expected: fail because `csv_mapping.py` does not exist.
- [x] Implement deterministic suggestions only: `high`, `medium`, `low`, `unmapped`.
- [x] Return reasons as short codes, not prose blobs, so the frontend can translate if needed.
- [x] Do not add an LLM call in this task.
- [x] Run: `uv run pytest tests/service/crm/imports/test_csv_mapping.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: map crm import columns`

## Task 4: Storage Path Helper

**Files:**

- Modify: `src/infra/storage/azure_org_blob_storage.py`
- Create or update: `tests/infra/storage/test_azure_org_blob_storage.py`

- [x] Write a test that `plan_crm_import_path(user_id, import_id, original_filename)` returns `crm_imports/{user_id}/{import_id}/{safe_filename}` and sanitizes path traversal.
- [x] Run: `uv run pytest tests/infra/storage/test_azure_org_blob_storage.py -q`
- [x] Expected: fail until the helper exists.
- [x] Add `plan_crm_import_path(...)` by reusing the existing private filename sanitizer.
- [x] Run: `uv run pytest tests/infra/storage/test_azure_org_blob_storage.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: add crm import blob path`

## Task 5: Contact Explicit Account Context

**Files:**

- Modify: `src/service/crm/contacts/contracts.py`
- Modify: `src/service/crm/contacts/write_service.py`
- Create or update: `tests/service/crm/contacts/test_write_service.py`

- [x] Write a focused test proving a new contact can be created with an explicit existing `company_id` and does not fall back to the email domain account.
- [x] Write a focused test proving an invalid or cross-org `company_id` fails through `AccountWriteService.load_org_company`.
- [x] Run: `uv run pytest tests/service/crm/contacts/test_write_service.py -q`
- [x] Expected: fail until the command/service supports explicit account context.
- [x] Add `company_id: UUID | None = None` to `ContactCreateCommand`.
- [x] In `ContactWriteService._create_contact_record`, when `company_id` is present, load that account and link it; otherwise keep the existing email-domain behavior.
- [x] Keep the restriction that existing contacts cannot change email or LinkedIn URL.
- [x] Run: `uv run pytest tests/service/crm/contacts/test_write_service.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: support explicit account on contact create`

## Task 6: Import Service Lifecycle

**Files:**

- Create: `src/service/crm/imports/import_service.py`
- Create: `tests/service/crm/imports/test_import_service.py`

- [x] Write service tests for `presign`, `finalize`, `analyze`, `save_mapping`, `start`, `process_next_batch`, and ownership checks.
- [x] Include tests for duplicate start returning current status without second enqueue, import id filtered by `DealJobs.organisation_id` and `DealJobs.user_id`, and row retries not duplicating `row_number`.
- [x] Run: `uv run pytest tests/service/crm/imports/test_import_service.py -q`
- [x] Expected: fail because `import_service.py` does not exist.
- [x] Implement `CRMImportService` with injected org session, organisation id, user id, and optional task sender function.
- [x] `presign` creates a `DealJobs` row with `job_type=DealJobType.CRM_CSV_IMPORT`, `deal_id=None`, `status=JobStatus.PENDING`, stores filename/storage path/mode in `job_metadata`, and returns write SAS with `import_id=job.job_id`.
- [x] `finalize` loads the `DealJobs` row by `job_id`, `organisation_id`, and `user_id`, checks blob properties, file size, and status before setting `job_metadata["crm_import_status"] = "analyzing"`.
- [x] `analyze` downloads the blob, parses it, saves mapping suggestions/counts in `job_metadata`, inserts first row records with `status="preview"`, and records `job_metadata["crm_import_status"] = "ready_for_mapping"`.
- [x] `save_mapping` validates target field names against the V1 supported field set and stores mapping/options in `job_metadata`.
- [x] `start` transitions only when `job_metadata["crm_import_status"] == "ready_for_mapping"`, sets `DealJobs.status = IN_PROGRESS`, stores `crm_import_status = "importing"`, and enqueues one process task.
- [x] `process_next_batch` loads unprocessed row records, resolves/creates account first for contact-plus-account rows, then calls `ContactWriteService.create_contact(...)`.
- [x] Treat duplicate service errors as `skipped_duplicate`; treat validation errors as row `failed`; commit per row or small batch; write final counts to `DealJobs.result_data`.
- [x] Run: `uv run pytest tests/service/crm/imports/test_import_service.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: process crm csv import lifecycle`

## Task 7: Worker Tasks

**Files:**

- Create: `src/workers/deal_worker/tasks/crm_import_tasks.py`
- Modify: `src/workers/deal_worker/tasks/__init__.py`
- Modify: `src/workers/deal_worker/config.py`
- Create: `tests/workers/deal_worker/tasks/test_crm_import_tasks.py`

- [x] Write tests proving `crm_import.analyze_csv` marks failed jobs on non-transient parse/storage errors.
- [x] Write tests proving `crm_import.process_csv` continues when one row fails and does not duplicate rows on retry.
- [x] Write a test proving worker execution can load an import by `organisation_id` and `import_job_id` without a user id payload, then uses the stored `deal_jobs.user_id` as the CRM import actor.
- [x] Run: `uv run pytest tests/workers/deal_worker/tasks/test_crm_import_tasks.py -q`
- [x] Expected: fail because task module does not exist.
- [x] Add task names `crm_import.analyze_csv` and `crm_import.process_csv`; each task receives `organisation_id` and `import_job_id`.
- [x] Route both to the existing `deal` queue in `DealWorkerConfig`; do not add a new queue for V1.
- [x] Export the tasks from `src/workers/deal_worker/tasks/__init__.py`.
- [x] Run: `uv run pytest tests/workers/deal_worker/tasks/test_crm_import_tasks.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: add crm csv import worker tasks`

## Task 8: API Router

**Files:**

- Create: `src/api/schemas/crm_import_models.py`
- Create: `src/api/routers/crm/imports_handler.py`
- Modify: `src/api/routers/crm/__init__.py`
- Modify: `src/api/fastapi_app.py`
- Create: `tests/api/routers/crm/test_imports_handler.py`

- [x] Write API tests for presign, finalize, get status, save mapping, start, auth scoping, missing auth, and invalid ownership.
- [x] Run: `uv run pytest tests/api/routers/crm/test_imports_handler.py -q`
- [x] Expected: fail because the router does not exist.
- [x] Add Pydantic schemas matching the API Contract.
- [x] Add `imports_router = APIRouter()` with endpoints under `/imports/csv...`.
- [x] Construct `CRMImportService` from `get_current_user` and `get_org_async_session`.
- [x] Apply `rate_limit_user_write` to presign, finalize, mapping, and start; apply read limits to status, rows, and errors.
- [x] Mount the router in `src/api/fastapi_app.py` at `prefix="/users/crm"`.
- [x] Do not remove or repurpose the disabled deal import router in this task.
- [x] Run: `uv run pytest tests/api/routers/crm/test_imports_handler.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: expose crm csv import api`

## Task 9: Error CSV And Polling Shape

**Files:**

- Modify: `src/service/crm/imports/import_service.py`
- Modify: `src/api/routers/crm/imports_handler.py`
- Modify: `tests/service/crm/imports/test_import_service.py`
- Modify: `tests/api/routers/crm/test_imports_handler.py`

- [x] Add API and service tests proving error CSV sanitizes formula-looking cells and includes row number, status, error code, error message, and original values from `crm_import_rows`.
- [x] Add API and service tests proving row listing is cursor-paginated by `row_number` and filterable by status.
- [x] Run focused service and API tests and confirm they fail for missing behavior.
- [x] Implement `build_error_csv(...)` with `sanitize_csv_cell_for_export(...)`.
- [x] Return compact polling fields: status, counts, percent complete, latest errors, and mapping confidence summary.
- [x] Run: `uv run pytest tests/service/crm/imports/test_import_service.py tests/api/routers/crm/test_imports_handler.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: report crm csv import errors`

## Task 10: Documentation

**Files:**

- Create: `docs/API Documentation/deals/crm-imports-api.md`

- [x] Document the flow: presign, browser SAS upload, finalize, preview, mapping patch, start, poll, rows, error CSV.
- [x] Include supported columns, unsupported V1 features, dedupe rules, file limits, row statuses, and example frontend sequence.
- [x] State clearly that `/users/crm/deals/import/...` remains the disabled legacy deal-import surface.
- [x] Run: `uv run python -m compileall src/service/crm/imports src/api/routers/crm/imports_handler.py src/workers/deal_worker/tasks/crm_import_tasks.py`
- [x] Expected: compile succeeds.
- [ ] Commit: `docs: document crm csv import api`

## Task 11: Deal-Level Import Support

**Files:**

- Modify: `src/service/crm/imports/csv_mapping.py`
- Modify: `src/service/crm/imports/import_service.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/imports.py`
- Modify: `migrations/org_migrations/versions/3c7f79637725_074_add_crm_csv_import_rows.py`
- Modify: `src/api/schemas/crm_import_models.py`
- Modify: `src/api/routers/crm/imports_handler.py`
- Modify: `src/workers/deal_worker/tasks/crm_import_tasks.py`
- Modify: `tests/service/crm/imports/test_csv_mapping.py`
- Modify: `tests/service/crm/imports/test_import_service.py`
- Modify: `tests/api/routers/crm/test_imports_handler.py`
- Modify: `tests/migrations/org/test_074_add_crm_csv_import_rows.py`
- Modify: `docs/API Documentation/deals/crm-imports-api.md`

- [x] Document the deal-level design: reuse the generic CRM import job and row table; do not create a separate deal import table.
- [x] Add `deal_id` to `crm_import_rows` so row outcomes can point to created deals.
- [x] Add deterministic mappings for supported deal create fields: name, amount, currency, expected close date, description, deal type, priority, lead source, probability, and forecast category.
- [x] Allow `deal.*` target fields in saved mappings while keeping custom deal fields unsupported.
- [x] Reuse `DealWriteService.create_deal(...)` instead of writing direct deal insert logic.
- [x] For rows with account/contact/deal data, create or resolve the account first, create the contact second, then create the deal with relationship payloads.
- [x] Pass a master session into request and worker processing paths because `DealWriteService` needs it for owner/user identity internals.
- [x] Include `created_deals` in counts and `deal_id` in row API responses.
- [x] Keep `/users/crm/deals/import/...` disabled; do not repurpose the legacy endpoint.
- [x] Run: `uv run pytest tests/service/crm/imports/test_csv_mapping.py tests/migrations/org/test_074_add_crm_csv_import_rows.py tests/service/crm/imports/test_import_service.py tests/api/routers/crm/test_imports_handler.py -q`
- [x] Expected: pass.
- [ ] Commit: `feat: support deal rows in crm csv import`

## Task 12: Integration Verification

**Files:**

- No planned file edits. Fix only issues found by verification.

- [x] Run: `uv run pytest tests/service/crm/imports tests/api/routers/crm/test_imports_handler.py tests/workers/deal_worker/tasks/test_crm_import_tasks.py tests/migrations/org/test_074_add_crm_csv_import_rows.py -q`
- [x] Run: `uv run pytest tests/api/routers/crm/test_deal_import_handler.py -q`
- [x] Run: `uv run ruff check src/service/crm/imports src/api/routers/crm/imports_handler.py src/api/schemas/crm_import_models.py src/workers/deal_worker/tasks/crm_import_tasks.py src/domain/models/organisation_tables/sync/crm/imports.py tests/service/crm/imports tests/api/routers/crm/test_imports_handler.py tests/workers/deal_worker/tasks/test_crm_import_tasks.py`
- [x] Run: `uv run alembic -c migrations/org_migrations/alembic.ini heads`
- [x] Run: `git diff --check`
- [ ] Manually test the lifecycle with a tiny CSV containing account-only, contact-only, contact-plus-account, duplicate, blank, and invalid rows.
- [ ] Expected: legacy deal-import tests still pass, new importer tests pass, one org migration head exists, and the manual tiny CSV produces row-level outcomes without failing the whole import.

## Rollout Plan

1. Ship backend behind `CRM_CSV_IMPORT_ENABLED=false` by default.
2. Enable in dev for internal orgs only.
3. Run 5-row, 500-row, and 5,000-row imports.
4. Watch job duration, row failure reasons, task retries, memory, and database contention.
5. Enable for one production pilot org.
6. Raise row/file limits only after retry behavior and p95 duration are acceptable.

## Open Decisions

- `update_existing`: recommended V1 behavior is reject `true` with a 400 until update semantics are explicitly designed.
- Agent mapping: recommended V1 behavior is deterministic-only unless frontend already depends on an `agent_status` field.
- Name-only contacts: recommended V1 behavior is allow create because current `CreateContactRequest` allows it, but flag as low confidence in preview.

## Self-Review

- Spec coverage: upload, preview, mapping, validation, dedupe, async execution, row errors, security, observability, folder structure, and rollout are covered.
- Current-state alignment: the plan preserves the disabled deal-import route and adds a new account/contact import route under existing CRM router structure.
- Loophole coverage: explicit account linking, storage path scoping, `deal_jobs` header reuse, row table separation, idempotent start/retry, formula injection, PII logs, and migration-head validation are named as implementation requirements.
- Placeholder scan: no `TBD`, `TODO`, or "add appropriate" placeholders remain.
