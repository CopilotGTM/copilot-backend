# Salesforce Tasks Notes Source Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the Salesforce task/note move to `crm_tasks` and `crm_notes`, keeping `crm_activities` as the feed projection instead of the source table.

**Architecture:** Current code already writes Salesforce tasks to `CRMTask`, Salesforce notes to `CRMNote`, and projects timeline rows through `ActivityFeedProjectionService`. The remaining work is to close stale-contract gaps: clamp Salesforce task statuses to the `crm_tasks` constraint, update stale Salesforce writer tests, move Salesforce CDC delete helpers to the source tables, make CDC deletes idempotent when mappings were already tombstoned, and verify feed projections carry compact display metadata plus the object links needed by deal/contact/company timelines.

**Tech Stack:** Python 3.11, SQLModel, async SQLAlchemy sessions, PostgreSQL tenant schemas, Celery Salesforce record sync tasks, pytest.

---

## Current Code Facts

The latest commit `476c6909 Refactor CRM task and note source APIs` already moved the broad API and provider-writer direction toward source tables. Do not plan a full rewrite from `CRMActivity` to source tables; that would duplicate work already present.

Confirmed source-table writes:

- `src/service/integrations/crm/records/salesforce/tasks/task_writer.py` imports `CRMTask`, bulk-upserts `CRMTask`, writes `CRMSourceObjectType.TASK` mappings, and calls `ActivityFeedProjectionService.upsert_task`.
- `src/service/integrations/crm/records/salesforce/notes/note_writer.py` imports `CRMNote`, bulk-upserts `CRMNote`, writes `CRMSourceObjectType.NOTE` and `CRMSourceObjectType.NOTE_LINK` mappings, and calls `ActivityFeedProjectionService.upsert_note`.
- Salesforce enhanced-note realtime refresh must subscribe to `ContentDocumentChangeEvent`, not `ContentNoteChangeEvent`. The event record ID is still the note document ID that `SalesforceNotesFetcher.fetch_one_by_note_id` uses to query scoped `ContentDocumentLink` rows and hydrate the body/title from `ContentNote`.
- `tests/service/crm/test_task_service_boundaries.py` and `tests/service/crm/test_notes_service_boundaries.py` already enforce provider writers should not insert/select `CRMActivity` or use `local_object_type=CRMSourceObjectType.ACTIVITY`.
- `src/service/crm/activity_feed_projection_service.py` owns feed rows in `crm_activities`; source records own full task/note data. The current worktree already has task/note `display_payload` wiring, so implementation should verify and harden it rather than blindly add a second payload layer.
- `src/domain/models/organisation_tables/sync/crm/source_mappings.py` and `migrations/org_migrations/versions/3c7f79637743_056_add_crm_source_object_mappings.py` already include `CRMSourceObjectType.NOTE_LINK` / `'note_link'` in the mapping check constraints. No extra migration is needed for Salesforce `ContentDocumentLink.Id` mappings.
- `src/domain/models/organisation_tables/sync/crm/activities.py` and `migrations/org_migrations/versions/3c7f79637735_064_ai_native_crm_activity_tables.py` require these source-table fields: `CRMTask.title`, `CRMTask.status`, `CRMTask.priority`, `CRMNote.body`, `CRMNote.body_format`, and `CRMNote.visibility`. Salesforce writers currently populate the required task values and note body; note format/visibility rely on model/database defaults.

Remaining gaps found in the current checkout:

- `src/service/integrations/crm/records/salesforce/tasks/task_normalizer.py` can emit `WAITING` and `DEFERRED`, but `crm_tasks.status` only allows `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, and `CANCELLED`.
- Salesforce writer tests still construct `CRMActivity` fixtures and assert `CRMSourceObjectType.ACTIVITY` as the local mapping type.
- Salesforce writer tests still expect `ON CONFLICT (activity_id)` and `crm_activities.deal_id IN`, but current writers use `CRMTask.task_id`, `CRMNote.note_id`, `crm_tasks`, and `crm_notes`.
- `src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py` still soft-deletes task/note CDC deletes by selecting `CRMActivity`.
- CDC delete tests still use `CRMActivity` rows for `_soft_delete_task`, `_soft_delete_note`, and `_soft_delete_note_by_content_document_link_id`.
- `ContentDocumentLinkChangeEvent` delete handling can tombstone a note after one link disappears without proving no other in-scope links remain for the same `ContentDocumentId`.
- If the existing bad CDC delete path tombstoned a `CRMSourceObjectMapping` but left `CRMTask`/`CRMNote` active, a later fixed delete path that only calls `mark_external_ids_deleted()` will not find the local source row.
- `ActivityFeedProjectionService.upsert_task` in the current worktree already falls back to `CRMTask.assigned_to_name`, `CRMTask.assigned_to_email`, and `CRMTask.owner_email` for feed actors. Keep that behavior and add regression coverage instead of rewriting it.
- `ActivityFeedProjectionService` currently syncs only the primary deal `crm_activity_links` row. Salesforce writers resolve contact and company IDs into `task_metadata` / `note_metadata`, but those IDs do not become `CONTACT` or `COMPANY` activity links. A CDC note or task linked only to a contact/account can be correctly stored in `crm_notes`/`crm_tasks` but missing from contact/company timelines.
- `ActivityFeedProjectionService._upsert_activity` currently writes `display_payload or {}` without stripping `None` values. That leaves noisy nulls in the feed snapshot when optional task/note fields are absent.

## Source-Table Contract

Hard rules:

- Salesforce `Task.Id` maps as `external_object_type=CRMSourceObjectType.TASK`, `local_object_type=CRMSourceObjectType.TASK`, `local_object_id=CRMTask.task_id`.
- Salesforce enhanced-note document IDs (`ContentDocumentChangeEvent.recordIds` / `ContentNote.Id`) map as `external_object_type=CRMSourceObjectType.NOTE`, `local_object_type=CRMSourceObjectType.NOTE`, `local_object_id=CRMNote.note_id`.
- Salesforce `ContentDocumentLink.Id` maps as `external_object_type=CRMSourceObjectType.NOTE_LINK`, `local_object_type=CRMSourceObjectType.NOTE`, `local_object_id=CRMNote.note_id`.
- `CRMActivity.activity_id` may equal `CRMTask.task_id` or `CRMNote.note_id` only as a projection identity. New Salesforce task/note source writes must not insert or update `CRMActivity` directly.
- Source-row deletes must also tombstone the matching feed projection through `ActivityFeedProjectionService.soft_delete_activity_projection`.
- Keep provider IDs in `crm_source_object_mappings.external_id`; `CRMActivity.source_record_id` stores the local source row ID string.
- Salesforce task statuses written to `CRMTask.status` must be one of `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, or `CANCELLED`. Preserve Salesforce's raw status in `task_metadata.raw_status`.
- Resolved `contact_id` and `company_id` values in source metadata must create/update `crm_activity_links` rows with `object_type='CONTACT'` or `object_type='COMPANY'`, `link_role='RELATED'`, and `link_source='SYSTEM'`. When those metadata values disappear, remove the stale link for that object type.

