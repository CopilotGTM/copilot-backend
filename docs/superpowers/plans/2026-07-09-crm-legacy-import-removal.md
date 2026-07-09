# CRM Legacy Import Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Completely remove the old deal-specific CSV import compatibility surface now that the generic CRM CSV upload/import flow is implemented under `/users/crm/imports/csv`.

**Architecture:** Delete the disabled legacy router instead of keeping a second deprecation layer. Preserve the new generic import stack unchanged: `imports_handler.py`, `CRMImportService`, `crm_import_rows`, `DealJobType.CRM_CSV_IMPORT`, Azure direct upload, and deal-worker import tasks stay as the only CRM CSV import path.

**Tech Stack:** FastAPI routers, Pydantic response schemas, SQLModel/Alembic org migrations, Azure org blob storage, Celery deal worker, pytest, repo API docs.

---

## Evidence Read Before Planning

- Recent import implementation: PR 1038 added `docs/API Documentation/deals/crm-imports-api.md`, `docs/Technical Documentation/crm-csv-import-flow.md`, `src/api/routers/crm/imports_handler.py`, `src/service/crm/imports/*`, `src/workers/deal_worker/tasks/crm_import_tasks.py`, and `crm_import_rows`.
- Current new endpoint surface: `POST /users/crm/imports/csv/presign`, `POST /users/crm/imports/csv/{import_id}/finalize`, `GET /users/crm/imports/csv/{import_id}`, `PATCH /users/crm/imports/csv/{import_id}/mapping`, `POST /users/crm/imports/csv/{import_id}/start`, `GET /users/crm/imports/csv/{import_id}/rows`, and `GET /users/crm/imports/csv/{import_id}/errors.csv`.
- Current old endpoint surface: `src/api/routers/crm/deal_import_handler.py` still registers disabled routes under `/users/crm/deals/import/...` and returns `409 Conflict`.
- Current app wiring: `src/api/fastapi_app.py` imports and mounts `deal_import_router` at `/users/crm/deals`.
- Current router package wiring: `src/api/routers/crm/__init__.py` imports and exports `deal_import_router`.
- Current test coverage for old behavior: `tests/api/routers/crm/test_deal_import_handler.py` asserts the disabled 409 responses.
- Current deal list compatibility remnant: `src/api/schemas/crm_models.py` defines `DealImportJobStatus`; `DealsPage.active_import_job` is always set to `None` in `src/api/routers/crm/deals_handler.py`.
- Current docs remnants: `docs/API Documentation/deals/crm-deals-import-api.md`, sections in `crm-deals-api.md`, `crm-deals-architecture.md`, `crm-custom-fields-api.md`, `crm-pipelines-api.md`, `crm-imports-api.md`, `crm-csv-import-flow.md`, and `crm-foundation-production-tasklist.md` still describe the disabled legacy import surface or stale `active_import_job` behavior.

## Decision

Delete the legacy deal-specific import API. Do not keep the disabled 409 router.

Reasoning:

- The old flow is already functionally unavailable, so keeping the router only preserves a dead API contract.
- The new generic upload flow supports deal rows through `/users/crm/imports/csv`, so `/users/crm/deals/import/...` is no longer the future migration target.
- A hard 404 after route removal is cleaner than a permanent fake "coming soon" response that points clients at the wrong API shape.
- Removing `active_import_job` from the deal list response is a breaking API cleanup, so do it in the same removal branch only if the frontend has already migrated. If frontend still reads the field, keep it as `None` for one release and document the removal deadline.

## Non-Goals

- Do not remove the new generic CSV import upload functionality.
- Do not remove `CRMImportRow`, `DealJobType.CRM_CSV_IMPORT`, `src/service/crm/imports/*`, `src/api/routers/crm/imports_handler.py`, or `src/workers/deal_worker/tasks/crm_import_tasks.py`.
- Do not rewrite import processing, mapping, dedupe, row reporting, or Azure upload behavior.
- Do not add a new compatibility redirect from `/users/crm/deals/import/...` to `/users/crm/imports/csv`; the request/response lifecycle is different and redirecting would be misleading.
- Do not delete historical Alembic migrations.

## Removal Boundaries

Safe to delete:

- `src/api/routers/crm/deal_import_handler.py`
- `tests/api/routers/crm/test_deal_import_handler.py`
- `docs/API Documentation/deals/crm-deals-import-api.md`

Safe to modify:

- `src/api/fastapi_app.py`
- `src/api/routers/crm/__init__.py`
- `src/api/schemas/crm_models.py`
- `src/api/routers/crm/deals_handler.py`
- `tests/api/routers/crm/test_deals_handler.py`
- `docs/API Documentation/deals/crm-deals-api.md`
- `docs/API Documentation/deals/crm-deals-architecture.md`
- `docs/API Documentation/deals/crm-custom-fields-api.md`
- `docs/API Documentation/deals/crm-pipelines-api.md`
- `docs/API Documentation/deals/crm-imports-api.md`
- `docs/Technical Documentation/crm-foundation-production-tasklist.md`
- `docs/Technical Documentation/crm-foundation-production-readiness-review.md`
- `docs/Technical Documentation/crm-csv-import-flow.md`

