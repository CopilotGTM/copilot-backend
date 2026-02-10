# Stakeholder Edit API and Sync Guard Plan

## Worktree Setup

- Base branch: `main`
- New branch: `feat/stakeholder-edit-api`
- Worktree path: `D:/CopilotGTM/copilot-backend-stakeholder-edit`
- Base commit at creation: `8bddc24` (main head at worktree creation time)

## Goal

Enable stakeholder edit functionality in CRM Stakeholders API and prevent stakeholder mapper sync from unintentionally overwriting user-managed stakeholder fields.

## Current State Findings

### 1) Edit endpoint is missing

- `src/api/routers/crm/stakeholders_handler.py` currently has:
  - list stakeholders
  - create stakeholder
  - get detail
  - approve/reject/remove
- There is no `PATCH`/`PUT` endpoint for editing stakeholder details.

### 2) Stakeholder sync can overwrite values

- `src/workers/deal_worker/services/deal_mapper_services/stakeholder_sync_service.py`
  updates existing stakeholders matched by email.
- Current behavior in `_ensure_stakeholder()`:
  - sets `source = CRM`
  - sets `approval_status = APPROVED`
  - sets `is_deleted = False`
  - enriches and may overwrite `job_title` from `CRMContact`
- This means manual values can be changed by later mapper sync runs.

### 3) Existing data field can support override tracking

- `DealStakeholder` has `stakeholder_data: dict[str, Any] | None` in
  `src/domain/models/organisation_tables/sync/crm/stakeholders.py`.
- We can use this JSONB field to track manual field locks without adding a new DB column.

## Answer to the Mapping Question (Current Behavior)

Yes, today mapper sync can automatically update existing stakeholder values after manual changes for some fields (notably source, approval status, and potentially job title via CRM enrichment).  
So manual updates are not fully protected right now.

## Target Behavior

1. Users can edit stakeholder profile fields through API.
2. Manual edits remain stable across stakeholder mapping sync runs.
3. CRM sync still updates non-user-managed fields and can still create/reactivate CRM stakeholders when needed.
4. Organization/deal isolation remains strict for every query.

## Proposed API Contract

### New endpoint

- `PATCH /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}`

### Editable fields (safe v1)

- `name`
- `job_title`
- `department`
- `stance`
- `decision_power`
- `champion_potential`
- `motivation`
- `pain_points`
- `external_links`

### Non-editable in this endpoint

- `email` (avoid re-key/matching side effects in v1)
- `source`
- `approval_status` (already handled by approve/reject/remove endpoints)
- `engagement_score`, `temperature`, `last_contact_at` (system-derived)

## Implementation Plan

## Phase 1: Schema + Router Surface

### Files

- `src/api/schemas/crm_models.py`
- `src/api/routers/crm/stakeholders_handler.py`

### Changes

1. Add `UpdateStakeholderRequest` with optional editable fields.
2. Add `PATCH` endpoint in stakeholders handler.
3. Return `GenericResponse[StakeholderDetail]`.
4. Keep error behavior aligned with existing `StakeholdersServiceError`.

## Phase 2: Service Edit Logic

### File

- `src/service/crm/insights/stakeholders_service.py`

### Changes

1. Add `update_stakeholder(deal_id, stakeholder_id, request)` method.
2. Validate stakeholder ownership (`organisation_id` + `deal_id` + `stakeholder_id`).
3. Apply only provided fields (partial update semantics).
4. Persist manual override metadata in `stakeholder_data`, e.g.:
   - `stakeholder_data.manual_overrides = ["name", "job_title", ...]`
5. Commit and refresh, return `StakeholderDetail`.
6. Trigger rematching hook only if any future matching-critical field becomes editable.

## Phase 3: Protect Manual Fields During Sync

### File

- `src/workers/deal_worker/services/deal_mapper_services/stakeholder_sync_service.py`

### Changes

1. Add helper to read manual override set from `stakeholder_data`.
2. In `_ensure_stakeholder(existing=...)`, skip CRM writes for locked fields.
3. Recommended guard behavior:
   - Do not force `approval_status = APPROVED` if current status is `REJECTED` or `REMOVED`.
   - Keep `source` as-is when stakeholder originated from manual/user-managed flow.
   - Do not overwrite `job_title` when `job_title` is manually locked.
4. Continue to update `contact_id` link when safe and useful.

## Phase 4: Tests

### Files

- `tests/api/routers/crm/test_stakeholders_handler.py`
- `tests/api/routers/crm/conftest.py`
- New: `tests/workers/deal_worker/services/deal_mapper_services/test_stakeholder_sync_service.py`

### Test cases

1. PATCH success updates only submitted fields.
2. PATCH 404 for unknown stakeholder.
3. PATCH validation failures (invalid payload ranges/types).
4. PATCH preserves untouched fields.
5. Sync does not overwrite manually locked `job_title`/`name`.
6. Sync does not auto-re-approve `REJECTED`/`REMOVED` stakeholders.
7. Sync still updates non-locked CRM-managed records.

## Phase 5: API Documentation

### File

- `docs/API Documentation/deals/stakeholders-api.md`

### Changes

1. Add new PATCH endpoint docs and examples.
2. Document editable vs system-managed fields.
3. Document sync behavior with manual overrides.

## Phase 6: Verification

### Commands

- `uv run pytest tests/api/routers/crm/test_stakeholders_handler.py -q`
- `uv run pytest tests/workers/deal_worker/services/deal_mapper_services/test_stakeholder_sync_service.py -q`

## Rollout Notes

1. No DB migration required if manual override metadata is stored in existing `stakeholder_data` JSONB.
2. Backward compatible for existing records (`stakeholder_data` can be null).
3. If override metadata is absent, sync behavior defaults to current CRM-driven behavior.

## Acceptance Criteria

1. Stakeholder edit endpoint exists and is fully test-covered.
2. Manual edits persist after stakeholder sync task runs.
3. CRM sync still performs normal create/update/deactivate for non-overridden fields.
4. API docs include the new edit endpoint and behavior notes.
