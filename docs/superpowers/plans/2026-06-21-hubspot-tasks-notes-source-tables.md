# HubSpot Tasks Notes Source Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redirect HubSpot task and note persistence from full `crm_activities` rows into `crm_tasks` and `crm_notes`, while keeping `crm_activities` as the lightweight feed projection and `crm_activity_links` as the per-object timeline index.

**Architecture:** Keep the existing HubSpot fetch, normalize, full-sync, webhook, and incremental-refresh orchestration. Change only the writer and delete persistence boundary: provider source mappings should point to local `CRMTask.task_id` or `CRMNote.note_id`, then a feed-card `CRMActivity` should point back to the local source row through `source_type` and `source_record_id`. HubSpot is the first provider; Salesforce remains unchanged in this plan.

**Tech Stack:** Python 3.11, FastAPI service modules, SQLModel models, PostgreSQL tenant schemas, async SQLAlchemy sessions, pytest, existing HubSpot CRM record-sync services.

---

## Scope Boundary

This plan is HubSpot only.

Do not modify Salesforce task/note writers in this pass:

- `src/service/integrations/crm/records/salesforce/tasks/task_writer.py`
- `src/service/integrations/crm/records/salesforce/notes/note_writer.py`

Do not redesign task APIs, note APIs, agent tools, or timeline reads unless focused HubSpot tests expose a hard compile-time dependency. Existing readers can continue to read legacy `crm_activities` fields until a separate read-path migration is approved.

The persistence target changes are:

| Provider object | Source table | Feed table | Link table |
| --- | --- | --- | --- |
| HubSpot task | `crm_tasks` | `crm_activities` | `crm_activity_links` |
| HubSpot note | `crm_notes` | `crm_activities` | `crm_activity_links` |

Provider IDs remain in `crm_source_object_mappings`. `crm_activities.source_record_id` stores the local source row ID, not the HubSpot ID.

## Current Code Facts

Current HubSpot task and note writers still write full records into `CRMActivity`:

- `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`
- `src/service/integrations/crm/records/hubspot/notes/note_writer.py`

Current source mapping writes for HubSpot tasks and notes use:

```python
local_object_type=CRMSourceObjectType.ACTIVITY
```

That must become:

```python
local_object_type=CRMSourceObjectType.TASK
```

for tasks, and:

```python
local_object_type=CRMSourceObjectType.NOTE
```

for notes.

Current HubSpot webhook delete handlers resolve provider task/note IDs to activity IDs, then mark `CRMActivity` rows deleted:

- `src/service/integrations/webhooks/hubspot/tasks/handler.py`
- `src/service/integrations/webhooks/hubspot/notes/handler.py`

Those must resolve provider IDs to source rows first, then tombstone the source row and its feed projection.

## Schema Mapping From The Latest Activity Migrations

The last three activity migrations define the actual contract this plan must follow:

- `migrations/org_migrations/versions/3c7f79637735_064_ai_native_crm_activity_tables.py`
  - Adds `crm_tasks`, `crm_notes`, `crm_activity_links`, and lightweight feed columns on `crm_activities`.
- `migrations/org_migrations/versions/3c7f79637734_065_backfill_legacy_activity_tasks_notes.py`
  - Backfills legacy `crm_activities` task/note payloads into `crm_tasks` and `crm_notes`.
  - Rewrites `crm_source_object_mappings.local_object_type` from `activity` to `task` or `note`.
  - Sets existing task/note feed rows to `source_type = 'TASK'/'NOTE'` and `source_record_id = activity_id::text`.
- `migrations/org_migrations/versions/3c7f79637733_066_drop_legacy_activity_task_note_columns.py`
  - Drops full task/note payload columns from `crm_activities`.
  - After this migration, source writers must not depend on `crm_activities.body`, `status`, `priority`, `owner_id`, `due_date`, `source_created_at`, `source_modified_at`, `synced_at`, `last_seen_at`, `properties`, or `metrics`.

Column mapping that must be preserved:

| Legacy/full payload concept | New source column | Feed projection column |
| --- | --- | --- |
| Task subject | `crm_tasks.title` | `crm_activities.title` |
| Task body | `crm_tasks.description` | `crm_activities.preview` only |
| Task status | `crm_tasks.status` | `crm_activities.display_payload.status` |
| Task priority | `crm_tasks.priority` | `crm_activities.display_payload.priority` |
| HubSpot task type | `crm_tasks.task_metadata.hs_task_type` | `crm_activities.display_payload.task_type` |
| Task due timestamp | `crm_tasks.due_at` | `crm_activities.occurred_at` |
| Task due date | `crm_tasks.due_date` | none |
| Task reminder | `crm_tasks.reminder_at` if trusted later | none |
| Task completed timestamp | `crm_tasks.completed_at` | `crm_activities.occurred_at` fallback |
| Task owner | `crm_tasks.owner_user_id`, `owner_email` | `crm_activities.actor_name`, `actor_email` |
| Task assignee | `crm_tasks.assigned_to_user_id`, `assigned_to_email`, `assigned_to_name` | `crm_activity_links` with `link_role = 'ASSIGNEE'` only when explicitly implemented |
| Note title | `crm_notes.title` | `crm_activities.title` |
| Note body | `crm_notes.body` | `crm_activities.preview` only |
| Note body format | `crm_notes.body_format` | `crm_activities.display_payload.body_format` |
| Note visibility | `crm_notes.visibility` | `crm_activities.display_payload.visibility` |
| Note author | `crm_notes.author_user_id`, `author_name`, `author_email` | `crm_activities.actor_name`, `actor_email` |
| HubSpot note attachments | `crm_notes.note_metadata.hs_attachment_ids` | `crm_activities.display_payload.has_attachments` |
| Provider raw properties | `crm_tasks.task_metadata.properties`, `crm_notes.note_metadata.properties` | none |
| Provider ID | `crm_source_object_mappings.external_id` | never in `crm_activities.source_record_id` |
| Local source row ID | `crm_tasks.task_id` / `crm_notes.note_id` | `crm_activities.source_record_id` |

Important compatibility point: migration `065` intentionally uses the existing `crm_activities.activity_id` as the new `crm_tasks.task_id` or `crm_notes.note_id` for backfilled rows. New HubSpot writes should keep using source mappings to find the local task/note row; they do not need a separate production backfill in this implementation plan.

## Files To Modify

- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_types.py`
  - Add normalized fields for HubSpot task type and keep priority explicit.

- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_normalizer.py`
  - Map `hs_task_priority` to canonical task priorities.
  - Preserve `hs_task_type` in the normalized object for `crm_tasks.task_metadata`.

- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`
  - Write `CRMTask` as the source row.
  - Upsert the task source mapping as local object type `TASK`.
  - Upsert a lightweight `CRMActivity` feed projection.
  - Upsert `CRMActivityLink` rows for resolved deal/contact/company associations.

- Modify: `src/service/integrations/crm/records/hubspot/notes/note_types.py`
  - Add normalized fields for note title, body format, and attachment IDs.

- Modify: `src/service/integrations/crm/records/hubspot/notes/note_normalizer.py`
  - Derive a stable note title.
  - Treat HubSpot note body as HTML when `hs_note_body` is present.
  - Preserve `hs_attachment_ids` as structured metadata.

- Modify: `src/service/integrations/crm/records/hubspot/notes/note_writer.py`
  - Write `CRMNote` as the source row.
  - Upsert the note source mapping as local object type `NOTE`.
  - Upsert a lightweight `CRMActivity` feed projection.
  - Upsert `CRMActivityLink` rows for resolved deal/contact/company associations.

- Modify: `src/service/integrations/webhooks/hubspot/tasks/handler.py`
  - Delete path marks `CRMTask` deleted and marks the matching feed projection deleted.

- Modify: `src/service/integrations/webhooks/hubspot/notes/handler.py`
  - Delete path marks `CRMNote` deleted and marks the matching feed projection deleted.

- Modify: `src/service/integrations/crm/records/hubspot/deletion_reconciliation_service.py`
  - Task and note reconciliation should use `CRMTask` and `CRMNote` as source tables.

- Modify: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`
  - Convert assertions from full `CRMActivity` source rows to `CRMTask` source rows plus `CRMActivity` feed projection.

- Modify: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`
  - Convert assertions from full `CRMActivity` source rows to `CRMNote` source rows plus `CRMActivity` feed projection.

- Modify: `tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py`
  - Update expected delete lookup columns for HubSpot tasks and notes.

- Add if missing: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_activity_projection.py`
  - Focused projection/link tests if existing writer tests become too dense.

- Add if missing: `tests/service/integrations/crm/records/hubspot/notes/test_hubspot_note_activity_projection.py`
  - Focused projection/link tests if existing writer tests become too dense.

## Shared Implementation Rules

1. Preserve tenant isolation in every query:

```python
col(Model.organisation_id) == self.organisation_id
```

and provider boundary:

```python
col(Model.integration_id) == self.integration_id
```

2. Full sync may soft-delete source rows missing from the snapshot. Single-record webhook/incremental refresh must not soft-delete unrelated rows.

3. Stale-write checks must compare provider modified time against the source row:

```python
row.source_modified_at >= task.source_modified_at
```

or:

```python
row.source_modified_at >= note.source_modified_at
```

4. Do not store the HubSpot provider ID in `crm_activities.source_record_id`.

Correct:

```python
activity.source_record_id = str(task.task_id)
```

Incorrect:

```python
activity.source_record_id = task.external_id
```

5. Keep full bodies on source tables:

```python
CRMTask.description = task.body
CRMNote.body = note.body
CRMActivity.preview = _preview_text(...)
```

6. `crm_activity_links.occurred_at` must copy the feed-card timestamp used on `crm_activities.occurred_at`.

7. Do not write fields dropped by migration `066` onto `CRMActivity` in new projection code. Feed projection writes should use only feed-safe fields such as `title`, `summary`, `preview`, `occurred_at`, `source_type`, `source_record_id`, `source_provider`, `source_integration_id`, `source_url`, `actor_name`, `actor_email`, `display_payload`, `activity_type`, and `is_deleted`.

8. Do not reintroduce a custom production backfill script for the legacy activity rows in this plan. Migration `065` already owns the legacy-row backfill and source-mapping rewrite.

---

### Task 0: Normalize HubSpot Fields For The New Source Tables

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_types.py`
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_normalizer.py`
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_types.py`
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_normalizer.py`
- Test: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`
- Test: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`

- [ ] **Step 1: Add task type to `NormalizedHubSpotTask`**

In `src/service/integrations/crm/records/hubspot/tasks/task_types.py`, add:

```python
task_type: str | None
```

immediately after:

```python
activity_subtype: str | None
```

- [ ] **Step 2: Add note title, body format, and attachments to `NormalizedHubSpotNote`**

In `src/service/integrations/crm/records/hubspot/notes/note_types.py`, add:

```python
title: str | None
body_format: str
attachment_ids: list[str]
```

immediately after:

```python
body: str | None
```

- [ ] **Step 3: Preserve HubSpot task type and priority in task normalization**

In `src/service/integrations/crm/records/hubspot/tasks/task_normalizer.py`, add `CRMActivityPriority` to the existing import:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityObjectType,
    CRMActivityPriority,
    CRMActivityStatus,
)
```

Replace:

```python
priority=None,
activity_subtype="HUBSPOT_TASK",
owner_external_id=_coerce_id(properties.get("hubspot_owner_id")),
```

with:

```python
priority=_normalize_priority(properties.get("hs_task_priority")),
activity_subtype="HUBSPOT_TASK",
task_type=_coerce_str(properties.get("hs_task_type")),
owner_external_id=_coerce_id(properties.get("hubspot_owner_id")),
```

Add:

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

- [ ] **Step 4: Preserve HubSpot note title, body format, and attachments**

In `src/service/integrations/crm/records/hubspot/notes/note_normalizer.py`, replace:

```python
subject=None,
body=_coerce_str(properties.get("hs_note_body")),
activity_subtype="HUBSPOT_NOTE",
```

with:

```python
subject=_derive_note_title(properties.get("hs_note_body")),
body=_coerce_str(properties.get("hs_note_body")),
title=_derive_note_title(properties.get("hs_note_body")),
body_format="html" if _coerce_str(properties.get("hs_note_body")) else "plain_text",
attachment_ids=_split_hubspot_ids(properties.get("hs_attachment_ids")),
activity_subtype="HUBSPOT_NOTE",
```

Add:

```python
def _derive_note_title(value: Any) -> str | None:
    body = _coerce_str(value)
    if body is None:
        return None
    without_tags = body.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    compact = " ".join(without_tags.split())
    if not compact:
        return None
    return compact[:80]