Field mapping to preserve:

| Salesforce field | Source table target | Feed projection target |
| --- | --- | --- |
| `Task.Subject` | `CRMTask.title` | `CRMActivity.title` |
| `Task.Description` | `CRMTask.description` | `CRMActivity.preview` |
| `Task.Status` | `CRMTask.status` | `CRMActivity.display_payload.status` |
| `Task.Priority` | `CRMTask.priority` | `CRMActivity.display_payload.priority` |
| `Task.ActivityDate` | `CRMTask.due_at` and `due_date` | `CRMActivity.occurred_at` |
| `Task.CompletedDateTime` | `CRMTask.completed_at` | `CRMActivity.display_payload.completed_at` |
| `Task.OwnerId` | owner/assignee fields on `CRMTask` | `CRMActivity.actor_*` when available |
| `Task.WhoId` contact | `CRMTask.task_metadata.contact_id` | `CRMActivityLink` CONTACT RELATED row |
| `Task.AccountId` / account `WhatId` | `CRMTask.task_metadata.company_id` | `CRMActivityLink` COMPANY RELATED row |
| `ContentNote.Title` | `CRMNote.title` | `CRMActivity.title` |
| `ContentNote.Content` | `CRMNote.body` | `CRMActivity.preview` |
| `ContentDocumentChangeEvent.recordIds[]` | `CRMSourceObjectType.NOTE` lookup, then `ContentNote` hydration | drives note projection refresh |
| `ContentDocumentLink.Id` | `CRMSourceObjectType.NOTE_LINK` mapping | no direct feed field |
| `ContentDocumentLink.LinkedEntityId` contact/account | `CRMNote.note_metadata.contact_id` / `company_id` | `CRMActivityLink` CONTACT or COMPANY RELATED row |

---

### Task 1: Normalize Salesforce Task Status For `crm_tasks`

**Files:**
- Modify: `src/service/integrations/crm/records/salesforce/tasks/task_normalizer.py`
- Modify: `tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_normalizer.py`

- [ ] **Step 1: Add a failing constraint-parity test**

Add this test:

```python
def test_normalize_tasks_emits_only_crm_task_status_values() -> None:
    raw_rows = [
        {"Id": "00T_WAITING", "Status": "Waiting on someone else"},
        {"Id": "00T_DEFERRED", "Status": "Deferred"},
        {"Id": "00T_CANCELLED", "Status": "Canceled"},
        {"Id": "00T_PROGRESS", "Status": "In Progress"},
    ]

    normalized, skipped = normalize_tasks(raw_rows)

    assert skipped == 0
    assert [task.status for task in normalized] == [
        "IN_PROGRESS",
        "NOT_STARTED",
        "CANCELLED",
        "IN_PROGRESS",
    ]
    assert [task.properties["raw_status"] for task in normalized] == [
        "Waiting on someone else",
        "Deferred",
        "Canceled",
        "In Progress",
    ]
```

- [ ] **Step 2: Run the normalizer test to verify it fails**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_normalizer.py::test_normalize_tasks_emits_only_crm_task_status_values -v
```

Expected before implementation: FAIL because the normalizer emits `WAITING` and `DEFERRED`.

- [ ] **Step 3: Clamp Salesforce statuses to the source-table constraint**

Change `_normalize_status` so provider-specific statuses stay in raw metadata and `CRMTask.status` only gets allowed values:

```python
def _normalize_status(value: Any) -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    canonical = raw.strip().lower()
    if canonical in {"completed", "closed"}:
        return CRMActivityStatus.COMPLETED.value
    if canonical in {"in progress", "started", "waiting on someone else"}:
        return CRMActivityStatus.IN_PROGRESS.value
    if canonical in {"cancelled", "canceled"}:
        return CRMActivityStatus.CANCELLED.value
    return CRMActivityStatus.NOT_STARTED.value
```

Remove the `DEFERRED` return from this Salesforce source-table path.

- [ ] **Step 4: Run the focused normalizer tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_normalizer.py -v
```

Expected after implementation: PASS.

### Task 2: Bring Salesforce Task Writer Tests Up To The Source-Table Contract

**Files:**
- Modify: `tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_writer.py`
- No production code changes expected unless the test exposes a real writer bug.