Must not modify unless a test proves coupling:

- `src/api/routers/crm/imports_handler.py`
- `src/api/schemas/crm_import_models.py`
- `src/service/crm/imports/import_service.py`
- `src/service/crm/imports/csv_parser.py`
- `src/service/crm/imports/csv_mapping.py`
- `src/workers/deal_worker/tasks/crm_import_tasks.py`
- `src/domain/models/organisation_tables/sync/crm/imports.py`
- `migrations/org_migrations/versions/3c7f79637725_074_add_crm_csv_import_rows.py`

## Task 1: Delete The Legacy Deal Import Router

**Files:**

- Delete: `src/api/routers/crm/deal_import_handler.py`
- Modify: `src/api/routers/crm/__init__.py`
- Modify: `src/api/fastapi_app.py`
- Delete: `tests/api/routers/crm/test_deal_import_handler.py`

- [ ] Remove `from .deal_import_handler import deal_import_router` from `src/api/routers/crm/__init__.py`.
- [ ] Remove `"deal_import_router"` from `__all__` in `src/api/routers/crm/__init__.py`.
- [ ] Remove `deal_import_router` from the grouped CRM import in `src/api/fastapi_app.py`.
- [ ] Remove this mount from `src/api/fastapi_app.py`:

```python
fastapp.include_router(deal_import_router, prefix="/users/crm/deals", tags=["deals"])
```

- [ ] Delete `src/api/routers/crm/deal_import_handler.py`.
- [ ] Delete `tests/api/routers/crm/test_deal_import_handler.py`.
- [ ] Run: `uv run pytest tests/api/routers/crm/test_imports_handler.py -q`
- [ ] Expected: pass, proving the new generic import router still works.
- [ ] Run: `uv run pytest tests/api/routers/crm/test_deals_handler.py -q`
- [ ] Expected: pass or fail only on `active_import_job` assertions handled in Task 2.
- [ ] Commit: `refactor: remove legacy deal import router`

## Task 2: Remove Deal List Import Reconnect Remnants

**Files:**

- Modify: `src/api/schemas/crm_models.py`
- Modify: `src/api/routers/crm/deals_handler.py`
- Modify: `tests/api/routers/crm/test_deals_handler.py`

- [ ] In `src/api/schemas/crm_models.py`, delete the `DealImportJobStatus` class.
- [ ] In `src/api/schemas/crm_models.py`, remove `active_import_job` from `DealsPage`.
- [ ] In `src/api/routers/crm/deals_handler.py`, remove `active_import_job=None` from `DealsPage(...)`.
- [ ] In `tests/api/routers/crm/test_deals_handler.py`, replace the legacy test `test_list_deals_hides_active_import_job_links` with a test that asserts the deal list payload does not include `active_import_job`.

Expected assertion shape:

```python
assert "active_import_job" not in data
```

- [ ] Run: `uv run pytest tests/api/routers/crm/test_deals_handler.py -q`
- [ ] Expected: pass.
- [ ] Run: `uv run pytest tests/api/routers/crm/test_imports_handler.py -q`
- [ ] Expected: pass.
- [ ] Commit: `refactor: drop deal import reconnect field`

Fallback if frontend still depends on `active_import_job`:

- [ ] Keep `active_import_job: None` in `DealsPage` for one frontend release.
- [ ] Add a `copilotgtm:` comment above the field saying it is a temporary response-shape shim and must be removed after the frontend stops reading it.
- [ ] Still complete Task 1 so the old executable routes are gone.

## Task 3: Update API Documentation To Point Only At The New Upload Flow

**Files:**

- Delete: `docs/API Documentation/deals/crm-deals-import-api.md`
- Modify: `docs/API Documentation/deals/crm-deals-api.md`
- Modify: `docs/API Documentation/deals/crm-deals-architecture.md`
- Modify: `docs/API Documentation/deals/crm-custom-fields-api.md`
- Modify: `docs/API Documentation/deals/crm-pipelines-api.md`
- Modify: `docs/API Documentation/deals/crm-imports-api.md`
- Modify: `docs/Technical Documentation/crm-csv-import-flow.md`
- Modify: `docs/Technical Documentation/crm-foundation-production-tasklist.md`

- [ ] Delete `docs/API Documentation/deals/crm-deals-import-api.md`.
- [ ] In `docs/API Documentation/deals/crm-deals-api.md`, remove the `DealImportJobStatus` interface.
- [ ] In `docs/API Documentation/deals/crm-deals-api.md`, remove `active_import_job` from the `DealsPage` interface and response example if Task 2 removes the field.
- [ ] In `docs/API Documentation/deals/crm-deals-api.md`, replace the old `Deal Import (CSV)` section with this text:

```markdown
## Deal CSV Upload Import

Deal CSV import is handled by the generic CRM CSV upload API, not by deal-scoped routes.

Use `docs/API Documentation/deals/crm-imports-api.md` and the base path `/users/crm/imports/csv`.
The removed `/users/crm/deals/import/...` endpoints are no longer registered.
```

- [ ] In `docs/API Documentation/deals/crm-deals-architecture.md`, replace the old disabled endpoint list with a pointer to `docs/API Documentation/deals/crm-imports-api.md`.
- [ ] In `docs/API Documentation/deals/crm-custom-fields-api.md`, update CRM import references to say the generic CRM import flow does not support custom fields in V1.
- [ ] In `docs/API Documentation/deals/crm-pipelines-api.md`, update `crm-deals-import-api.md` references to `crm-imports-api.md`.
- [ ] In `docs/API Documentation/deals/crm-imports-api.md`, replace wording that says the legacy route remains disabled with wording that says deal imports use the generic CRM CSV import API and the legacy deal-scoped endpoints are no longer registered.
- [ ] In `docs/Technical Documentation/crm-csv-import-flow.md`, replace wording that says the legacy `/users/crm/deals/import/...` endpoints remain disabled with wording that says they have been removed.
- [ ] In `docs/Technical Documentation/crm-foundation-production-tasklist.md`, remove or update the stale statement that CSV import remains disabled and `active_import_job` is always null.
- [ ] Run: `rg -n "crm-deals-import-api|/users/crm/deals/import|Deal CSV import is coming soon|active_import_job|CSV import is disabled|legacy .*remain[s]? disabled" docs src tests -S -g "!docs/superpowers/plans/**"`
- [ ] Expected: no matches in current source, tests, or active docs, unless Task 2 used the temporary `active_import_job` fallback.
- [ ] Historical implementation plans under `docs/superpowers/plans/**` may still mention the old disabled route as past context; do not edit old completed plans just to make this grep clean.
- [ ] Commit: `docs: remove legacy deal import contract`

## Task 4: Keep The New Generic Upload Flow Covered

**Files:**

- No planned production edits.

- [ ] Run: `uv run pytest tests/service/crm/imports tests/api/routers/crm/test_imports_handler.py tests/workers/deal_worker/tasks/test_crm_import_tasks.py tests/migrations/org/test_074_add_crm_csv_import_rows.py -q`
- [ ] Expected: pass.
- [ ] Run: `uv run ruff check src/api/fastapi_app.py src/api/routers/crm src/api/schemas/crm_models.py tests/api/routers/crm`
- [ ] Expected: pass.
- [ ] Run: `uv run python -m compileall src/api/fastapi_app.py src/api/routers/crm src/api/schemas/crm_models.py`
- [ ] Expected: compile succeeds.
- [ ] Commit: `test: verify crm import removal safety`

## Task 5: Frontend/Consumer Contract Check Before Merge

**Files:**

- No backend edits unless this check finds a backend-owned contract miss.

- [ ] Search the frontend for old endpoints:

```bash
rg -n "/users/crm/deals/import|active_import_job|crm-deals-import-api|DealImportJobStatus" D:/CopilotGTM/copilot-app-frontend -S
```

- [ ] Expected: no runtime references to the old endpoints before backend deletion ships.
- [ ] If frontend still uses old endpoints, update frontend first to call `/users/crm/imports/csv`.
- [ ] If frontend only still reads `active_import_job`, use the Task 2 fallback for one release, then remove it in a follow-up backend cleanup.
- [ ] Do not reintroduce the disabled backend router to satisfy frontend references.

## Final Acceptance Criteria

- `/users/crm/deals/import/template`, `/users/crm/deals/import/upload-url`, `/users/crm/deals/import/process`, and `/users/crm/deals/import/jobs/{job_id}/events` are not registered in FastAPI.
- `deal_import_handler.py` and its 409-response tests are gone.
- `src/api/fastapi_app.py` mounts only the generic `imports_router` for CRM CSV upload/import.
- Docs mention `/users/crm/imports/csv` as the only CRM CSV import/upload API.
- Docs do not describe the disabled legacy deal import API as current behavior.
- The generic import tests still pass.
- Deal list tests pass after either removing `active_import_job` or explicitly retaining it as a one-release shim.

## Rollout

1. Merge backend removal after frontend no longer calls `/users/crm/deals/import/...`.
2. Deploy to dev.
3. Confirm OpenAPI no longer exposes `/users/crm/deals/import/*`.
4. Run a small dev import through `/users/crm/imports/csv` using one account row, one contact row, and one deal row.
5. Deploy to production.
6. Watch 404s for `/users/crm/deals/import/*` for one week. If traffic appears, fix the caller; do not restore the legacy backend route.

## Self-Review

- Scope coverage: the plan removes executable legacy routes, dead tests, response-schema reconnect remnants, and docs references while preserving the new generic upload flow.
- YAGNI check: no replacement compatibility router, redirect layer, feature flag, or new dependency is introduced.
- Safety check: every task has focused verification aimed at proving the new generic import path and deal listing still work.
- Placeholder scan: no TBD or unspecified implementation steps remain.