def _split_hubspot_ids(value: Any) -> list[str]:
    raw = _coerce_str(value)
    if raw is None:
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        cleaned = part.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ids.append(cleaned)
    return ids
```

- [ ] **Step 5: Update test factories for new normalized fields**

In the task writer test factory, add:

```python
task_type="TODO",
```

after:

```python
activity_subtype="HUBSPOT_TASK",
```

In the note writer test factory, add:

```python
title="Spoke with buyer offline",
body_format="html",
attachment_ids=["attachment-1"],
```

after:

```python
body="Spoke with buyer offline",
```

- [ ] **Step 6: Run focused writer tests and confirm expected failure**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected: FAIL only where the existing writers still target `CRMActivity` and do not yet map the new normalized fields into `CRMTask` or `CRMNote`.

---

### Task 1: Lock The HubSpot Task Source Mapping Contract

**Files:**
- Modify: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`
- Modify later: `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`

- [ ] **Step 1: Change the writer test imports**

Replace the old import:

```python
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
```

with:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityType,
    CRMTask,
)
```

This keeps `CRMActivity` available for feed-card assertions while the source row becomes `CRMTask`.

- [ ] **Step 2: Change the existing row fixtures from `CRMActivity` to `CRMTask`**

In `test_single_record_apply_does_not_soft_delete_unrelated_rows`, replace:

```python
updated = CRMActivity(
    activity_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    external_id="task-1",
    activity_type=CRMActivityType.TASK.value,
    is_deleted=False,
)
unrelated = CRMActivity(
    activity_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    external_id="task-2",
    activity_type=CRMActivityType.TASK.value,
    is_deleted=False,
)
```

with:

```python
updated = CRMTask(
    task_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Existing task",
    is_deleted=False,
)
unrelated = CRMTask(
    task_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Unrelated task",
    is_deleted=False,
)
```

In `test_writer_reactivates_existing_rows_and_soft_deletes_missing_rows`, replace the `CRMActivity(...)` fixtures with:

```python
reactivated = CRMTask(
    task_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Existing task",
    is_deleted=True,
)
stale = CRMTask(
    task_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Missing task",
    is_deleted=False,
)
```

- [ ] **Step 3: Change the task mapping assertion**

Replace:

```python
assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
    CRMSourceObjectType.ACTIVITY
)
```

with:

```python
assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
    CRMSourceObjectType.TASK
)
```

- [ ] **Step 4: Run the focused task writer test and confirm it fails**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py -v
```

Expected: FAIL. The writer still returns and mutates `CRMActivity` rows, and the mapping assertion still receives `CRMSourceObjectType.ACTIVITY`.

- [ ] **Step 5: Update `HubSpotTaskWriter` imports**

In `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`, replace:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityType,
    CRMSourceObjectType,
)
```

with:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityType,
    CRMSourceObjectType,
    CRMTask,
)
```

- [ ] **Step 6: Change `_load_existing_rows` to load `CRMTask` rows**

Replace the method signature:

```python
async def _load_existing_rows(self) -> dict[str, CRMActivity]:
```

with:

```python
async def _load_existing_rows(self) -> dict[str, CRMTask]:
```

Replace the select block with:

```python
stmt = select(CRMTask).where(
    col(CRMTask.organisation_id) == self.organisation_id,
    col(CRMTask.integration_id) == self.integration_id,
    col(CRMTask.task_id).in_(set(external_to_local.values())),
)
rows = (await self.session.execute(stmt)).scalars().all()
local_to_external = {
    local_id: external_id for external_id, local_id in external_to_local.items()
}
return {
    local_to_external[row.task_id]: row
    for row in rows
    if row.task_id in local_to_external
}
```

- [ ] **Step 7: Change `_upsert_source_mapping` to store local task IDs**

Replace:

```python
local_object_type=CRMSourceObjectType.ACTIVITY,
local_object_id=local_activity_id,
```

with:

```python
local_object_type=CRMSourceObjectType.TASK,
local_object_id=local_task_id,
```

Rename the parameter from:

```python
local_activity_id: UUID,
```

to:

```python
local_task_id: UUID,
```

and update call sites from:

```python
await self._upsert_source_mapping(task, row.activity_id, now=now)
```

to:

```python
await self._upsert_source_mapping(task, row.task_id, now=now)
```