- [ ] **Step 1: Replace task fixtures with `CRMTask`**

Change imports:

```python
from src.domain.models.organisation_tables.sync.crm import CRMTask
```

Remove `CRMActivity` and `CRMActivityType` from this test file unless another assertion still needs them.

Use source-table rows in the reactivation test:

```python
reactivated = CRMTask(
    task_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Old task",
    is_deleted=True,
)
stale = CRMTask(
    task_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Old stale task",
    is_deleted=False,
)
```

- [ ] **Step 2: Assert source-table fields instead of legacy activity fields**

Replace legacy assertions:

```python
assert reactivated.owner_id == owner_id
assert reactivated.contact_id == contact_id
assert reactivated.company_id == company_id
assert reactivated.related_local_id == deal_id
assert reactivated.related_name == "Big Deal"
```

with source-table assertions:

```python
assert reactivated.assigned_to_user_id == owner_id
assert reactivated.owner_user_id == owner_id
assert reactivated.assigned_to_name == "Alex Morgan"
assert reactivated.assigned_to_email == "alex@example.com"
assert reactivated.deal_id == deal_id
assert reactivated.task_metadata["contact_id"] == str(contact_id)
assert reactivated.task_metadata["company_id"] == str(company_id)
assert reactivated.task_metadata["related_local_id"] == str(deal_id)
assert reactivated.task_metadata["related_name"] == "Big Deal"
```

- [ ] **Step 3: Assert source mappings point to `CRMTask`**

Replace:

```python
assert mapped_rows[0].local_object_type == CRMSourceObjectType.ACTIVITY
assert mapped_rows[0].local_object_id == reactivated.activity_id
```

with:

```python
assert mapped_rows[0].local_object_type == CRMSourceObjectType.TASK
assert mapped_rows[0].local_object_id == reactivated.task_id
```

- [ ] **Step 4: Update SQL-shape assertions**

Replace `activity_id` conflict assertions with:

```python
assert "ON CONFLICT (task_id)" in sql
```

Replace scoped deactivation table assertions with:

```python
assert "crm_tasks.deal_id IN" in sql
```

For the stale-column test, keep assertions that unrelated task-table columns are not overwritten:

```python
assert "created_by_user_id = excluded.created_by_user_id" not in sql
assert "reminder_at = excluded.reminder_at" not in sql
assert "completed_by_user_id = excluded.completed_by_user_id" not in sql
```

- [ ] **Step 5: Fix `_row_values` calls**

Current test calls pass `now=now`, but `_row_values` accepts `last_seen_at`. Use:

```python
rows = [
    writer._row_values(
        task=_normalized_task(external_id=f"00T{i}", deal_external_id="006OPPTY"),
        owner_hit=None,
        deal_hit=None,
        contact_hit=None,
        company_hit=None,
        related_local_id=None,
        related_name=None,
        last_seen_at=now,
    )
    for i in range(1000)
]
```

- [ ] **Step 6: Run focused task writer tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_writer.py -v
```

Expected before this task: FAIL from stale `CRMActivity` assumptions. Expected after this task: PASS.

### Task 3: Bring Salesforce Note Writer Tests Up To The Source-Table Contract

**Files:**
- Modify: `tests/service/integrations/crm/records/salesforce/notes/test_salesforce_note_writer.py`
- No production code changes expected unless the test exposes a real writer bug.

- [ ] **Step 1: Replace note fixtures with `CRMNote`**

Change imports:

```python
from src.domain.models.organisation_tables.sync.crm import CRMNote
```

Use source-table rows:

```python
reactivated = CRMNote(
    note_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Old note",
    body="Old body",
    is_deleted=True,
)
stale = CRMNote(
    note_id=uuid4(),
    organisation_id=organisation_id,
    integration_id=integration_id,
    title="Old stale note",
    body="Old stale body",
    is_deleted=False,
)
```

- [ ] **Step 2: Assert source-table fields instead of legacy activity fields**

Replace legacy owner/object assertions with:

```python
assert reactivated.author_user_id == owner_id
assert reactivated.author_name == "Alex Morgan"
assert reactivated.author_email == "alex@example.com"
assert reactivated.deal_id == deal_id
assert reactivated.note_metadata["contact_id"] == str(contact_id)
assert reactivated.note_metadata["company_id"] == str(company_id)
assert reactivated.note_metadata["related_local_id"] == str(deal_id)
assert reactivated.note_metadata["related_name"] == "Big Deal"
```

- [ ] **Step 3: Assert source mappings point to `CRMNote`**

Replace:

```python
assert mapped_rows[0].local_object_type == CRMSourceObjectType.ACTIVITY
assert mapped_rows[0].local_object_id == reactivated.activity_id
```

with:

```python
assert mapped_rows[0].local_object_type == CRMSourceObjectType.NOTE
assert mapped_rows[0].local_object_id == reactivated.note_id
```

In the `NOTE_LINK` test, replace:

```python
assert mapped_rows[1].local_object_type == CRMSourceObjectType.ACTIVITY
```

with:

```python
assert mapped_rows[1].local_object_type == CRMSourceObjectType.NOTE
```

- [ ] **Step 4: Update SQL-shape assertions**

Replace `activity_id` conflict assertions with:

```python
assert "ON CONFLICT (note_id)" in sql
```

Replace scoped deactivation table assertions with:

```python
assert "crm_notes.deal_id IN" in sql
```

For stale-column coverage, keep note-table-specific assertions:

```python
assert "body_format = excluded.body_format" not in sql
assert "visibility = excluded.visibility" in sql
```

- [ ] **Step 5: Fix `_row_values` calls**

Use:

```python
rows = [
    writer._row_values(
        note=_normalized_note(external_id=f"069NOTE{i}", deal_external_id="006OPPTY"),
        owner_hit=None,
        deal_hit=None,
        contact_hit=None,
        company_hit=None,
        related_local_id=None,
        related_name=None,
        last_seen_at=now,
    )
    for i in range(1000)
]
```

- [ ] **Step 6: Run focused note writer tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/notes/test_salesforce_note_writer.py -v
```

