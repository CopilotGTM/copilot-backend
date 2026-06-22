# HubSpot Tasks Notes Source Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the HubSpot task/note move to `crm_tasks` and `crm_notes`, preserving the fields introduced by the new source tables and keeping `crm_activities` as the feed projection.

**Architecture:** Current code already writes HubSpot tasks to `CRMTask`, HubSpot notes to `CRMNote`, and calls `ActivityFeedProjectionService` for `crm_activities`/`crm_activity_links`. The remaining work is to close stale-plan gaps: normalize first-class task/note fields, update stale writer tests, redirect webhook/reconciliation deletes to source tables, and verify projection rows are tombstoned with their source rows.

**Tech Stack:** Python 3.11, SQLModel, async SQLAlchemy sessions, PostgreSQL tenant schemas, pytest, existing HubSpot record sync services.

---

## Current Code Facts

The previous version of this plan is stale. The current branch has already changed these surfaces:

- `src/service/integrations/crm/records/hubspot/tasks/task_writer.py` writes `CRMTask`.
- `src/service/integrations/crm/records/hubspot/notes/note_writer.py` writes `CRMNote`.
- Both HubSpot writers use `CRMSourceObjectType.TASK` or `CRMSourceObjectType.NOTE` mappings.
- Both HubSpot writers call `src/service/crm/activity_feed_projection_service.py`.
- Salesforce task/note writers have also moved to `CRMTask`/`CRMNote`, so this plan must not assume Salesforce remains on legacy `CRMActivity`.
- `tests/service/crm/test_task_service_boundaries.py` and `tests/service/crm/test_notes_service_boundaries.py` already enforce provider writers should not insert/select `CRMActivity` as the source table.

Remaining gaps found in the current checkout:

- `NormalizedHubSpotTask` does not expose `task_type`.
- `task_normalizer.py` still ignores `hs_task_priority` and `hs_task_type`.
- `NormalizedHubSpotNote` does not expose `title`, `body_format`, or `attachment_ids`.
- `note_normalizer.py` still sets `subject=None` and ignores `hs_attachment_ids`.
- HubSpot writer unit tests still construct `CRMActivity` fixtures and assert `CRMSourceObjectType.ACTIVITY`.
- HubSpot webhook delete handlers still query `CRMActivity` and set dropped legacy fields such as `synced_at`.
- HubSpot deletion reconciliation still maps `"tasks"` and `"notes"` to `CRMActivity`.
- `ActivityFeedProjectionService` does not put task priority/type or note body format/attachments into `display_payload`.

## Schema Rules

The latest activity migrations define the contract:

- `3c7f79637735_064_ai_native_crm_activity_tables.py` adds `crm_tasks`, `crm_notes`, `crm_activity_links`, and lightweight feed columns.
- `3c7f79637734_065_backfill_legacy_activity_tasks_notes.py` backfills legacy task/note activity rows into source tables and rewrites source mappings.
- `3c7f79637733_066_drop_legacy_activity_task_note_columns.py` removes legacy full-payload fields from `crm_activities`.

Field mapping to preserve:

| HubSpot field | Source table target | Feed projection target |
| --- | --- | --- |
| `hs_task_subject` | `CRMTask.title` | `CRMActivity.title` |
| `hs_task_body` | `CRMTask.description` | `CRMActivity.preview` |
| `hs_task_status` | `CRMTask.status` | `CRMActivity.display_payload.status` |
| `hs_task_priority` | `CRMTask.priority` | `CRMActivity.display_payload.priority` |
| `hs_task_type` | `CRMTask.task_metadata.hs_task_type` | `CRMActivity.display_payload.task_type` |
| `hs_timestamp` | `CRMTask.due_at` and `due_date` | `CRMActivity.occurred_at` |
| `hubspot_owner_id` | owner/assignee fields on `CRMTask` | `CRMActivity.actor_*` when available |
| `hs_note_body` | `CRMNote.body` | `CRMActivity.preview` |
| derived note title | `CRMNote.title` | `CRMActivity.title` |
| note body format | `CRMNote.body_format` | `CRMActivity.display_payload.body_format` |
| `hs_attachment_ids` | `CRMNote.note_metadata.hs_attachment_ids` | `CRMActivity.display_payload.has_attachments` |

Hard rules:

- Provider IDs stay in `crm_source_object_mappings.external_id`.
- `CRMActivity.source_record_id` stores local `CRMTask.task_id` or `CRMNote.note_id`, never a HubSpot ID.
- Source row deletes must also soft-delete the matching feed projection and remove timeline links through `ActivityFeedProjectionService.soft_delete_activity_projection`.
- Do not write legacy columns removed by migration `066` on `CRMActivity`.