- [ ] **Step 8: Run the focused task writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py -v
```

Expected: tests that only cover source-row update and source mapping now pass, or fail only because `_new_row`, `_apply_updates`, and `_is_stale_task` still type against `CRMActivity`. Fix those in Task 2.

---

### Task 2: Move HubSpot Task Source Row Fields To `CRMTask`

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`
- Test: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`

- [ ] **Step 1: Change `_new_row` return type and body**

Replace:

```python
) -> CRMActivity:
    return CRMActivity(
```

with:

```python
) -> CRMTask:
    return CRMTask(
```

Use this field mapping:

```python
return CRMTask(
    organisation_id=self.organisation_id,
    integration_id=self.integration_id,
    deal_id=deal_hit.internal_id if deal_hit is not None else None,
    title=task.subject or "Untitled task",
    description=task.body,
    status=task.status or "NOT_STARTED",
    priority=task.priority or "NONE",
    due_at=task.due_date,
    due_date=task.due_date.date() if task.due_date is not None else None,
    completed_at=task.completed_date,
    assigned_to_user_id=owner_hit.internal_id if owner_hit is not None else None,
    assigned_to_email=owner_hit.email if owner_hit is not None else None,
    assigned_to_name=owner_hit.name if owner_hit is not None else None,
    owner_user_id=owner_hit.internal_id if owner_hit is not None else None,
    owner_email=owner_hit.email if owner_hit is not None else None,
    source_created_at=task.source_created_at,
    source_modified_at=task.source_modified_at,
    is_deleted=False,
    task_metadata={
        "activity_subtype": task.activity_subtype,
        "hs_task_type": task.task_type,
        "properties": task.properties,
        "related_object_type": task.related_object_type,
        "related_external_id": task.related_external_id,
        "deal_external_id": task.deal_external_id,
        "contact_external_id": task.contact_external_id,
        "company_external_id": task.company_external_id,
    },
)
```

- [ ] **Step 2: Change `_apply_updates` to mutate `CRMTask`**

Change:

```python
row: CRMActivity,
```

to:

```python
row: CRMTask,
```

Replace the body with:

```python
row.deal_id = deal_hit.internal_id if deal_hit is not None else None
row.title = task.subject or "Untitled task"
row.description = task.body
row.status = task.status or "NOT_STARTED"
row.priority = task.priority or "NONE"
row.due_at = task.due_date
row.due_date = task.due_date.date() if task.due_date is not None else None
row.completed_at = task.completed_date
row.assigned_to_user_id = owner_hit.internal_id if owner_hit is not None else None
row.assigned_to_email = owner_hit.email if owner_hit is not None else None
row.assigned_to_name = owner_hit.name if owner_hit is not None else None
row.owner_user_id = owner_hit.internal_id if owner_hit is not None else None
row.owner_email = owner_hit.email if owner_hit is not None else None
row.source_created_at = task.source_created_at
row.source_modified_at = task.source_modified_at
row.is_deleted = False
row.task_metadata = {
    "activity_subtype": task.activity_subtype,
    "hs_task_type": task.task_type,
    "properties": task.properties,
    "related_object_type": task.related_object_type,
    "related_external_id": task.related_external_id,
    "deal_external_id": task.deal_external_id,
    "contact_external_id": task.contact_external_id,
    "company_external_id": task.company_external_id,
}
```

- [ ] **Step 3: Change `_is_stale_task` to accept `CRMTask`**

Replace:

```python
def _is_stale_task(row: CRMActivity, task: NormalizedHubSpotTask) -> bool:
```

with:

```python
def _is_stale_task(row: CRMTask, task: NormalizedHubSpotTask) -> bool:
```

Keep the existing function body.

- [ ] **Step 4: Update assertions in the existing task writer test**

Replace:

```python
assert reactivated.owner_id == owner_id
assert reactivated.deal_id == deal_id
assert reactivated.related_local_id == deal_id
assert reactivated.related_name == "Big Deal"
```

with:

```python
assert reactivated.owner_user_id == owner_id
assert reactivated.assigned_to_user_id == owner_id
assert reactivated.deal_id == deal_id
assert reactivated.task_metadata["related_object_type"] == "DEAL"
assert reactivated.task_metadata["deal_external_id"] == "deal-1"
```

- [ ] **Step 5: Run the focused task writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py -v
```

Expected: PASS for source-row behavior and mapping behavior. Projection assertions are added in Task 3.

---

### Task 3: Add HubSpot Task Feed Projection And Activity Links

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`
- Modify or Add: `tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py`

- [ ] **Step 1: Add helper methods for task projection**

Add these methods inside `HubSpotTaskWriter`:

```python
async def _upsert_activity_projection(
    self,
    *,
    task_row: CRMTask,
    task: NormalizedHubSpotTask,
    owner_hit: ActivityDimensionHit | None,
    now: datetime,
) -> CRMActivity:
    existing = await self._load_activity_projection(
        source_type=CRMActivityType.TASK.value,
        source_record_id=str(task_row.task_id),
    )
    occurred_at = task.completed_date or task.due_date or task.source_modified_at or task.source_created_at or now
    title = task.subject or "Task"
    preview = _preview_text(task.body or title)

    if existing is None:
        activity = CRMActivity(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            title=title,
            subject=title,
            summary=None,
            preview=preview,
            occurred_at=occurred_at,
            source_type=CRMActivityType.TASK.value,
            source_record_id=str(task_row.task_id),
            source_provider="hubspot",
            source_integration_id=self.integration_id,
            actor_name=owner_hit.name if owner_hit is not None else None,
            actor_email=owner_hit.email if owner_hit is not None else None,
            activity_type=CRMActivityType.TASK.value,
            is_deleted=bool(task_row.is_deleted),
            display_payload={
                "status": task_row.status,
                "priority": task_row.priority,
                "task_type": task.task_type,
                "due_at": task_row.due_at.isoformat() if task_row.due_at else None,
            },
        )
        self.session.add(activity)
        await self.session.flush()
        return activity

    existing.title = title
    existing.subject = title
    existing.preview = preview
    existing.occurred_at = occurred_at
    existing.source_provider = "hubspot"
    existing.source_integration_id = self.integration_id
    existing.actor_name = owner_hit.name if owner_hit is not None else None
    existing.actor_email = owner_hit.email if owner_hit is not None else None
    existing.activity_type = CRMActivityType.TASK.value
    existing.is_deleted = bool(task_row.is_deleted)
    existing.display_payload = {
        "status": task_row.status,
        "priority": task_row.priority,
        "task_type": task.task_type,
        "due_at": task_row.due_at.isoformat() if task_row.due_at else None,
    }
    return existing

async def _load_activity_projection(
    self,
    *,
    source_type: str,
    source_record_id: str,
) -> CRMActivity | None:
    stmt = select(CRMActivity).where(
        col(CRMActivity.organisation_id) == self.organisation_id,
        col(CRMActivity.integration_id) == self.integration_id,
        col(CRMActivity.source_type) == source_type,
        col(CRMActivity.source_record_id) == source_record_id,
    )
    return (await self.session.execute(stmt)).scalar_one_or_none()
```

- [ ] **Step 2: Add helper method for task links**

Add:

```python
async def _replace_activity_links(
    self,
    *,
    activity: CRMActivity,
    occurred_at: datetime,
    deal_hit: ActivityDimensionHit | None,
    contact_hit: ActivityDimensionHit | None,
    company_hit: ActivityDimensionHit | None,
) -> None:
    targets: list[tuple[str, UUID, str]] = []
    if deal_hit is not None:
        targets.append(("DEAL", deal_hit.internal_id, "PRIMARY"))
    if contact_hit is not None:
        targets.append(("CONTACT", contact_hit.internal_id, "RELATED"))
    if company_hit is not None:
        targets.append(("COMPANY", company_hit.internal_id, "RELATED"))

    for object_type, object_id, link_role in targets:
        stmt = select(CRMActivityLink).where(
            col(CRMActivityLink.organisation_id) == self.organisation_id,
            col(CRMActivityLink.activity_id) == activity.activity_id,
            col(CRMActivityLink.object_type) == object_type,
            col(CRMActivityLink.object_id) == object_id,
            col(CRMActivityLink.link_role) == link_role,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.occurred_at = occurred_at
            continue
        self.session.add(
            CRMActivityLink(
                organisation_id=self.organisation_id,
                activity_id=activity.activity_id,
                object_type=object_type,
                object_id=object_id,
                link_role=link_role,
                link_source="PROVIDER_ASSOCIATION",
                occurred_at=occurred_at,
            )
        )
```

- [ ] **Step 3: Call projection helpers after each source-row upsert**

After each new or updated `CRMTask` row is available, add:

```python
activity = await self._upsert_activity_projection(
    task_row=row,
    task=task,
    owner_hit=owner_hit,
    now=now,
)
await self._replace_activity_links(
    activity=activity,
    occurred_at=activity.occurred_at or now,
    deal_hit=deal_hit,
    contact_hit=contact_hit,
    company_hit=company_hit,
)
```

Place this before incrementing write counters for both create and update/reactivate paths.

- [ ] **Step 4: Add preview helper at module bottom**

Add:

```python
def _preview_text(value: str, *, limit: int = 500) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."
```

- [ ] **Step 5: Add test assertion that task projection is written**

In the task writer test, make `writer._load_activity_projection` return `None`:

```python
writer._load_activity_projection = AsyncMock(return_value=None)  # type: ignore[method-assign]
writer._replace_activity_links = AsyncMock()  # type: ignore[method-assign]
```

After `writer.apply(...)`, assert:

```python
added_rows = [call.args[0] for call in session.add.call_args_list]
activity_rows = [row for row in added_rows if isinstance(row, CRMActivity)]
assert activity_rows
assert activity_rows[0].source_type == CRMActivityType.TASK.value
assert activity_rows[0].source_record_id == str(reactivated.task_id)
assert activity_rows[0].source_record_id != "task-1"
```

- [ ] **Step 6: Run the focused task writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py -v
```

Expected: PASS.

---

### Task 4: Lock The HubSpot Note Source Mapping Contract

**Files:**
- Modify: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`
- Modify later: `src/service/integrations/crm/records/hubspot/notes/note_writer.py`

- [ ] **Step 1: Change the writer test imports**

Replace:

```python
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
```

with:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityType,
    CRMNote,
)
```

- [ ] **Step 2: Change existing note fixtures from `CRMActivity` to `CRMNote`**

Replace note source fixtures with:

```python
updated = CRMNote(
    note_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    body="Existing note",
    is_deleted=False,
)
unrelated = CRMNote(
    note_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    body="Unrelated note",
    is_deleted=False,
)
```

and:

```python
reactivated = CRMNote(
    note_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    body="Existing note",
    is_deleted=True,
)
stale = CRMNote(
    note_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    body="Missing note",
    is_deleted=False,
)
```

- [ ] **Step 3: Change note mapping assertion**

Replace:

```python
assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
    CRMSourceObjectType.ACTIVITY
)
```

with:

```python
assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
    CRMSourceObjectType.NOTE
)
```

- [ ] **Step 4: Run focused note writer test and confirm failure**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected: FAIL. The writer still targets `CRMActivity` and writes mappings as local object type `ACTIVITY`.

- [ ] **Step 5: Update `HubSpotNoteWriter` imports**

In `src/service/integrations/crm/records/hubspot/notes/note_writer.py`, replace:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityType,
    CRMSourceObjectType,
)
```

with:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityType,
    CRMNote,
    CRMSourceObjectType,
)
```

- [ ] **Step 6: Change `_load_existing_rows` to load `CRMNote` rows**

Replace the select block with:

```python
stmt = select(CRMNote).where(
    col(CRMNote.organisation_id) == self.organisation_id,
    col(CRMNote.integration_id) == self.integration_id,
    col(CRMNote.note_id).in_(set(external_to_local.values())),
)
rows = (await self.session.execute(stmt)).scalars().all()
local_to_external = {
    local_id: external_id for external_id, local_id in external_to_local.items()
}
return {
    local_to_external[row.note_id]: row
    for row in rows
    if row.note_id in local_to_external
}
```

- [ ] **Step 7: Change `_upsert_source_mapping` to store local note IDs**

Replace:

```python
local_object_type=CRMSourceObjectType.ACTIVITY,
local_object_id=local_activity_id,
```

with:

```python
local_object_type=CRMSourceObjectType.NOTE,
local_object_id=local_note_id,
```

Rename the parameter from:

```python
local_activity_id: UUID,
```

to:

```python
local_note_id: UUID,
```

and update call sites to pass `row.note_id`.

- [ ] **Step 8: Run focused note writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected: tests fail only on remaining source-row field mutations, fixed in Task 5.

---

### Task 5: Move HubSpot Note Source Row Fields To `CRMNote`

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_writer.py`
- Test: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`

- [ ] **Step 1: Change `_new_row` return type and body**

Replace the `CRMActivity(...)` construction with:

```python
return CRMNote(
    organisation_id=self.organisation_id,
    integration_id=self.integration_id,
    deal_id=deal_hit.internal_id if deal_hit is not None else None,
    title=note.title,
    body=note.body or "",
    body_format=note.body_format,
    author_user_id=owner_hit.internal_id if owner_hit is not None else None,
    author_name=owner_hit.name if owner_hit is not None else None,
    author_email=owner_hit.email if owner_hit is not None else None,
    visibility="TEAM",
    source_created_at=note.source_created_at,
    source_modified_at=note.source_modified_at,
    is_deleted=False,
    note_metadata={
        "activity_subtype": note.activity_subtype,
        "hs_attachment_ids": note.attachment_ids,
        "properties": note.properties,
        "related_object_type": note.related_object_type,
        "related_external_id": note.related_external_id,
        "deal_external_id": note.deal_external_id,
        "contact_external_id": note.contact_external_id,
        "company_external_id": note.company_external_id,
    },
)
```

- [ ] **Step 2: Change `_apply_updates` to mutate `CRMNote`**

Replace the body with:

```python
row.deal_id = deal_hit.internal_id if deal_hit is not None else None
row.title = note.title
row.body = note.body or ""
row.body_format = note.body_format
row.author_user_id = owner_hit.internal_id if owner_hit is not None else None
row.author_name = owner_hit.name if owner_hit is not None else None
row.author_email = owner_hit.email if owner_hit is not None else None
row.visibility = "TEAM"
row.source_created_at = note.source_created_at
row.source_modified_at = note.source_modified_at
row.is_deleted = False
row.note_metadata = {
    "activity_subtype": note.activity_subtype,
    "hs_attachment_ids": note.attachment_ids,
    "properties": note.properties,
    "related_object_type": note.related_object_type,
    "related_external_id": note.related_external_id,
    "deal_external_id": note.deal_external_id,
    "contact_external_id": note.contact_external_id,
    "company_external_id": note.company_external_id,
}
```

- [ ] **Step 3: Change `_is_stale_note` to accept `CRMNote`**

Replace:

```python
def _is_stale_note(row: CRMActivity, note: NormalizedHubSpotNote) -> bool:
```

with:

```python
def _is_stale_note(row: CRMNote, note: NormalizedHubSpotNote) -> bool:
```

- [ ] **Step 4: Update assertions in the existing note writer test**

Replace:

```python
assert reactivated.owner_id == owner_id
assert reactivated.deal_id == deal_id
assert reactivated.related_local_id == deal_id
assert reactivated.related_name == "Big Deal"
```

with:

```python
assert reactivated.author_user_id == owner_id
assert reactivated.deal_id == deal_id
assert reactivated.note_metadata["related_object_type"] == "DEAL"
assert reactivated.note_metadata["deal_external_id"] == "deal-1"
```

- [ ] **Step 5: Run the focused note writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected: PASS for source-row behavior and mapping behavior. Projection assertions are added in Task 6.

---

### Task 6: Add HubSpot Note Feed Projection And Activity Links

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/notes/note_writer.py`
- Modify or Add: `tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py`

- [ ] **Step 1: Add note projection helper**

Add inside `HubSpotNoteWriter`:

```python
async def _upsert_activity_projection(
    self,
    *,
    note_row: CRMNote,
    note: NormalizedHubSpotNote,
    owner_hit: ActivityDimensionHit | None,
    now: datetime,
) -> CRMActivity:
    existing = await self._load_activity_projection(
        source_type=CRMActivityType.NOTE.value,
        source_record_id=str(note_row.note_id),
    )
    occurred_at = note.source_modified_at or note.source_created_at or now
    title = note.title or "Note"
    preview = _preview_text(note.body or title)

    if existing is None:
        activity = CRMActivity(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            title=title,
            subject=title,
            summary=None,
            preview=preview,
            occurred_at=occurred_at,
            source_type=CRMActivityType.NOTE.value,
            source_record_id=str(note_row.note_id),
            source_provider="hubspot",
            source_integration_id=self.integration_id,
            actor_name=owner_hit.name if owner_hit is not None else None,
            actor_email=owner_hit.email if owner_hit is not None else None,
            activity_type=CRMActivityType.NOTE.value,
            is_deleted=bool(note_row.is_deleted),
            display_payload={
                "body_format": note_row.body_format,
                "visibility": note_row.visibility,
                "has_attachments": bool(note.attachment_ids),
            },
        )
        self.session.add(activity)
        await self.session.flush()
        return activity

    existing.title = title
    existing.subject = title
    existing.preview = preview
    existing.occurred_at = occurred_at
    existing.source_provider = "hubspot"
    existing.source_integration_id = self.integration_id
    existing.actor_name = owner_hit.name if owner_hit is not None else None
    existing.actor_email = owner_hit.email if owner_hit is not None else None
    existing.activity_type = CRMActivityType.NOTE.value
    existing.is_deleted = bool(note_row.is_deleted)
    existing.display_payload = {
        "body_format": note_row.body_format,
        "visibility": note_row.visibility,
        "has_attachments": bool(note.attachment_ids),
    }
    return existing
```

- [ ] **Step 2: Add note projection lookup**

Add:

```python
async def _load_activity_projection(
    self,
    *,
    source_type: str,
    source_record_id: str,
) -> CRMActivity | None:
    stmt = select(CRMActivity).where(
        col(CRMActivity.organisation_id) == self.organisation_id,
        col(CRMActivity.integration_id) == self.integration_id,
        col(CRMActivity.source_type) == source_type,
        col(CRMActivity.source_record_id) == source_record_id,
    )
    return (await self.session.execute(stmt)).scalar_one_or_none()
```

- [ ] **Step 3: Add note link helper**

Add the same `_replace_activity_links(...)` helper from Task 3, using `CRMActivityLink` and roles:

```python
("DEAL", deal_hit.internal_id, "PRIMARY")
("CONTACT", contact_hit.internal_id, "RELATED")
("COMPANY", company_hit.internal_id, "RELATED")
```

- [ ] **Step 4: Call projection helpers after note source-row upsert**

After each new or updated `CRMNote` row is available, add:

```python
activity = await self._upsert_activity_projection(
    note_row=row,
    note=note,
    owner_hit=owner_hit,
    now=now,
)
await self._replace_activity_links(
    activity=activity,
    occurred_at=activity.occurred_at or now,
    deal_hit=deal_hit,
    contact_hit=contact_hit,
    company_hit=company_hit,
)
```

- [ ] **Step 5: Add preview helper**

Add:

```python
def _preview_text(value: str, *, limit: int = 500) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."
```

- [ ] **Step 6: Add projection assertions to note writer test**

In the note writer test, stub projection lookup and link replacement:

```python
writer._load_activity_projection = AsyncMock(return_value=None)  # type: ignore[method-assign]
writer._replace_activity_links = AsyncMock()  # type: ignore[method-assign]
```

After `writer.apply(...)`, assert:

```python
added_rows = [call.args[0] for call in session.add.call_args_list]
activity_rows = [row for row in added_rows if isinstance(row, CRMActivity)]
assert activity_rows
assert activity_rows[0].source_type == CRMActivityType.NOTE.value
assert activity_rows[0].source_record_id == str(reactivated.note_id)
assert activity_rows[0].source_record_id != "note-1"
```

- [ ] **Step 7: Run the focused note writer test**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected: PASS.

---

### Task 7: Redirect HubSpot Task And Note Webhook Deletes

**Files:**
- Modify: `src/service/integrations/webhooks/hubspot/tasks/handler.py`
- Modify: `src/service/integrations/webhooks/hubspot/notes/handler.py`
- Modify: `tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py`

- [ ] **Step 1: Update delete test expected source columns**

In `tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py`, replace task case:

```python
lambda org_id, int_id, local_id: task_handler.CRMActivity(
    organisation_id=org_id,
    integration_id=int_id,
    activity_id=local_id,
    external_id="provider-id",
    activity_type=task_handler.CRMActivityType.TASK.value,
),
CRMSourceObjectType.TASK,
"crm_activities.activity_id",
```

with:

```python
lambda org_id, int_id, local_id: task_handler.CRMTask(
    organisation_id=org_id,
    integration_id=int_id,
    task_id=local_id,
    title="Provider task",
),
CRMSourceObjectType.TASK,
"crm_tasks.task_id",
```

Replace note case:

```python
lambda org_id, int_id, local_id: note_handler.CRMActivity(
    organisation_id=org_id,
    integration_id=int_id,
    activity_id=local_id,
    external_id="provider-id",
    activity_type=note_handler.CRMActivityType.NOTE.value,
),
CRMSourceObjectType.NOTE,
"crm_activities.activity_id",
```

with:

```python
lambda org_id, int_id, local_id: note_handler.CRMNote(
    organisation_id=org_id,
    integration_id=int_id,
    note_id=local_id,
    body="Provider note",
),
CRMSourceObjectType.NOTE,
"crm_notes.note_id",
```

- [ ] **Step 2: Run webhook delete test and confirm failure**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py -v
```

Expected: FAIL. Handlers still query `CRMActivity`.

- [ ] **Step 3: Update task delete handler imports**

In `src/service/integrations/webhooks/hubspot/tasks/handler.py`, replace:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityType,
    CRMSourceObjectType,
)
```

with:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityType,
    CRMSourceObjectType,
    CRMTask,
)
```