Expected before this task: FAIL from stale `CRMActivity` assumptions. Expected after this task: PASS.

### Task 4: Move Salesforce CDC Task/Note Deletes To Source Tables

**Files:**
- Modify: `src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py`
- Modify: `tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py`

- [ ] **Step 1: Import source models and projection service**

In `salesforce_cdc_subscriber_task.py`, import:

```python
from src.domain.models.organisation_tables.sync.crm import (
    CRMNote,
    CRMSourceObjectType,
    CRMTask,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
```

Remove the old `CRMActivity` and `CRMActivityType` imports from this file after task/note CDC deletes move to `CRMTask` and `CRMNote`.

- [ ] **Step 2: Add idempotent source ID resolution**

Add a helper near `_mark_source_mapping_deleted`:

```python
async def _resolve_deleted_source_id(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    object_type: CRMSourceObjectType,
    external_id: str,
) -> UUID | None:
    local_id = await _mark_source_mapping_deleted(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_type=object_type,
        external_id=external_id,
    )
    if local_id is not None:
        return local_id

    local_ids = await CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    ).load_local_id_map(
        object_type=object_type,
        external_ids=[external_id],
        include_deleted=True,
    )
    return local_ids.get(external_id.strip())
```

This closes the partial-rollout loophole where a prior CDC delete committed a deleted mapping while leaving the source row active.

- [ ] **Step 3: Update `_soft_delete_task`**

Use `_resolve_deleted_source_id(... object_type=CRMSourceObjectType.TASK ...)` and treat the returned UUID as `task_id`:

```python
task_id = await _resolve_deleted_source_id(
    session=session,
    organisation_id=organisation_id,
    integration_id=integration_id,
    object_type=CRMSourceObjectType.TASK,
    external_id=external_id,
)
if task_id is None:
    return

stmt = select(CRMTask).where(
    col(CRMTask.organisation_id) == organisation_id,
    col(CRMTask.integration_id) == integration_id,
    col(CRMTask.task_id) == task_id,
    col(CRMTask.is_deleted) == False,  # noqa: E712
)
task = (await session.execute(stmt)).scalar_one_or_none()
if task is None:
    return
task.is_deleted = True
session.add(task)
await ActivityFeedProjectionService(
    session,
    organisation_id,
).soft_delete_activity_projection(task.task_id)
await session.commit()
return
```

- [ ] **Step 4: Update `_soft_delete_note`**

Use `_resolve_deleted_source_id(... object_type=CRMSourceObjectType.NOTE ...)` and treat the returned UUID as `note_id`:

```python
note_id = await _resolve_deleted_source_id(
    session=session,
    organisation_id=organisation_id,
    integration_id=integration_id,
    object_type=CRMSourceObjectType.NOTE,
    external_id=external_id,
)
if note_id is None:
    return

stmt = select(CRMNote).where(
    col(CRMNote.organisation_id) == organisation_id,
    col(CRMNote.integration_id) == integration_id,
    col(CRMNote.note_id) == note_id,
    col(CRMNote.is_deleted) == False,  # noqa: E712
)
note = (await session.execute(stmt)).scalar_one_or_none()
if note is None:
    return
note.is_deleted = True
session.add(note)
await ActivityFeedProjectionService(
    session,
    organisation_id,
).soft_delete_activity_projection(note.note_id)
await session.commit()
return
```

- [ ] **Step 5: Update `_soft_delete_note_by_content_document_link_id`**

The `NOTE_LINK` source mapping should still resolve the linked note's local ID. Select `CRMNote` by `note_id`, not `CRMActivity.activity_id`:

```python
note_id = await _resolve_deleted_source_id(
    session=session,
    organisation_id=organisation_id,
    integration_id=integration_id,
    object_type=CRMSourceObjectType.NOTE_LINK,
    external_id=content_document_link_id,
)
if note_id is None:
    return

stmt = select(CRMNote).where(
    col(CRMNote.organisation_id) == organisation_id,
    col(CRMNote.integration_id) == integration_id,
    col(CRMNote.note_id) == note_id,
    col(CRMNote.is_deleted) == False,  # noqa: E712
)
note = (await session.execute(stmt)).scalar_one_or_none()
if note is None:
    return
note.is_deleted = True
session.add(note)
await ActivityFeedProjectionService(
    session,
    organisation_id,
).soft_delete_activity_projection(note.note_id)
await session.commit()
return
```

- [ ] **Step 6: Update CDC delete tests**

In `tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py`, change the parametrized row factories:

```python
(
    "_soft_delete_task",
    "external_id",
    lambda org_id, int_id, local_id: CRMTask(
        organisation_id=org_id,
        integration_id=int_id,
        task_id=local_id,
        title="Provider task",
    ),
    CRMSourceObjectType.TASK,
    "crm_tasks.task_id",
    ("is_deleted", True),
),
(
    "_soft_delete_note",
    "external_id",
    lambda org_id, int_id, local_id: CRMNote(
        organisation_id=org_id,
        integration_id=int_id,
        note_id=local_id,
        body="Provider note",
    ),
    CRMSourceObjectType.NOTE,
    "crm_notes.note_id",
    ("is_deleted", True),
),
(
    "_soft_delete_note_by_content_document_link_id",
    "content_document_link_id",
    lambda org_id, int_id, local_id: CRMNote(
        organisation_id=org_id,
        integration_id=int_id,
        note_id=local_id,
        body="Provider note",
    ),
    CRMSourceObjectType.NOTE_LINK,
    "crm_notes.note_id",
    ("is_deleted", True),
),
```