---

### Task 1: Update Normalized HubSpot Task Fields

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_types.py`
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_normalizer.py`
- Test: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`

- [ ] **Step 1: Add `task_type` to `NormalizedHubSpotTask`**

Add after `activity_subtype`:

```python
task_type: str | None
```

- [ ] **Step 2: Normalize priority and task type**

Import `CRMActivityPriority` and map:

```python
priority=_normalize_priority(properties.get("hs_task_priority")),
task_type=_coerce_str(properties.get("hs_task_type")),
```

Use this helper:

```python
def _normalize_priority(value: Any) -> str:
    raw = _coerce_str(value)
    if raw is None:
        return CRMActivityPriority.NONE.value
    canonical = raw.upper()
    if canonical in {"LOW", "MEDIUM", "HIGH", "URGENT"}:
        return canonical
    return CRMActivityPriority.NONE.value
```

- [ ] **Step 3: Assert task metadata preserves provider type**

In the task writer test factory, set:

```python
priority="HIGH",
task_type="TODO",
```

Assert the written `CRMTask` has:

```python
assert reactivated.priority == "HIGH"
assert reactivated.task_metadata["hs_task_type"] == "TODO"
```

- [ ] **Step 4: Run the focused task writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py -v
```

Expected before implementation: FAIL because `task_type` is not accepted/persisted. Expected after implementation: PASS.

### Task 2: Update Normalized HubSpot Note Fields

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_types.py`
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_normalizer.py`
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_writer.py`
- Test: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`

- [ ] **Step 1: Add first-class note fields**

Add after `body`:

```python
title: str | None
body_format: str
attachment_ids: list[str]
```

- [ ] **Step 2: Derive title, body format, and attachments**

Normalize `hs_note_body` once, then set:

```python
subject=_derive_note_title(body),
body=body,
title=_derive_note_title(body),
body_format="html" if body else "plain_text",
attachment_ids=_split_hubspot_ids(properties.get("hs_attachment_ids")),
```

- [ ] **Step 3: Persist fields in `CRMNote`**

Use:

```python
title=note.title,
body_format=note.body_format,
```

and include in metadata:

```python
"hs_attachment_ids": note.attachment_ids,
```

- [ ] **Step 4: Assert writer output**

Update note writer tests to assert:

```python
assert reactivated.title == "Spoke with buyer offline"
assert reactivated.body_format == "html"
assert reactivated.note_metadata["hs_attachment_ids"] == ["attachment-1"]
```

- [ ] **Step 5: Run the focused note writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected before implementation: FAIL because `title`, `body_format`, and `attachment_ids` are absent. Expected after implementation: PASS.

### Task 3: Bring Writer Tests Up To The Source-Table Contract

**Files:**
- Modify: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`
- Modify: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`

- [ ] **Step 1: Replace task fixtures with `CRMTask`**

Use `CRMTask(task_id=..., title=..., is_deleted=...)` instead of `CRMActivity(activity_id=..., external_id=...)`.

- [ ] **Step 2: Replace note fixtures with `CRMNote`**

Use `CRMNote(note_id=..., body=..., title=..., is_deleted=...)` instead of `CRMActivity(activity_id=..., external_id=...)`.

- [ ] **Step 3: Assert mapping types**

Task mappings must assert:

```python
CRMSourceObjectType.TASK
```

Note mappings must assert:

```python
CRMSourceObjectType.NOTE
```

- [ ] **Step 4: Mock projection calls explicitly**

Set:

```python
writer._projection.upsert_task = AsyncMock()
writer._projection.soft_delete_activity_projection = AsyncMock()
```

or:

```python
writer._projection.upsert_note = AsyncMock()
writer._projection.soft_delete_activity_projection = AsyncMock()
```

Assert reactivated and deactivated source rows call the projection service.

### Task 4: Redirect Webhook Deletes To Source Tables

**Files:**
- Modify: `src/service/integrations/webhooks/hubspot/tasks/handler.py`
- Modify: `src/service/integrations/webhooks/hubspot/notes/handler.py`
- Test: `tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py`

- [ ] **Step 1: Change task delete handler**

After `mark_external_ids_deleted(CRMSourceObjectType.TASK, ...)`, load `CRMTask` by `task_id`, mark it deleted, and call:

```python
await ActivityFeedProjectionService(
    session,
    organisation_id,
).soft_delete_activity_projection(task.task_id)
```

- [ ] **Step 2: Change note delete handler**

After `mark_external_ids_deleted(CRMSourceObjectType.NOTE, ...)`, load `CRMNote` by `note_id`, mark it deleted, and call:

```python
await ActivityFeedProjectionService(
    session,
    organisation_id,
).soft_delete_activity_projection(note.note_id)
```

- [ ] **Step 3: Remove legacy activity tombstone fields**

Do not set `synced_at` on `CRMActivity` in these delete paths.

- [ ] **Step 4: Run webhook delete tests**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py tests/service/integrations/webhooks/hubspot/tasks/test_task_handler.py tests/service/integrations/webhooks/hubspot/notes/test_note_handler.py -v
```

Expected after implementation: PASS.

### Task 5: Redirect Deletion Reconciliation To Source Tables

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/deletion_reconciliation_service.py`
- Test: `tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py`

- [ ] **Step 1: Map tasks and notes to source models**

Use:

```python
"tasks": CRMTask
"notes": CRMNote
```

and:

```python
"tasks": CRMTask.task_id
"notes": CRMNote.note_id
```

- [ ] **Step 2: Remove task/note `CRMActivity.activity_type` filters**

Those filters are only valid when `CRMActivity` is the source table. Keep them out of source table queries.

- [ ] **Step 3: Soft-delete projections after source rows**

For every deleted task/note source row, call:

```python
await ActivityFeedProjectionService(
    session,
    organisation_id,
).soft_delete_activity_projection(row.task_id_or_note_id)
```

- [ ] **Step 4: Run reconciliation tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py -v
```

Expected after implementation: PASS.

### Task 6: Enrich Feed Projection Payloads

**Files:**
- Modify: `src/service/crm/activity_feed_projection_service.py`
- Test: `tests/test_ai_native_activity_schema.py` if schema checks need updates
- Test: existing writer tests if projection calls are asserted

- [ ] **Step 1: Task projection payload**

Set task feed `display_payload` to:

```python
{
    "status": task.status,
    "priority": task.priority,
    "task_type": metadata.get("hs_task_type") or metadata.get("task_type"),
}
```

- [ ] **Step 2: Note projection payload**

Set note feed `display_payload` to:

```python
{
    "body_format": note.body_format,
    "visibility": note.visibility,
    "has_attachments": bool((note.note_metadata or {}).get("hs_attachment_ids")),
}
```

- [ ] **Step 3: Keep feed rows lightweight**

Do not add full note bodies, task bodies, raw provider properties, or dropped legacy activity columns to the feed row.

### Task 7: Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run focused HubSpot writer tests**

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

- [ ] **Step 2: Run webhook tests**

```bash
uv run pytest tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py tests/service/integrations/webhooks/hubspot/tasks/test_task_handler.py tests/service/integrations/webhooks/hubspot/notes/test_note_handler.py -v
```

- [ ] **Step 3: Run reconciliation and schema tests**

```bash
uv run pytest tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py tests/test_ai_native_activity_schema.py tests/migrations/test_ai_native_activity_migration_contract.py -v
```

- [ ] **Step 4: Run targeted ruff**

```bash
uv run ruff check src/service/crm/activity_feed_projection_service.py src/service/integrations/crm/records/hubspot src/service/integrations/webhooks/hubspot tests/service/integrations/crm/records/hubspot tests/service/integrations/webhooks/hubspot tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py
```

---

## Production Risks And Guardrails

- A stale `CRMActivity` delete path can leave `crm_tasks`/`crm_notes` active. Webhook and reconciliation deletes must tombstone source rows.
- A source-only delete can leave the feed visible. Always call `ActivityFeedProjectionService.soft_delete_activity_projection`.
- Source mappings rewritten by migration `065` mean writer lookups must use `CRMSourceObjectType.TASK` and `CRMSourceObjectType.NOTE`.
- Task/note writer tests must not construct `CRMActivity` as the source row, or they will hide regressions.
- HubSpot task priority/status must remain inside current check constraints.
- HubSpot note bodies can be HTML; full content belongs in `CRMNote.body`, while feed rows should only store a compact preview and display flags.

## Self-Review

Spec coverage:

- HubSpot source table writes: already implemented, locked by Tasks 1-3.
- New note/task fields: Tasks 1, 2, and 6.
- Webhook deletes: Task 4.
- Delete reconciliation: Task 5.
- Feed projection payloads: Task 6.
- Verification: Task 7.

Placeholder scan:

- No step depends on unspecified future work.
- No production backfill is planned because migration `065` already owns legacy task/note backfill and source-mapping rewrite.

Type consistency:

- HubSpot task provider mapping points to `CRMSourceObjectType.TASK` and local `CRMTask.task_id`.
- HubSpot note provider mapping points to `CRMSourceObjectType.NOTE` and local `CRMNote.note_id`.
- Feed rows point to local source row IDs through `CRMActivity.source_record_id`.