- [ ] **Step 4: Update `_soft_delete_task` source lookup**

Replace the statement with:

```python
statement = select(CRMTask).where(
    col(CRMTask.organisation_id) == organisation_id,
    col(CRMTask.integration_id) == integration_id,
    col(CRMTask.task_id) == activity_id,
    col(CRMTask.is_deleted).is_(False),
)
task = (await session.execute(statement)).scalars().first()
if task is None:
    return False

task.is_deleted = True
session.add(task)
await _soft_delete_activity_projection(
    session=session,
    organisation_id=organisation_id,
    integration_id=integration_id,
    source_type=CRMActivityType.TASK.value,
    source_record_id=str(task.task_id),
)
await session.commit()
return True
```

Rename local variable `activity_id` to `task_id` if preferred, but keep the mapping value tenant-scoped.

- [ ] **Step 5: Add task projection tombstone helper**

Add module-level helper:

```python
async def _soft_delete_activity_projection(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    source_type: str,
    source_record_id: str,
) -> None:
    statement = select(CRMActivity).where(
        col(CRMActivity.organisation_id) == organisation_id,
        col(CRMActivity.integration_id) == integration_id,
        col(CRMActivity.source_type) == source_type,
        col(CRMActivity.source_record_id) == source_record_id,
        col(CRMActivity.is_deleted).is_(False),
    )
    activity = (await session.execute(statement)).scalars().first()
    if activity is None:
        return
    activity.is_deleted = True
    session.add(activity)
```