Add projection assertion support to `_FakeDeleteSession` or monkeypatch `ActivityFeedProjectionService`:

```python
class _FakeProjectionService:
    calls: list[UUID] = []

    def __init__(self, session, organisation_id):  # noqa: ANN001
        self.session = session
        self.organisation_id = organisation_id

    async def soft_delete_activity_projection(self, activity_id: UUID) -> None:
        self.calls.append(activity_id)
```

Assert task/note/delete-by-link cases call projection with the local source ID.

- [ ] **Step 7: Add idempotent deleted-mapping coverage**

Add a test where `mark_external_ids_deleted()` returns `{}` but `load_local_id_map(... include_deleted=True)` returns the local task or note ID. Assert the source row is still tombstoned and the feed projection is soft-deleted.

Use this fake mapping helper shape:

```python
class _FakeAlreadyDeletedMappingHelper:
    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.kwargs = kwargs

    async def mark_external_ids_deleted(self, *, object_type, external_ids):  # noqa: ANN001
        return {}

    async def load_local_id_map(self, *, object_type, external_ids=None, include_deleted=False):
        assert include_deleted is True
        assert list(external_ids) == ["provider-id"]
        return {"provider-id": _FakeDeleteMappingHelper.local_id}
```

- [ ] **Step 8: Run CDC tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py -v
```

Expected before this task: FAIL because task/note deletes query `crm_activities`. Expected after this task: PASS.

### Task 5: Fix Salesforce `ContentDocumentLink` Delete Semantics

**Files:**
- Modify: `src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py`
- Modify: `tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py`

- [ ] **Step 1: Add a failing multi-link delete test**

Add a CDC test where the deleted link belongs to an in-scope deal, but another active in-scope link still exists for the same `ContentDocumentId`:

```python
@pytest.mark.asyncio
async def test_content_document_link_delete_rehydrates_note_when_other_scoped_links_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeNotesFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            pass

        async def fetch_one_by_link_id(self, link_id: str, *, linked_entity_ids):
            return []

        async def fetch_link_by_id(self, link_id: str):
            return {
                "Id": link_id,
                "ContentDocumentId": "069xx0000000001AAA",
                "LinkedEntityId": "006xx0000000001AAA",
            }

        async def fetch_one_by_note_id(self, note_id: str, *, linked_entity_ids):
            calls.append({"rehydrate_note": note_id, "linked_entity_ids": linked_entity_ids})
            return [
                {
                    "Id": note_id,
                    "ContentDocumentId": note_id,
                    "ContentDocumentLinkId": "06Axx0000000002AAA",
                    "LinkedEntityId": "006xx0000000002AAA",
                    "Title": "Still linked note",
                }
            ]

    class FakeNoteService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def _load_linked_entity_ids(self, organisation_id, integration_id):  # noqa: ANN001
            return ["006xx0000000001AAA", "006xx0000000002AAA"]

        async def persist_single_note(self, **kwargs):
            calls.append({"persist": kwargs})

    async def fail_delete(**_kwargs):
        raise AssertionError("note still has an in-scope link and must not be deleted")

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceNotesFetcher", FakeNotesFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceNoteRecordSyncService",
        FakeNoteService,
    )
    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_soft_delete_note", fail_delete)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "_soft_delete_note_by_content_document_link_id",
        fail_delete,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="06Axx0000000001AAA",
        change_type="DELETE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/ContentDocumentLinkChangeEvent",
    )

    assert calls[-1]["persist"]["raw_notes"][0]["ContentDocumentLinkId"] == (
        "06Axx0000000002AAA"
    )
```

- [ ] **Step 2: Rehydrate by note ID before deleting**

In `_process_content_document_link_record_change`, after `fetch_link_by_id()` returns a scoped deleted link, call:

```python
remaining_records = await fetcher.fetch_one_by_note_id(
    content_document_id,
    linked_entity_ids=linked_entity_ids,
)
if remaining_records:
    await service.persist_single_note(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_notes=remaining_records,
    )
    return
```

Only call `_soft_delete_note(...)` when there are no remaining scoped records for the same note.

- [ ] **Step 3: Run the CDC subscriber tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py -v
```

Expected after implementation: PASS.

### Task 6: Add Salesforce Source-Table Boundary Tests

**Files:**
- Modify: `tests/service/crm/test_task_service_boundaries.py`
- Modify: `tests/service/crm/test_notes_service_boundaries.py`

- [ ] **Step 1: Enforce CDC task deletes do not select `CRMActivity`**

Add to `test_task_service_boundaries.py`:

```python
def test_salesforce_cdc_task_delete_targets_crm_tasks_not_activity_rows() -> None:
    source = Path(
        "src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py"
    ).read_text()
    task_delete_source = source.split("async def _soft_delete_task", 1)[1].split(
        "async def _deactivate_user", 1
    )[0]

    assert "CRMTask" in task_delete_source
    assert "select(CRMActivity)" not in task_delete_source
    assert "CRMActivityType.TASK" not in task_delete_source
```

- [ ] **Step 2: Enforce CDC note deletes do not select `CRMActivity`**

Add to `test_notes_service_boundaries.py`:

```python
def test_salesforce_cdc_note_deletes_target_crm_notes_not_activity_rows() -> None:
    source = Path(
        "src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py"
    ).read_text()
    note_delete_source = source.split("async def _soft_delete_note", 1)[1].split(
        "async def _soft_delete_note_by_content_document_link_id", 1
    )[0]
    link_delete_source = source.split(
        "async def _soft_delete_note_by_content_document_link_id", 1
    )[1].split("async def _mark_source_mapping_deleted", 1)[0]

    assert "CRMNote" in note_delete_source
    assert "CRMNote" in link_delete_source
    assert "select(CRMActivity)" not in note_delete_source
    assert "select(CRMActivity)" not in link_delete_source
    assert "CRMActivityType.NOTE" not in note_delete_source
    assert "CRMActivityType.NOTE" not in link_delete_source
```

- [ ] **Step 3: Run boundary tests**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py tests/service/crm/test_notes_service_boundaries.py -v
```

Expected after implementation: PASS.

### Task 7: Lock Feed Projection Payloads, Actor Fallbacks, And Object Links

**Files:**
- Modify: `src/service/crm/activity_feed_projection_service.py`
- Modify: `tests/service/crm/test_activity_feed_projection_service.py`

- [ ] **Step 1: Expand task projection tests for payload, actor fallback, and object links**

In `tests/service/crm/test_activity_feed_projection_service.py`, expand the task projection test so it builds a `CRMTask` with `assigned_to_name`, `assigned_to_email`, status, priority, completion timestamp, contact metadata, and company metadata. Assert the compiled insert uses the local source ID, compact display payload, current actor fallback, and object-link upserts:

```python
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.sql.dml import Delete


def _compiled_values(statement: object) -> dict[str, object]:
    return statement.compile().params


@pytest.mark.asyncio
async def test_task_projection_uses_source_id_payload_and_assignee_actor() -> None:
    organisation_id = uuid4()
    task_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    session = AsyncMock()

    task = CRMTask(
        task_id=task_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Follow up",
        description="Call buyer",
        status="IN_PROGRESS",
        priority="HIGH",
        completed_at=datetime(2026, 6, 22, 10, 30, tzinfo=UTC),
        assigned_to_name="Alex Morgan",
        assigned_to_email="alex@example.com",
        task_metadata={
            "provider": "salesforce",
            "task_type": "TASK",
            "contact_id": str(contact_id),
            "company_id": str(company_id),
        },
    )

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    values = _compiled_values(session.execute.await_args_list[0].args[0])
    assert values["activity_id"] == task_id
    assert values["source_record_id"] == str(task_id)
    assert values["source_provider"] == "salesforce"
    assert values["actor_name"] == "Alex Morgan"
    assert values["actor_email"] == "alex@example.com"
    assert values["display_payload"] == {
        "status": "IN_PROGRESS",
        "priority": "HIGH",
        "task_type": "TASK",
        "completed_at": "2026-06-22T10:30:00+00:00",
    }

    link_values = [
        _compiled_values(call.args[0])
        for call in session.execute.await_args_list
        if "crm_activity_links" in str(call.args[0])
        and not isinstance(call.args[0], Delete)
    ]
    assert {
        (values["object_type"], values["object_id"], values["link_role"], values["link_source"])
        for values in link_values
    } == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }
```

- [ ] **Step 2: Add a stale-link deletion test**

Add a test proving absent metadata removes old related links for that object type. This catches the production bug where a provider update removes a contact/account relationship but the timeline link stays visible:

```python
@pytest.mark.asyncio
async def test_task_projection_removes_stale_contact_and_company_links() -> None:
    organisation_id = uuid4()
    task_id = uuid4()
    session = AsyncMock()
    task = CRMTask(
        task_id=task_id,
        organisation_id=organisation_id,
        integration_id=uuid4(),
        title="Follow up",
        task_metadata={"provider": "salesforce"},
    )

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    delete_params = [
        call.args[0].compile().params
        for call in session.execute.await_args_list
        if isinstance(call.args[0], Delete)
    ]
    assert any("CONTACT" in params.values() for params in delete_params)
    assert any("COMPANY" in params.values() for params in delete_params)
```

- [ ] **Step 3: Keep source columns as actor fallback**

In `ActivityFeedProjectionService.upsert_task`, keep metadata actors as an override but fall back to source-table assignment fields:

```python
actor_name = (
    metadata.get("actor_name") if isinstance(metadata.get("actor_name"), str) else None
) or task.assigned_to_name
actor_email = (
    metadata.get("actor_email") if isinstance(metadata.get("actor_email"), str) else None
) or task.assigned_to_email or task.owner_email
```

- [ ] **Step 4: Preserve completed timestamp only in compact task payload**

Keep `CRMTask.completed_at` in the source table, but expose the timestamp to the feed card as a compact string:

```python
display_payload={
    "status": task.status,
    "priority": task.priority,
    "task_type": metadata.get("hs_task_type") or metadata.get("task_type"),
    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
}
```

- [ ] **Step 5: Add projection tests for note payload and related object links**

Create a test that builds a `CRMNote` with Salesforce metadata and asserts:

```python
assert values["source_record_id"] == str(note.note_id)
assert values["source_provider"] == "salesforce"
assert values["display_payload"] == {
    "body_format": "plain_text",
    "visibility": "TEAM",
    "has_attachments": False,
}
```

Also assert a note with `note_metadata={"contact_id": str(contact_id), "company_id": str(company_id)}` creates the same `CONTACT` and `COMPANY` `RELATED` links as the task test.

- [ ] **Step 6: Add related object link helpers**

In `src/service/crm/activity_feed_projection_service.py`, add helpers below `_sync_primary_deal_link`:

```python
def _uuid_from_metadata(metadata: dict[str, Any], key: str) -> UUID | None:
    value = metadata.get(key)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
```

```python
async def _sync_related_object_link(
    self,
    *,
    activity_id: UUID,
    object_type: str,
    object_id: UUID | None,
    occurred_at: datetime,
) -> None:
    link_c = cast(Any, CRMActivityLink).__table__.c
    base_where = (
        link_c.organisation_id == self.organisation_id,
        link_c.activity_id == activity_id,
        link_c.object_type == object_type,
        link_c.link_role == "RELATED",
    )
    if object_id is None:
        await self.session.execute(delete(CRMActivityLink).where(*base_where))
        return

    await self.session.execute(
        delete(CRMActivityLink).where(*base_where, link_c.object_id != object_id)
    )
    stmt = pg_insert(CRMActivityLink).values(
        organisation_id=self.organisation_id,
        activity_id=activity_id,
        object_type=object_type,
        object_id=object_id,
        link_role="RELATED",
        link_source="SYSTEM",
        occurred_at=occurred_at,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_crm_activity_link_target_role",
        set_={"occurred_at": occurred_at},
    )
    await self.session.execute(stmt)
```

Call it from `upsert_task` and `upsert_note` after `_sync_primary_deal_link`:

```python
await self._sync_related_object_link(
    activity_id=task.task_id,
    object_type=CRMActivityObjectType.CONTACT.value,
    object_id=_uuid_from_metadata(metadata, "contact_id"),
    occurred_at=occurred_at,
)
await self._sync_related_object_link(
    activity_id=task.task_id,
    object_type=CRMActivityObjectType.COMPANY.value,
    object_id=_uuid_from_metadata(metadata, "company_id"),
    occurred_at=occurred_at,
)
```

Use `note.note_id` for note calls.

- [ ] **Step 7: Filter `None` values from display payloads**

In `_upsert_activity`, normalize payload values once:

```python
clean_display_payload = {
    key: value for key, value in (display_payload or {}).items() if value is not None
}
```

Use `clean_display_payload` for both insert values and conflict update.

- [ ] **Step 8: Keep the feed row lightweight**

Do not add full `CRMNote.body`, full `CRMTask.description`, raw Salesforce payloads, `ContentDocumentLink` rows, or provider IDs to `display_payload`.

- [ ] **Step 9: Run projection-adjacent tests**

Run:

```bash
uv run pytest tests/service/crm/test_activity_feed_projection_service.py tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_writer.py tests/service/integrations/crm/records/salesforce/notes/test_salesforce_note_writer.py tests/test_ai_native_activity_schema.py -v
```

Expected after implementation: PASS.

### Task 8: Verify Source Mapping And New Source-Table Constraints

**Files:**
- Modify: `tests/test_ai_native_activity_schema.py`
- Modify: `tests/migrations/test_ai_native_activity_migration_contract.py`

- [ ] **Step 1: Add model-level source mapping tests for `note_link`**

In `tests/test_ai_native_activity_schema.py`, add:

```python
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectMapping


def test_source_mapping_allows_note_link_for_salesforce_content_document_links() -> None:
    external_sql = _check_constraint_sql(
        CRMSourceObjectMapping,
        "ck_crm_source_mapping_external_type",
    )
    local_sql = _check_constraint_sql(
        CRMSourceObjectMapping,
        "ck_crm_source_mapping_local_type",
    )

    assert "'note_link'" in external_sql
    assert "'note_link'" in local_sql
```

- [ ] **Step 2: Add model-level source-table required-field tests**

In `tests/test_ai_native_activity_schema.py`, add:

```python
def test_task_and_note_source_tables_have_required_defaults_for_provider_writes() -> None:
    assert CRMTask.__table__.c.title.nullable is False
    assert CRMTask.__table__.c.status.nullable is False
    assert CRMTask.__table__.c.status.server_default is not None
    assert CRMTask.__table__.c.priority.nullable is False
    assert CRMTask.__table__.c.priority.server_default is not None
    assert CRMTask.__table__.c.task_metadata.nullable is False
    assert CRMTask.__table__.c.task_metadata.server_default is not None

    assert CRMNote.__table__.c.body.nullable is False
    assert CRMNote.__table__.c.body_format.nullable is False
    assert CRMNote.__table__.c.body_format.server_default is not None
    assert CRMNote.__table__.c.visibility.nullable is False
    assert CRMNote.__table__.c.visibility.server_default is not None
    assert CRMNote.__table__.c.note_metadata.nullable is False
    assert CRMNote.__table__.c.note_metadata.server_default is not None
```

- [ ] **Step 3: Add migration-level source mapping test for `note_link`**

In `tests/migrations/test_ai_native_activity_migration_contract.py`, add:

```python
def test_source_mapping_migration_allows_note_link() -> None:
    source = Path(
        "migrations/org_migrations/versions/3c7f79637743_056_add_crm_source_object_mappings.py"
    ).read_text()

    assert "'note_link'" in source
    assert "ck_crm_source_mapping_external_type" in source
    assert "ck_crm_source_mapping_local_type" in source