- [ ] **Step 6: Update note delete handler imports and lookup**

In `src/service/integrations/webhooks/hubspot/notes/handler.py`, import `CRMNote` and replace the source lookup with:

```python
statement = select(CRMNote).where(
    col(CRMNote.organisation_id) == organisation_id,
    col(CRMNote.integration_id) == integration_id,
    col(CRMNote.note_id) == note_id,
    col(CRMNote.is_deleted).is_(False),
)
note = (await session.execute(statement)).scalars().first()
if note is None:
    return False

note.is_deleted = True
session.add(note)
await _soft_delete_activity_projection(
    session=session,
    organisation_id=organisation_id,
    integration_id=integration_id,
    source_type=CRMActivityType.NOTE.value,
    source_record_id=str(note.note_id),
)
await session.commit()
return True
```

Add the same `_soft_delete_activity_projection(...)` helper in the notes handler module.

- [ ] **Step 7: Run webhook delete test**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/hubspot/test_source_mapping_deletes.py -v
```

Expected: PASS.

---

### Task 8: Redirect HubSpot Deletion Reconciliation For Tasks And Notes

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/deletion_reconciliation_service.py`
- Test: `tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py`

- [ ] **Step 1: Inspect current reconciliation mapping**

Open:

```bash
Get-Content src/service/integrations/crm/records/hubspot/deletion_reconciliation_service.py
```