```

- [ ] **Step 4: Run schema and migration contract tests**

Run:

```bash
uv run pytest tests/test_ai_native_activity_schema.py tests/migrations/test_ai_native_activity_migration_contract.py -v
```

Expected after implementation: PASS.

### Task 9: Verify Initial-Sync Scope Is Intentional

**Files:**
- Modify: `docs/superpowers/plans/2026-06-22-salesforce-tasks-notes-source-tables.md` only if the product decision changes.
- Test: `tests/service/integrations/crm/records/salesforce/tasks/test_task_sync_service.py`
- Test: `tests/service/integrations/crm/records/salesforce/notes/test_note_sync_service.py`
- Test: `tests/service/integrations/crm/records/salesforce/test_activity_scope.py`

- [ ] **Step 1: Confirm the current scope contract before changing code**

Current code intentionally scopes initial Salesforce task/note sync to eligible deal external IDs:

```python
deal_external_ids = await self._load_initial_sync_deal_external_ids(
    organisation_id,
    integration_id,
)
```

Do not broaden this plan to account-only or contact-only Salesforce activities unless the product requirement is changed from deal-timeline activity ingestion to all CRM activity ingestion.

- [ ] **Step 2: Keep CDC note scope broader than initial sync**

CDC note sync already calls `_load_linked_entity_ids()` and includes deals, contacts, and companies. Preserve that behavior because a changed note/link event can arrive for any already-synced CRM dimension row.

- [ ] **Step 3: Run scope tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/tasks/test_task_sync_service.py tests/service/integrations/crm/records/salesforce/notes/test_note_sync_service.py tests/service/integrations/crm/records/salesforce/test_activity_scope.py -v
```

Expected after implementation: PASS. If the product decision changes to sync all account/contact-only activities, write a separate plan because that expands fetch volume, deactivation semantics, and timeline linking beyond this source-table migration.

### Task 10: Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run focused Salesforce normalizer and writer tests**

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_normalizer.py tests/service/integrations/crm/records/salesforce/tasks/test_salesforce_task_writer.py tests/service/integrations/crm/records/salesforce/notes/test_salesforce_note_writer.py -v
```

- [ ] **Step 2: Run Salesforce CDC tests**

```bash
uv run pytest tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py tests/workers/integrations_worker/test_salesforce_record_sync_locks.py tests/workers/integrations_worker/test_salesforce_task_limits.py -v
```

- [ ] **Step 3: Run source-table boundary and schema tests**

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py tests/service/crm/test_notes_service_boundaries.py tests/service/crm/test_activity_feed_projection_service.py tests/test_ai_native_activity_schema.py tests/migrations/test_ai_native_activity_migration_contract.py -v
```

- [ ] **Step 4: Run Salesforce scope tests**

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/tasks/test_task_sync_service.py tests/service/integrations/crm/records/salesforce/notes/test_note_sync_service.py tests/service/integrations/crm/records/salesforce/test_activity_scope.py -v
```

- [ ] **Step 5: Run targeted ruff**

```bash
uv run ruff check src/service/crm/activity_feed_projection_service.py src/service/integrations/crm/records/salesforce src/workers/integrations_worker/tasks/crm/salesforce/salesforce_cdc_subscriber_task.py tests/service/integrations/crm/records/salesforce tests/workers/integrations_worker/test_salesforce_cdc_subscriber_task.py tests/service/crm/test_task_service_boundaries.py tests/service/crm/test_notes_service_boundaries.py tests/service/crm/test_activity_feed_projection_service.py tests/test_ai_native_activity_schema.py tests/migrations/test_ai_native_activity_migration_contract.py
```

---

## Production Risks And Guardrails

- A stale CDC delete path can mark only the feed row while leaving `crm_tasks`/`crm_notes` active. The source row must be tombstoned first, then the feed projection.
- A previously tombstoned source mapping can hide an active source row from a fixed CDC delete path. Delete helpers must fall back to `include_deleted=True` mapping lookup before giving up.
- `NOTE_LINK` deletes are subtle: `ContentDocumentLink.Id` is not the note ID. Resolve it through `CRMSourceObjectType.NOTE_LINK`, then tombstone the local `CRMNote.note_id` returned by the mapping helper.
- A Salesforce note can have multiple `ContentDocumentLink` rows. Deleting one link must rehydrate the note if another in-scope link remains; only delete the note when no scoped links remain.
- Salesforce `WAITING` and `DEFERRED` task statuses do not fit the current `crm_tasks.status` check constraint. Normalize them into allowed app states and keep the raw provider status in metadata.
- Do not rewrite initial sync orchestration unless a test proves it is broken. Current task/note sync services already instantiate the source-table writers.
- Do not add new migrations for this plan. The source tables and mapping object types already exist in the current branch.
- Do not store raw Salesforce note bodies, task descriptions, or raw payloads in `CRMActivity.display_payload`; the feed row is a compact projection.
- Do not rely on `deal_id` alone for feed visibility. Salesforce contact/account activity context is stored in task/note metadata today, so projection must materialize `CONTACT` and `COMPANY` `crm_activity_links` rows from those metadata IDs.
- Keep organisation and integration filters on every CDC delete query.

## Self-Review

Spec coverage:

- Salesforce task writes to `crm_tasks`: current implementation plus Task 2 test hardening.
- Salesforce note writes to `crm_notes`: current implementation plus Task 3 test hardening.
- Salesforce task status check-constraint safety: Task 1.
- Salesforce CDC delete path: Task 4.
- Salesforce multi-link note delete safety: Task 5.
- Regression guardrails against future `CRMActivity` source writes: Task 6.
- Feed projection remains lightweight, actor-safe, and linked to resolved deal/contact/company context: Task 7.
- New source-table and source-mapping constraints are locked by tests: Task 8.
- Initial-sync scope decision is explicit: Task 9.
- Verification: Task 10.

Placeholder scan:

- No step depends on unspecified future work.
- No backfill step is included because existing migrations already introduce source tables and source mapping object types.

Type consistency:

- Task source rows use `CRMTask.task_id`.
- Note source rows use `CRMNote.note_id`.
- Feed rows still use `CRMActivity.activity_id` equal to the local source ID for projection only.
- Source mappings use `CRMSourceObjectType.TASK`, `CRMSourceObjectType.NOTE`, and `CRMSourceObjectType.NOTE_LINK`, never `CRMSourceObjectType.ACTIVITY` for Salesforce task/note source rows.