Confirm the mapping currently treats `"tasks"` and `"notes"` as `CRMActivity`.

- [ ] **Step 2: Update imports**

Replace the activity-only import with:

```python
from src.domain.models.organisation_tables.sync.crm.activities import (
    CRMActivity,
    CRMActivityType,
    CRMNote,
    CRMTask,
)
```

- [ ] **Step 3: Change source model maps**

Use:

```python
_OBJECT_MODEL_BY_TYPE = {
    "companies": CRMCompany,
    "contacts": CRMContact,
    "deals": CRMDeal,
    "tasks": CRMTask,
    "notes": CRMNote,
}

_LOCAL_ID_COLUMN_BY_TYPE = {
    "companies": CRMCompany.company_id,
    "contacts": CRMContact.contact_id,
    "deals": CRMDeal.deal_id,
    "tasks": CRMTask.task_id,
    "notes": CRMNote.note_id,
}
```

Keep any `CRMActivity` use only for projection cleanup after source rows are marked deleted.

- [ ] **Step 4: Add projection cleanup for reconciled task/note deletes**

When reconciliation marks a task or note source row deleted, also tombstone projection rows with:

```python
source_type = CRMActivityType.TASK.value if object_type == "tasks" else CRMActivityType.NOTE.value
source_record_ids = [str(local_id) for local_id in deleted_local_ids]
stmt = select(CRMActivity).where(
    col(CRMActivity.organisation_id) == organisation_id,
    col(CRMActivity.integration_id) == integration_id,
    col(CRMActivity.source_type) == source_type,
    col(CRMActivity.source_record_id).in_(source_record_ids),
    col(CRMActivity.is_deleted).is_(False),
)
for activity in (await session.execute(stmt)).scalars().all():
    activity.is_deleted = True
```

Use the existing local variables in the service; do not introduce a new reconciliation service.

- [ ] **Step 5: Run focused reconciliation tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py -v
```

Expected: PASS or a focused failure showing existing unit fixtures still expect `CRMActivity`. Update only those fixture expectations for task/note cases.

---

### Task 9: Verify Migration 065 And 066 Compatibility

**Files:**
- Modify if needed: `tests/migrations/test_ai_native_activity_migration_contract.py`
- Test: `tests/migrations/test_ai_native_activity_migration_contract.py`

- [ ] **Step 1: Confirm migration 065 already backfills source rows**

Open:

```bash
Get-Content migrations/org_migrations/versions/3c7f79637734_065_backfill_legacy_activity_tasks_notes.py
```

Confirm it contains all of these statements:

```text
INSERT INTO crm_tasks
INSERT INTO crm_notes
local_object_type = CASE WHEN a.activity_type = 'NOTE' THEN 'note' ELSE 'task' END
source_record_id = activity_id::text
INSERT INTO crm_activity_links
```

- [ ] **Step 2: Confirm migration 066 removes legacy activity payload columns**

Open:

```bash
Get-Content migrations/org_migrations/versions/3c7f79637733_066_drop_legacy_activity_task_note_columns.py
```

Confirm it drops these legacy columns from `crm_activities`:

```text
body
status
priority
activity_subtype
owner_name
owner_email
owner_id
external_assignee_email
external_assignee_name
source
created_by_user_id
due_date
completed_date
reminder_date
source_created_at
source_modified_at
synced_at
last_seen_at
properties
metrics
```

- [ ] **Step 3: Add a migration-contract assertion if missing**

If `tests/migrations/test_ai_native_activity_migration_contract.py` does not already assert the mapping rewrite, add:

```python
def test_legacy_task_note_backfill_repoints_source_mappings() -> None:
    source = _read_migration("3c7f79637734_065_backfill_legacy_activity_tasks_notes.py")

    assert "UPDATE crm_source_object_mappings" in source
    assert "local_object_type = CASE WHEN a.activity_type = 'NOTE' THEN 'note' ELSE 'task' END" in source
    assert "m.local_object_type = 'activity'" in source
    assert "a.activity_type IN ('TASK', 'TODO', 'NOTE')" in source
```

- [ ] **Step 4: Add a dropped-column contract assertion if missing**

If `tests/migrations/test_ai_native_activity_migration_contract.py` does not already assert the legacy drop, add:

```python
def test_activity_payload_columns_are_removed_after_source_backfill() -> None:
    source = _read_migration("3c7f79637733_066_drop_legacy_activity_task_note_columns.py")

    for column_name in [
        "body",
        "status",
        "priority",
        "owner_id",
        "due_date",
        "source_created_at",
        "source_modified_at",
        "synced_at",
        "last_seen_at",
        "properties",
        "metrics",
    ]:
        assert f'op.drop_column("crm_activities", "{column_name}")' in source
```

- [ ] **Step 5: Run migration contract tests**

Run:

```bash
uv run pytest tests/migrations/test_ai_native_activity_migration_contract.py -v
```

Expected: PASS. If this fails because the model still exposes columns dropped by migration `066`, stop implementation and fix the schema/model mismatch before changing HubSpot writers.

---

### Task 10: Run Focused Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run HubSpot task writer tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/tasks/test_hubspot_task_writer.py -v
```

Expected: PASS.

- [ ] **Step 2: Run HubSpot note writer tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/notes/test_note_writer.py -v
```

Expected: PASS.

- [ ] **Step 3: Run HubSpot webhook tests**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/hubspot -v
```

Expected: PASS.

- [ ] **Step 4: Run HubSpot reconciliation tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_hubspot_deletion_reconciliation_tasks.py -v
```

Expected: PASS.

- [ ] **Step 5: Run schema contract tests**

Run:

```bash
uv run pytest tests/test_ai_native_activity_schema.py tests/migrations/test_ai_native_activity_migration_contract.py -v
```

Expected: PASS.

- [ ] **Step 6: Run ruff only after focused tests pass**

Run:

```bash
uv run ruff check src/service/integrations/crm/records/hubspot src/service/integrations/webhooks/hubspot tests/service/integrations/crm/records/hubspot tests/service/integrations/webhooks/hubspot
```

Expected: PASS.

---

## Production Risks And Guardrails

1. Existing HubSpot task/note mappings may already be rewritten by migration `065`.
   - Guardrail: writers must load source rows through `CRMSourceObjectType.TASK` / `CRMSourceObjectType.NOTE`; do not add a second custom mapping backfill in this implementation.

2. Existing read paths may still expect task/note data on legacy `crm_activities` columns that migration `066` removes.
   - Guardrail: run schema/migration contract tests first. If model/read-path compatibility is broken by `066`, stop and fix that schema/model/read boundary before changing HubSpot writers.

3. Webhook delete can leave a feed row active if only the source row is tombstoned.
   - Guardrail: delete handlers tombstone both source row and projection row in the same session.

4. Full sync can accidentally delete unrelated records if single-record paths call full-snapshot behavior.
   - Guardrail: preserve `deactivate_missing=False` in `apply_single(...)`.

5. HubSpot task statuses may not fit `crm_tasks.ck_crm_task_status`.
   - Guardrail: keep normalizer output limited to `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, `CANCELLED`, or expand the check constraint in a separate schema task before writing other statuses.

6. Provider ID can be written to the wrong place.
   - Guardrail: `crm_source_object_mappings.external_id` stores HubSpot ID; `crm_activities.source_record_id` stores local `task_id` or `note_id`.

7. New note/task fields can be lost if the writer only ports the old `CRMActivity` assignments.
   - Guardrail: normalize and assert first-class fields: note `title`, `body_format`, `visibility`, attachment IDs; task `priority`, `hs_task_type`, due/completed timestamps, owner and assignee fields.

8. Projection code can accidentally keep writing columns dropped from `crm_activities`.
   - Guardrail: projection snippets must not set `source`, `synced_at`, `last_seen_at`, `source_created_at`, `source_modified_at`, `properties`, `metrics`, `body`, `status`, or `priority` on `CRMActivity`.

## Self-Review

Spec coverage:

- HubSpot task source persistence: Tasks 1, 2, 3.
- HubSpot note source persistence: Tasks 4, 5, 6.
- HubSpot task/note first-class field preservation: Task 0, Task 2, Task 5.
- Webhook deletes: Task 7.
- Deletion reconciliation: Task 8.
- Migration `065`/`066` compatibility: Task 9.
- Verification: Task 10.

Placeholder scan:

- No task relies on unspecified future behavior.
- No separate backfill script is planned because migration `065` already owns the legacy activity-to-source-table backfill.

Type consistency:

- HubSpot task provider mapping points to `CRMSourceObjectType.TASK` and local `CRMTask.task_id`.
- HubSpot note provider mapping points to `CRMSourceObjectType.NOTE` and local `CRMNote.note_id`.
- Feed rows point to local source row IDs through `CRMActivity.source_record_id`.
- HubSpot note title/body format/attachments are normalized before writer persistence.
- HubSpot task priority/type are normalized before writer persistence.
