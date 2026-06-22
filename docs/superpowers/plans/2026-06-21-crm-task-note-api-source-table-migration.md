# CRM Task and Note API Source Table Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repoint the Tasks API to `crm_tasks` first, then repoint the Deal Notes API to `crm_notes`, leaving `crm_activities` as the feed-card projection plus `crm_activity_links` as the object-timeline index.

**Architecture:** Task and note source rows become the API source of truth. API writes update the source row and maintain a small `crm_activities` projection row with `source_type`, `source_record_id`, `title`, `summary`, `preview`, `occurred_at`, and one primary `crm_activity_links` row when a deal is attached. Route paths and response contracts stay stable unless the new source-table constraints make the old contract impossible.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL, pytest.

---

## Grounded Context

The current task and note APIs still use `CRMActivity` as the task/note source row:

- `src/service/crm/tasks/read_service.py` selects `CRMActivity` and filters `activity_type IN ('TASK', 'TODO')`.
- `src/service/crm/tasks/write_service.py` creates, updates, and deletes `CRMActivity` rows.
- `src/service/crm/notes/read_service.py` selects `CRMActivity` and filters `activity_type == NOTE`.
- `src/service/crm/notes/write_service.py` creates, updates, and deletes `CRMActivity` rows.
- `src/api/routers/crm/tasks_handler.py` maps activity-shaped fields such as `activity_id`, `subject`, `body`, `due_date`, `reminder_date`, `owner_*`, `properties`, and `metrics`.
- `src/api/routers/crm/deal_notes_handler.py` maps activity-shaped fields such as `activity_id`, `subject`, `body`, `owner_*`, and `synced_at`.

The finalized schema now has:

- `CRMTask` / `crm_tasks`: full task source state with `task_id`, `title`, `description`, `status`, `priority`, `due_date`, `due_at`, `reminder_at`, `completed_at`, `assigned_to_*`, `created_by_user_id`, `owner_user_id`, `owner_email`, `source_created_at`, `source_modified_at`, and `task_metadata`.
- `CRMNote` / `crm_notes`: full note source state with `note_id`, `title`, `body`, `body_format`, `author_*`, `visibility`, `source_created_at`, `source_modified_at`, and `note_metadata`.
- `CRMActivity` / `crm_activities`: feed-card projection row.
- `CRMActivityLink` / `crm_activity_links`: object-to-activity timeline links, indexed by `(organisation_id, object_type, object_id, occurred_at, activity_id)`.

Important loopholes to close during execution:

- `crm_tasks.status` currently allows only `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, and `CANCELLED`. The task API currently exposes `WAITING` and `DEFERRED`; execution must either remove those from the API options or expand the DB constraint in a schema task. For this API-only plan, use the DB constraint as source of truth and stop offering `WAITING` / `DEFERRED`.
- `crm_tasks` does not have `task_type`, `subject`, `body`, `reminder_date`, `completed_date`, `owner_name`, `external_assignee_*`, `properties`, or `metrics` columns. The router must map old API field names from `CRMTask` source fields instead of using `TaskListItem.model_validate(task, from_attributes=True)`.
- `crm_notes.title` is optional. For display, return `subject = note.title or first 30 characters of normalized body`.
- `UserReminder.activity_id` has no foreign key. Keep using that column to store the task UUID for now, but the task code should pass `CRMTask.task_id`, not `CRMActivity.activity_id`.
- Existing provider ingestion still writes legacy `CRMActivity` task/note rows in this branch. This plan is API-level first. Provider writers should get a separate follow-up plan unless the execution scope is explicitly expanded.

## Pre-Implementation Loophole Review

These are hard gates found by comparing the plan with the current branch before implementation:

- `migrations/org_migrations/versions/3c7f79637733_066_drop_legacy_activity_task_note_columns.py` drops legacy task/note columns from `crm_activities`, but the current `CRMActivity` ORM model and multiple provider/internal writers still reference those columns. Do not ship this API implementation together with migration `066` unless the ORM and every writer that touches dropped columns are migrated in the same release. For API-only execution, keep migration `066` gated out of production deployment.
- `TaskWriteService` is not API-only in practice. Agent tools and bot form handlers call it too (`create_task`, voice tool manifest, `confirm_task_handler`). Either keep the return DTO backward-compatible for those callers or update their tests/call sites in this plan before merging.
- `DealTaskAutomationService`, provider task/note writers, and deletion reconciliation still write/read task and note rows through `CRMActivity`. This plan must not claim system-wide source-table migration; it is limited to the FastAPI task/note services plus direct callers touched by those services.
- Public user IDs and CRM user IDs are different identity spaces. Any `owner_user_id` API filter or write path must resolve/store both deliberately; do not compare a public `users.user_id` directly to `CRMTask.assigned_to_user_id` unless that value has first been resolved to a CRM user ID.
- Activity projection deletes must not insert tombstone-only `crm_activities` rows. If a source row has no existing projection, a delete should be a no-op plus link cleanup, not a stub feed card.
- `CRMActivityLink.object_id != None` is not a safe way to clear old deal links. When a task/note is detached from a deal, delete all primary deal links for that source activity explicitly before returning.
- Assignee is not actor. Projection rows should not set `actor_name` / `actor_email` from `CRMTask.assigned_to_*`; use explicit actor metadata when present or leave actor fields null.
- Text-only boundary tests are useful tripwires but insufficient. Add at least one runtime mapper/import test for each API phase so SQLModel field mismatches and DTO caller breakage are caught.

## File Structure

### Create

- `src/service/crm/activity_feed_projection_service.py`
  - Owns upsert/delete of feed-card projection rows and deal links for source-table writes.

### Modify In Task Phase

- `src/agents/tools/tasks/create_task.py`
  - Preserve compatibility with the `TaskWriteService` return shape or update the tool mapper.
- `src/service/bots/form_handlers/confirm_task_handler.py`
  - Preserve compatibility with `TaskWriteService.create_task()` returning a `CRMTask` source row.
- `src/service/crm/tasks/contracts.py`
  - Return `CRMTask` and a read DTO instead of `CRMActivity`.
- `src/service/crm/tasks/read_service.py`
  - Query `CRMTask` and join/decorate deal names.
- `src/service/crm/tasks/write_service.py`
  - Create/update/delete `CRMTask`; call projection service after source row mutation.
- `src/service/crm/tasks/reminder_service.py`
  - Accept `CRMTask` and use `task.task_id`.
- `src/service/crm/tasks/notification_service.py`
  - Accept `CRMTask` and map title/description fields.
- `src/api/routers/crm/tasks_handler.py`
  - Map `TaskReadRecord` / `CRMTask` to `TaskListItem` and `TaskDetail`.
- `src/api/schemas/crm_task_models.py`
  - Keep public response shape stable; adjust docs/descriptions where old activity-backed fields changed meaning.
- `tests/service/crm/test_task_service_boundaries.py`
  - Assert task services no longer use `CRMActivity` as their source table.
- `tests/api/routers/crm/conftest.py`
  - Update task fakes to look like `CRMTask` plus read records.
- `tests/api/routers/crm/test_tasks_handler.py`
  - Preserve current route behavior while asserting source-table field mapping.
- `docs/API Documentation/deals/deal-tasks-api.md`
  - Document `crm_tasks` source semantics and the reduced status set.

### Modify In Notes Phase

- `src/service/crm/notes/contracts.py`
  - Return `CRMNote` and a read DTO.
- `src/service/crm/notes/read_service.py`
  - Query `CRMNote`.
- `src/service/crm/notes/write_service.py`
  - Create/update/delete `CRMNote`; call projection service.
- `src/api/routers/crm/deal_notes_handler.py`
  - Map `CRMNote` to `DealNoteItem`; use the 30-character title fallback.
- `src/api/schemas/crm_note_models.py`
  - Keep response field names stable.
- `tests/service/crm/test_notes_service_boundaries.py`
  - Assert notes services no longer use `CRMActivity` as their source table.
- `tests/api/routers/crm/conftest.py`
  - Update note fakes to look like `CRMNote` plus read records.
- `tests/api/routers/crm/test_deal_notes_handler.py`
  - Add fallback-title and source-table mapping tests.
- `docs/API Documentation/deals/deal-notes-api.md`
  - Document `crm_notes` source semantics and fallback subject behavior.

---

## Task 0: Preflight Current Callers and Migration Gate

**Files:**
- Inspect only first, then modify this plan if new blockers are found.

- [ ] **Step 1: Confirm this branch is not ready to apply migration `066` with API-only changes**

Run:

```bash
rg -n "CRMActivity|activity_type|subject|body|due_date|reminder_date|completed_date|last_seen_at|properties|metrics" src/service src/agents src/workers tests
```

Expected: provider writers, internal task/note services, agent tools, bot form handlers, and tests still reference activity-shaped task/note fields. Treat migration `066` as release-gated until those writers are either migrated or the `CRMActivity` ORM remains compatible with the deployed schema.

- [ ] **Step 2: Enumerate direct `TaskWriteService` callers**

Run:

```bash
rg -n "TaskWriteService|create_task\\(" src/api src/agents src/service tests
```

Expected: the API is not the only caller. Add or preserve a compatibility layer so agent tools and bot form handlers do not break when `TaskWriteService` returns `CRMTask`.

- [ ] **Step 3: Decide the release boundary before coding**

For this plan, use this boundary:

- FastAPI task and deal-note services move to `crm_tasks` / `crm_notes`.
- Direct callers of those services are kept compatible.
- Provider ingestion, `DealTaskAutomationService`, deletion reconciliation, and migration `066` remain follow-up work unless the user explicitly expands scope.

Do not mark the API migration production-ready unless this boundary is repeated in the final verification notes.

---

## Task 1: Add Task API Source-Table Boundary Tests

**Files:**
- Modify: `tests/service/crm/test_task_service_boundaries.py`

- [ ] **Step 1: Add failing boundary tests**

Add these tests to `tests/service/crm/test_task_service_boundaries.py`:

```python
def test_task_read_service_uses_crm_tasks_as_source_table() -> None:
    source = Path("src/service/crm/tasks/read_service.py").read_text()

    assert "CRMTask" in source
    assert "select(CRMTask)" in source
    assert "select(CRMActivity)" not in source
    assert "cast(Any, CRMActivity)" not in source


def test_task_write_service_uses_crm_tasks_as_source_table() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "CRMTask" in source
    assert "CRMTask(" in source
    assert "CRMActivity(" not in source


def test_task_projection_boundary_is_explicit() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "ActivityFeedProjectionService" in source
    assert Path("src/service/crm/activity_feed_projection_service.py").exists()
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py -q
```

Expected: FAIL because `TaskReadService` still selects `CRMActivity`, `TaskWriteService` still creates `CRMActivity`, and the projection service does not exist.

---

## Task 2: Add Shared Feed Projection Service

**Files:**
- Create: `src/service/crm/activity_feed_projection_service.py`

- [ ] **Step 1: Create the projection service**

Create `src/service/crm/activity_feed_projection_service.py`:

```python
"""Maintain crm_activities feed projections for CRM source rows."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
    CRMActivityType,
    CRMNote,
    CRMTask,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _compact_preview(value: str | None, *, limit: int = 1000) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:limit].rstrip() if compact else None


def note_title_or_preview(note: CRMNote, *, limit: int = 30) -> str | None:
    if note.title and note.title.strip():
        return note.title.strip()
    preview = _compact_preview(note.body, limit=limit)
    return preview


class ActivityFeedProjectionService:
    """Upsert feed-card rows and object links from canonical CRM source tables."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def upsert_task(self, task: CRMTask, *, deal_name: str | None) -> None:
        occurred_at = task.due_at or task.source_created_at or task.created_at or _now_utc()
        title = task.title
        preview = _compact_preview(task.description or task.title)
        metadata = task.task_metadata or {}
        actor_name = metadata.get("actor_name") if isinstance(metadata.get("actor_name"), str) else None
        actor_email = metadata.get("actor_email") if isinstance(metadata.get("actor_email"), str) else None
        await self._upsert_activity(
            activity_id=task.task_id,
            integration_id=task.integration_id,
            activity_type=CRMActivityType.TASK.value,
            title=title,
            summary=task.description,
            preview=preview,
            occurred_at=occurred_at,
            source_type="TASK",
            source_record_id=str(task.task_id),
            actor_name=actor_name,
            actor_email=actor_email,
        )
        await self._sync_primary_deal_link(
            activity_id=task.task_id,
            deal_id=task.deal_id,
            occurred_at=occurred_at,
        )

    async def upsert_note(self, note: CRMNote) -> None:
        occurred_at = note.source_created_at or note.created_at or _now_utc()
        title = note_title_or_preview(note)
        await self._upsert_activity(
            activity_id=note.note_id,
            integration_id=note.integration_id,
            activity_type=CRMActivityType.NOTE.value,
            title=title,
            summary=note.body,
            preview=_compact_preview(note.body),
            occurred_at=occurred_at,
            source_type="NOTE",
            source_record_id=str(note.note_id),
            actor_name=note.author_name,
            actor_email=note.author_email,
        )
        await self._sync_primary_deal_link(
            activity_id=note.note_id,
            deal_id=note.deal_id,
            occurred_at=occurred_at,
        )

    async def soft_delete_activity_projection(self, activity_id: UUID) -> None:
        stmt = (
            update(CRMActivity)
            .where(
                CRMActivity.organisation_id == self.organisation_id,
                CRMActivity.activity_id == activity_id,
            )
            .values(is_deleted=True, updated_at=_now_utc())
        )
        await self.session.execute(stmt)
        await self.session.execute(
            delete(CRMActivityLink).where(
                CRMActivityLink.organisation_id == self.organisation_id,
                CRMActivityLink.activity_id == activity_id,
            )
        )

    async def _upsert_activity(
        self,
        *,
        activity_id: UUID,
        integration_id: UUID | None,
        activity_type: str,
        title: str | None,
        summary: str | None,
        preview: str | None,
        occurred_at: datetime,
        source_type: str,
        source_record_id: str,
        actor_name: str | None,
        actor_email: str | None,
    ) -> None:
        now = _now_utc()
        values = {
            "activity_id": activity_id,
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "activity_type": activity_type,
            "title": title,
            "summary": summary,
            "preview": preview,
            "occurred_at": occurred_at,
            "source_type": source_type,
            "source_record_id": source_record_id,
            "source_integration_id": integration_id,
            "actor_name": actor_name,
            "actor_email": actor_email,
            "display_payload": {},
            "is_deleted": False,
            "updated_at": now,
        }
        stmt = pg_insert(CRMActivity).values(**values).on_conflict_do_update(
            index_elements=["activity_id"],
            set_={
                "integration_id": integration_id,
                "activity_type": activity_type,
                "title": title,
                "summary": summary,
                "preview": preview,
                "occurred_at": occurred_at,
                "source_type": source_type,
                "source_record_id": source_record_id,
                "source_integration_id": integration_id,
                "actor_name": actor_name,
                "actor_email": actor_email,
                "is_deleted": False,
                "updated_at": now,
            },
        )
        await self.session.execute(stmt)

    async def _sync_primary_deal_link(
        self,
        *,
        activity_id: UUID,
        deal_id: UUID | None,
        occurred_at: datetime,
    ) -> None:
        if deal_id is None:
            await self.session.execute(
                delete(CRMActivityLink).where(
                    CRMActivityLink.organisation_id == self.organisation_id,
                    CRMActivityLink.activity_id == activity_id,
                    CRMActivityLink.object_type == CRMActivityObjectType.DEAL.value,
                    CRMActivityLink.link_role == "PRIMARY",
                )
            )
            return

        await self.session.execute(
            delete(CRMActivityLink).where(
                CRMActivityLink.organisation_id == self.organisation_id,
                CRMActivityLink.activity_id == activity_id,
                CRMActivityLink.object_type == CRMActivityObjectType.DEAL.value,
                CRMActivityLink.link_role == "PRIMARY",
                CRMActivityLink.object_id != deal_id,
            )
        )
        if deal_id is None:
            return

        stmt = pg_insert(CRMActivityLink).values(
            organisation_id=self.organisation_id,
            activity_id=activity_id,
            object_type=CRMActivityObjectType.DEAL.value,
            object_id=deal_id,
            link_role="PRIMARY",
            link_source="SYSTEM",
            occurred_at=occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_crm_activity_link_target_role",
            set_={"occurred_at": occurred_at},
        )
        await self.session.execute(stmt)
```

- [ ] **Step 2: Run import check**

Run:

```bash
uv run python -c "from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService, note_title_or_preview; print(ActivityFeedProjectionService.__name__, note_title_or_preview.__name__)"
```

Expected: prints `ActivityFeedProjectionService note_title_or_preview`.

---

## Task 3: Update Task Contracts for `CRMTask`

**Files:**
- Modify: `src/service/crm/tasks/contracts.py`

- [ ] **Step 1: Replace activity-typed contracts**

Update `src/service/crm/tasks/contracts.py` so task write/read contracts use `CRMTask`:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm import CRMTask


@dataclass(frozen=True)
class TaskWriteContext:
    """Caller metadata for internal task writes and future task mutation events."""

    source: str
    actor_user_id: UUID
    created_via: str = "api"
    user_timezone: str | None = None


@dataclass(frozen=True)
class TaskReadRecord:
    """Task source row plus API display fields resolved outside crm_tasks."""

    task: CRMTask
    deal_name: str | None = None


@dataclass(frozen=True)
class TaskWriteResult:
    """Canonical task write result returned to API, agents, bot forms, and workflows."""

    task: CRMTask


class TaskReadServiceError(Exception):
    """HTTP-shaped task read error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TaskWriteServiceError(Exception):
    """HTTP-shaped task write error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
```

- [ ] **Step 2: Run contract import check**

Run:

```bash
uv run python -c "from src.service.crm.tasks.contracts import TaskReadRecord, TaskWriteResult; print(TaskReadRecord.__name__, TaskWriteResult.__name__)"
```

Expected: prints `TaskReadRecord TaskWriteResult`.

---

## Task 4: Repoint Task Read Service to `CRMTask`

**Files:**
- Modify: `src/service/crm/tasks/read_service.py`

- [ ] **Step 1: Replace imports and allowed status constants**

Use these imports and constants:

```python
from collections.abc import Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMTask, CRMUser
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.crm.tasks.contracts import TaskReadRecord, TaskReadServiceError

logger = get_logger("task_read_service")

_ALLOWED_TASK_STATUSES = {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
```

- [ ] **Step 2: Replace assignee key helpers**

```python
def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _task_metadata(task: CRMTask) -> dict[str, Any]:
    return task.task_metadata or {}


def _task_assignee_filter_key(task: Any) -> str | None:
    metadata = getattr(task, "task_metadata", None) or {}
    assignee_kind = metadata.get("assignee_kind")
    assigned_email = _normalize_email(getattr(task, "assigned_to_email", None))
    assigned_name = getattr(task, "assigned_to_name", None)

    if assignee_kind == "external":
        if assigned_email:
            return f"email:{assigned_email}"
        if assigned_name:
            return f"external_name:{str(assigned_name).strip().lower()}"

    public_user_id = metadata.get("assigned_to_public_user_id")
    crm_user_id = getattr(task, "assigned_to_user_id", None)
    if assigned_email:
        return f"email:{assigned_email}"
    if public_user_id:
        return f"public_user:{public_user_id}"
    if crm_user_id:
        return f"crm_user:{crm_user_id}"
    if assigned_name:
        return f"name:{str(assigned_name).strip().lower()}"
    return None
```

- [ ] **Step 3: Update `list_tasks_by_deal()`**

Replace the method so it returns `TaskReadRecord` rows and filters `CRMTask`:

```python
async def list_tasks_by_deal(
    self,
    deal_id: UUID,
    limit: int = 50,
    page: int = 1,
    statuses: Iterable[str] | None = None,
    priorities: Iterable[str] | None = None,
    task_types: Iterable[str] | None = None,
) -> tuple[list[TaskReadRecord], int]:
    if task_types and not ({t for t in task_types if t} & {"TASK", "TODO"}):
        return [], 0
    if page < 1:
        raise TaskReadServiceError("Page must be >= 1", status_code=400)

    c = cast(Any, CRMTask).__table__.c
    stmt = select(CRMTask, CRMDeal.name.label("deal_name")).join(
        CRMDeal,
        and_(
            CRMDeal.organisation_id == self.organisation_id,
            CRMDeal.deal_id == CRMTask.deal_id,
        ),
        isouter=True,
    ).where(
        c.organisation_id == self.organisation_id,
        c.deal_id == deal_id,
        or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
    )

    if statuses:
        stmt = stmt.where(c.status.in_(list(statuses)))
    if priorities:
        stmt = stmt.where(c.priority.in_(list(priorities)))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_count = await self.org_session.scalar(count_stmt) or 0

    offset = (page - 1) * limit
    stmt = stmt.order_by(c.created_at.desc(), c.task_id.desc()).offset(offset).limit(limit)
    result = await self.org_session.execute(stmt)
    rows = result.all()
    return [TaskReadRecord(task=row[0], deal_name=row.deal_name) for row in rows], total_count
```

- [ ] **Step 4: Update `list_tasks()`**

Apply the same pattern to global listing:

```python
stmt = select(CRMTask, CRMDeal.name.label("deal_name")).join(
    CRMDeal,
    and_(
        CRMDeal.organisation_id == self.organisation_id,
        CRMDeal.deal_id == CRMTask.deal_id,
    ),
    isouter=True,
).where(
    c.organisation_id == self.organisation_id,
    or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
)
```

Then map old filters to new columns:

```python
if statuses:
    stmt = stmt.where(c.status.in_(list(statuses)))
elif not include_completed:
    stmt = stmt.where(c.status.notin_(["COMPLETED", "CANCELLED"]))

if priorities:
    stmt = stmt.where(c.priority.in_(list(priorities)))

if source:
    stmt = stmt.where(c.task_metadata["source"].as_string().in_(list(source)))

if owner_user_id is not None:
    owner_value = str(owner_user_id)
    assignee_keys = await self.resolve_current_user_task_assignees(owner_user_id)
    stmt = stmt.where(
        or_(
            c.task_metadata["assigned_to_public_user_id"].as_string() == owner_value,
            *[self._assigned_to_key_condition(key) for key in assignee_keys],
        )
    )

if created_by_user_id is not None:
    stmt = stmt.where(c.created_by_user_id == created_by_user_id)

if due_from is not None:
    stmt = stmt.where(c.due_at >= due_from)
if due_to is not None:
    stmt = stmt.where(c.due_at <= due_to)

if has_due_date is True:
    stmt = stmt.where(or_(c.due_at.isnot(None), c.due_date.isnot(None)))
elif has_due_date is False:
    stmt = stmt.where(and_(c.due_at.is_(None), c.due_date.is_(None)))

now = datetime.now(UTC)
if is_overdue is True:
    stmt = stmt.where(c.due_at < now, c.status != "COMPLETED")
elif is_overdue is False:
    stmt = stmt.where(or_(c.due_at.is_(None), c.due_at >= now))
```

Use this sort map:

```python
sort_col_map = {
    "due_date": c.due_at,
    "created_at": c.created_at,
    "updated_at": c.updated_at,
    "priority": c.priority,
    "status": c.status,
    "subject": c.title,
}
```

Return:

```python
rows = result.all()
return [TaskReadRecord(task=row[0], deal_name=row.deal_name) for row in rows], total_count
```

- [ ] **Step 5: Update assignee query helpers**

Replace `CRMActivity` column references with `CRMTask` columns:

```python
def _assigned_to_key_condition(self, assigned_to_key: str):
    c = cast(Any, CRMTask).__table__.c
    prefix, _, raw_value = assigned_to_key.partition(":")
    value = raw_value.strip()
    lowered = value.lower()

    if not prefix or not value:
        return c.task_id.is_(None)

    if prefix == "email":
        return func.lower(c.assigned_to_email) == lowered
    if prefix == "public_user":
        return c.task_metadata["assigned_to_public_user_id"].as_string() == value
    if prefix == "crm_user":
        try:
            return c.assigned_to_user_id == UUID(value)
        except ValueError:
            return c.task_id.is_(None)
    if prefix in {"name", "external_name"}:
        return func.lower(c.assigned_to_name) == lowered
    return c.task_id.is_(None)
```

Port the existing current-user helper methods to the same `CRMTask` fields before using `scope="assigned_to_me"`:

- `_current_user_assignee_condition()` must check `task_metadata["assigned_to_public_user_id"]`, `assigned_to_email`, and resolved CRM user IDs.
- `resolve_current_user_task_assignees()` must keep returning filter keys such as `public_user:<id>`, `email:<email>`, and `crm_user:<id>`.
- `_resolve_current_user_crm_user_ids()` remains the bridge from public `users.user_id` / email to `crm_users.user_id`.
- Do not leave any helper reading `CRMActivity.properties`, `owner_id`, `external_assignee_email`, or `external_assignee_name`.

- [ ] **Step 6: Update `get_task_by_id()`**

```python
async def get_task_by_id(self, task_id: UUID) -> TaskReadRecord | None:
    try:
        c = cast(Any, CRMTask).__table__.c
        stmt = select(CRMTask, CRMDeal.name.label("deal_name")).join(
            CRMDeal,
            and_(
                CRMDeal.organisation_id == self.organisation_id,
                CRMDeal.deal_id == CRMTask.deal_id,
            ),
            isouter=True,
        ).where(
            c.task_id == task_id,
            c.organisation_id == self.organisation_id,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )
        result = await self.org_session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        return TaskReadRecord(task=row[0], deal_name=row.deal_name)
    except Exception as exc:
        logger.error("Failed to get task %s: %s", task_id, exc, exc_info=True)
        raise TaskReadServiceError("Failed to retrieve task") from exc
```

- [ ] **Step 7: Run the task boundary test again**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py -q
```

Expected: still FAIL until write service and projection are completed, but read-service assertions should now pass.

---

## Task 5: Repoint Task Write and Reminder Services to `CRMTask`

**Files:**
- Modify: `src/service/crm/tasks/write_service.py`
- Modify: `src/service/crm/tasks/reminder_service.py`
- Modify: `src/service/crm/tasks/notification_service.py`

- [ ] **Step 1: Update write service imports**

Replace activity imports with:

```python
from src.domain.models.organisation_tables.sync.crm import CRMActivityPriority, CRMTask
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.tasks.contracts import TaskReadRecord
from src.service.crm.tasks.read_service import TaskReadService
```

Keep `CRMActivityStatus` / `CRMActivityType` only if used for enum value constants. Do not construct `CRMActivity`.

- [ ] **Step 2: Create `CRMTask` rows**

In `TaskWriteService.create_task()`, keep `_resolve_owner()` as the public-user-to-CRM-user resolver. Preserve the requested public assignee in `task_metadata["assigned_to_public_user_id"]`, because `assigned_to_user_id`, `owner_user_id`, and `completed_by_user_id` are CRM-user IDs.

Add a title helper so body-only tasks do not become low-signal `"Untitled task"` rows:

```python
def _task_title_from_payload(payload: dict[str, Any]) -> str:
    subject = payload.get("subject")
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    body = payload.get("body")
    if isinstance(body, str) and body.strip():
        return " ".join(body.split())[:80].rstrip()
    return "Untitled task"
```

Then write `CRMTask`:

```python
task_type = payload.get("task_type") or "TASK"
actor_crm_user_id, _, _ = await self._resolve_owner(self.actor_user_id)
task = CRMTask(
    task_id=uuid4(),
    organisation_id=self.organisation_id,
    integration_id=None,
    title=_task_title_from_payload(payload),
    description=payload.get("body"),
    status=status,
    priority=priority,
    due_date=due_date.date() if due_date else None,
    due_at=None if is_all_day else due_date,
    is_all_day=is_all_day,
    reminder_at=payload.get("reminder_date"),
    completed_at=completed_date,
    deal_id=deal.deal_id if deal else None,
    assigned_to_user_id=owner_id,
    assigned_to_name=owner_name or external_assignee_name,
    assigned_to_email=owner_email or external_assignee_email,
    created_by_user_id=self.actor_user_id,
    owner_user_id=owner_id,
    owner_email=owner_email,
    completed_by_user_id=actor_crm_user_id if status == "COMPLETED" else None,
    source_created_at=now,
    source_modified_at=now,
    task_metadata={
        "source": "MANUAL",
        "task_type": task_type,
        "is_user_created": True,
        "created_via": "api",
        "created_by_public_user_id": str(self.actor_user_id),
        "assignee_kind": "external" if external_assignee_email else "internal_user",
        **assignment_properties,
        **due_properties,
    },
)
self.session.add(task)
await self.session.flush()
await self.session.refresh(task)
await ActivityFeedProjectionService(self.session, self.organisation_id).upsert_task(
    task,
    deal_name=deal.name if deal else None,
)
```

- [ ] **Step 3: Update mutable fields on `CRMTask`**

Map API payload fields to source-table fields:

```python
if "subject" in payload:
    task.title = _task_title_from_payload(payload)
if "body" in payload:
    task.description = payload.get("body")
    if "subject" not in payload:
        task.title = _task_title_from_payload(payload)
if "priority" in payload:
    task.priority = priority
if "due_date" in payload:
    task.due_date = due_date.date() if due_date else None
    task.due_at = None if is_all_day else due_date
    task.is_all_day = is_all_day
if "reminder_date" in payload:
    task.reminder_at = payload.get("reminder_date")
if "owner_user_id" in payload:
    task.assigned_to_user_id = owner_id
    task.assigned_to_name = owner_name
    task.assigned_to_email = owner_email
    task.owner_user_id = owner_id
    task.owner_email = owner_email
if "external_assignee_email" in payload or "external_assignee_name" in payload:
    task.assigned_to_user_id = None
    task.assigned_to_email = payload.get("external_assignee_email")
    task.assigned_to_name = payload.get("external_assignee_name")
if "task_type" in payload:
    metadata = task.task_metadata or {}
    metadata["task_type"] = payload.get("task_type") or "TASK"
    task.task_metadata = metadata
```

After updates:

```python
task.source_modified_at = now
metadata = task.task_metadata or {}
metadata["updated_via"] = "api"
task.task_metadata = metadata
await self.session.flush()
await self.session.refresh(task)
deal = await self._load_deal(task.deal_id) if task.deal_id else None
await ActivityFeedProjectionService(self.session, self.organisation_id).upsert_task(
    task,
    deal_name=deal.name if deal else None,
)
```

- [ ] **Step 4: Load and delete `CRMTask`**

```python
async def _load_user_task(self, task_id: UUID) -> CRMTask:
    task = await self.session.get(CRMTask, task_id)
    if not task or task.organisation_id != self.organisation_id:
        raise TaskWriteServiceError("Task not found", 404)
    if task.is_deleted:
        raise TaskWriteServiceError("Task not found", 404)
    if task.integration_id is not None:
        raise TaskWriteServiceError("External CRM tasks are read-only", 409)
    return task
```

On delete:

```python
task.is_deleted = True
task.source_modified_at = now
metadata = task.task_metadata or {}
metadata["deleted_via"] = "api"
task.task_metadata = metadata
await self.session.flush()
await self.session.refresh(task)
await ActivityFeedProjectionService(self.session, self.organisation_id).soft_delete_activity_projection(
    task.task_id
)
```

- [ ] **Step 5: Add a read-back helper for API and non-API callers**

Add this method to `TaskWriteService` so create/update routes can return a decorated record without duplicating deal-name lookup logic:

```python
async def read_task_record(self, task_id: UUID) -> TaskReadRecord:
    read_service = TaskReadService(self.session, self.organisation_id)
    record = await read_service.get_task_by_id(task_id)
    if record is None:
        raise TaskWriteServiceError("Task not found", 404)
    return record
```

Also audit direct callers of `TaskWriteService.create_task()` and `update_task()` so agent tools and bot form handlers either consume `CRMTask` fields (`task_id`, `title`, `description`, `due_at`, `reminder_at`) or call `read_task_record()` before rendering user-facing text.

- [ ] **Step 6: Update reminder service**

In `src/service/crm/tasks/reminder_service.py`, import `CRMTask` and use `task_id`:

```python
from src.domain.models.organisation_tables.sync.crm import CRMTask

async def create_task_reminder(self, task: CRMTask, cfg: dict[str, Any]) -> UserReminder:
    reminder_message = f"Reminder for task: {task.title}"
    if task.description:
        reminder_message = f"{reminder_message}\n\n{task.description}"
    ...
    activity_id=task.task_id,
    ...
    task.reminder_at = trigger_time
```

Update `update_task_reminder()` and `cancel_task_reminders()` to fetch `CRMTask`:

```python
task = await self.session.get(CRMTask, reminder.activity_id)
if task:
    task.reminder_at = reminder.next_trigger_at
```

- [ ] **Step 7: Update notification service**

In `src/service/crm/tasks/notification_service.py`, import `CRMTask` and replace all activity-shaped field reads, not only local variable names. Search the file for `subject`, `body`, `due_date`, `reminder_date`, and `activity_id`; each task reference must map to the new source field:

```python
task_id = task.task_id
task_title = task.title
task_body = task.description
task_due_date = task.due_at or task.due_date
task_reminder_at = task.reminder_at
```

- [ ] **Step 8: Run task service boundary tests**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py -q
```

Expected: PASS.

---

## Task 6: Update Tasks Router Mapping

**Files:**
- Modify: `src/api/routers/crm/tasks_handler.py`
- Modify: `src/api/schemas/crm_task_models.py`
- Modify: `tests/api/routers/crm/conftest.py`
- Modify: `tests/api/routers/crm/test_tasks_handler.py`

- [ ] **Step 1: Update task options to the DB-backed status set**

In `src/api/routers/crm/tasks_handler.py`, remove `WAITING` and `DEFERRED` from `_TASK_STATUS_OPTIONS`:

```python
_TASK_STATUS_OPTIONS = [
    TaskOption(value=CRMActivityStatus.NOT_STARTED.value, label="To Do"),
    TaskOption(value=CRMActivityStatus.IN_PROGRESS.value, label="In Progress"),
    TaskOption(value=CRMActivityStatus.COMPLETED.value, label="Done"),
    TaskOption(value=CRMActivityStatus.CANCELLED.value, label="Cancelled"),
]
```

- [ ] **Step 2: Reject non-DB task statuses at the request boundary**

In `src/api/schemas/crm_task_models.py`, keep response enum compatibility only where needed, but add validators to create/update request models so `WAITING` and `DEFERRED` fail with a 422 before service code reaches the DB constraint:

```python
_ALLOWED_TASK_STATUS_VALUES = {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}


@field_validator("status")
@classmethod
def validate_db_backed_status(cls, value: CRMActivityStatus | None) -> CRMActivityStatus | None:
    if value is not None and value.value not in _ALLOWED_TASK_STATUS_VALUES:
        raise ValueError("status must be NOT_STARTED, IN_PROGRESS, COMPLETED, or CANCELLED")
    return value
```

Add a focused API/schema test that posting or patching `WAITING` returns 422 and does not call `TaskWriteService`.

- [ ] **Step 3: Replace model-validate based mapping**

Add mapping helpers:

```python
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.service.crm.tasks.contracts import TaskReadRecord


def _task_source(task: CRMTask) -> str | None:
    metadata = task.task_metadata or {}
    source = metadata.get("source")
    if isinstance(source, str) and source:
        return source
    return CRMActivitySource.CRM_SYNC.value if task.integration_id else CRMActivitySource.MANUAL.value


def _task_due_datetime(task: CRMTask) -> datetime | None:
    if task.due_at:
        return task.due_at
    if task.due_date:
        return datetime.combine(task.due_date, datetime.min.time(), tzinfo=UTC)
    return None
```

Then update `_build_task_assignee()` to read `CRMTask` fields:

```python
def _build_task_assignee(task: CRMTask) -> TaskAssignee | None:
    metadata = task.task_metadata or {}
    assignee_kind = metadata.get("assignee_kind")
    email = _normalize_email(task.assigned_to_email)
    name = task.assigned_to_name
    public_user_id = _parse_uuid(metadata.get("assigned_to_public_user_id"))
    crm_user_id = task.assigned_to_user_id

    if assignee_kind == "external":
        filter_key = f"email:{email}" if email else f"external_name:{str(name).strip().lower()}"
        return TaskAssignee(kind="external", filter_key=filter_key, name=name, email=email)

    if email:
        filter_key = f"email:{email}"
    elif public_user_id:
        filter_key = f"public_user:{public_user_id}"
    elif crm_user_id:
        filter_key = f"crm_user:{crm_user_id}"
    elif name:
        filter_key = f"name:{str(name).strip().lower()}"
    else:
        return None

    return TaskAssignee(
        kind="internal_user" if public_user_id else "crm_owner" if crm_user_id else "unknown",
        filter_key=filter_key,
        name=name,
        email=email,
        public_user_id=public_user_id,
        crm_user_id=crm_user_id,
    )
```

- [ ] **Step 4: Build `TaskListItem` from `TaskReadRecord`**

```python
async def _enrich_task_list_item(record: TaskReadRecord, session: AsyncSession) -> TaskListItem:
    task = record.task
    due_date = _task_due_datetime(task)
    has_reminder = await _has_active_reminder(session, task.task_id)
    metadata = task.task_metadata or {}
    is_external = metadata.get("assignee_kind") == "external"

    return TaskListItem(
        task_id=task.task_id,
        subject=task.title,
        body=task.description,
        status=task.status,
        task_type=metadata.get("task_type") if metadata.get("task_type") in {"TASK", "TODO"} else "TASK",
        priority=task.priority,
        due_date=due_date,
        completed_date=task.completed_at,
        reminder_date=task.reminder_at,
        due_local_date=metadata.get("due_local_date") if isinstance(metadata.get("due_local_date"), str) else None,
        due_timezone=metadata.get("due_timezone") if isinstance(metadata.get("due_timezone"), str) else None,
        is_all_day=task.is_all_day,
        owner_id=task.assigned_to_user_id,
        owner_name=None if is_external else task.assigned_to_name,
        owner_email=None if is_external else task.assigned_to_email,
        assigned_to=_build_task_assignee(task),
        external_assignee_email=task.assigned_to_email if is_external else None,
        external_assignee_name=task.assigned_to_name if is_external else None,
        deal_id=task.deal_id,
        deal_name=record.deal_name,
        source=_task_source(task),
        created_by_user_id=task.created_by_user_id,
        is_overdue=_compute_is_overdue(due_date, task.status),
        has_reminder=has_reminder,
        is_read_only=task.integration_id is not None,
        created_at=task.created_at,
        updated_at=task.updated_at,
        synced_at=task.source_modified_at or task.updated_at,
    )
```

- [ ] **Step 5: Build `TaskDetail` from `TaskReadRecord`**

Use the list mapper as the base and add detail fields:

```python
async def _enrich_task_detail(record: TaskReadRecord, session: AsyncSession) -> TaskDetail:
    item = await _enrich_task_list_item(record, session)
    task = record.task
    reminder_dict = await _get_reminder_dict(session, task.task_id)
    metadata = task.task_metadata or {}

    return TaskDetail(
        **item.model_dump(),
        source_created_at=task.source_created_at,
        source_modified_at=task.source_modified_at,
        is_deleted_remote=bool(task.is_deleted),
        reminder=reminder_dict,
        properties=metadata,
        metrics=None,
        integration_id=task.integration_id,
        organisation_id=task.organisation_id,
        last_seen_at=task.source_modified_at,
    )
```

- [ ] **Step 6: Update router call sites**

Change list/detail loops from task rows to read records:

```python
records, total_count = await service.list_tasks(...)
items = [await _enrich_task_list_item(record, service.session) for record in records]
```

For detail:

```python
record = await service.get_task_by_id(task_id)
if not record:
    ...
detail = await _enrich_task_detail(record, service.session)
```

For writes:

```python
task = await service.create_task(payload, deal_id=deal_id)
await service.session.commit()
record = await service.read_task_record(task.task_id)
detail = await _enrich_task_detail(record, service.session)
```

Do not keep an alternate path here; `read_task_record()` is required so create/update responses and non-API callers use the same decorated source-row mapping.

- [ ] **Step 7: Update test fakes**

In `tests/api/routers/crm/conftest.py`, update `FakeTask` to expose `CRMTask`-like fields:

```python
self.task_id = activity_id
self.activity_id = activity_id
self.title = subject
self.subject = subject
self.description = body
self.body = body
self.due_at = due_date
self.due_date = due_date
self.reminder_at = reminder_date
self.reminder_date = reminder_date
self.completed_at = completed_date
self.completed_date = completed_date
self.assigned_to_user_id = owner_id
self.assigned_to_name = owner_name
self.assigned_to_email = owner_email
self.task_metadata = properties or {}
if external_assignee_email or external_assignee_name:
    self.task_metadata["assignee_kind"] = "external"
    self.assigned_to_email = external_assignee_email
    self.assigned_to_name = external_assignee_name
```

Return `TaskReadRecord(task=task, deal_name=task.related_name)` from fake service methods.

- [ ] **Step 8: Add source-table mapping tests**

Add to `tests/api/routers/crm/test_tasks_handler.py`:

```python
def test_task_response_maps_crm_task_source_fields(client: TestClient, tasks_dependency_overrides):
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    task = create_task(
        deal_id,
        org_id,
        subject="Source table title",
        body="Source table description",
        due_date=datetime.now(UTC) + timedelta(days=1),
        properties={"source": "MANUAL", "assigned_to_public_user_id": str(tasks_dependency_overrides["user_ids"]["user_id"])},
    )
    service.add_task(task)

    resp = client.get(f"/users/crm/tasks/{task.task_id}")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["task_id"] == str(task.task_id)
    assert data["subject"] == "Source table title"
    assert data["body"] == "Source table description"
    assert data["source"] == "MANUAL"
```

- [ ] **Step 9: Run focused API tests**

Run:

```bash
uv run pytest tests/api/routers/crm/test_tasks_handler.py tests/service/crm/test_task_service_boundaries.py -q
```

Expected: PASS.

---

## Task 7: Update Task API Documentation

**Files:**
- Modify: `docs/API Documentation/deals/deal-tasks-api.md`

- [ ] **Step 1: Update storage and status contract**

Add this note under Overview:

```markdown
### Storage Contract

Tasks are read from and written to `crm_tasks`. The unified activity feed uses
`crm_activities` only as a feed-card projection and `crm_activity_links` for
deal/contact/company timeline visibility.
```

Update the status enum section to remove `WAITING` and `DEFERRED` unless the DB constraint has been expanded in a separate schema task.

- [ ] **Step 2: Update field descriptions**

Update TypeScript comments:

```ts
subject: string | null;           // crm_tasks.title
body: string | null;              // crm_tasks.description
due_date: string | null;          // crm_tasks.due_at when present, otherwise crm_tasks.due_date at midnight UTC
completed_date: string | null;    // crm_tasks.completed_at
reminder_date: string | null;     // crm_tasks.reminder_at
```

- [ ] **Step 3: Run doc/source checks**

Run:

```bash
rg -n "WAITING|DEFERRED|crm_activities.*source|activity_type" "docs/API Documentation/deals/deal-tasks-api.md"
```

Expected: no stale claim that task API rows are sourced from `crm_activities`; no `WAITING`/`DEFERRED` status entries unless the schema constraint was changed.

---

## Task 8: Notes Phase Boundary Tests

**Files:**
- Modify: `tests/service/crm/test_notes_service_boundaries.py`
- Modify: `tests/api/routers/crm/test_deal_notes_handler.py`

- [ ] **Step 1: Add notes source-table boundary tests**

Add to `tests/service/crm/test_notes_service_boundaries.py`:

```python
def test_notes_read_service_uses_crm_notes_as_source_table() -> None:
    source = Path("src/service/crm/notes/read_service.py").read_text()

    assert "CRMNote" in source
    assert "select(CRMNote)" in source
    assert "select(CRMActivity)" not in source
    assert "cast(Any, CRMActivity)" not in source


def test_notes_write_service_uses_crm_notes_as_source_table() -> None:
    source = Path("src/service/crm/notes/write_service.py").read_text()

    assert "CRMNote" in source
    assert "CRMNote(" in source
    assert "CRMActivity(" not in source
```

- [ ] **Step 2: Add title fallback API test**

Add to `tests/api/routers/crm/test_deal_notes_handler.py`:

```python
def test_list_notes_subject_falls_back_to_body_preview(client: TestClient, notes_dependency_overrides):
    service = notes_dependency_overrides["service"]
    org_id = notes_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    service.add_deal(deal_id)
    service.add_note(create_note(deal_id, org_id, content="This body becomes the visible note title", subject=None))

    resp = client.get(f"/users/crm/deals/{deal_id}/notes")

    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["subject"] == "This body becomes the visible"
```

- [ ] **Step 3: Run failing notes tests**

Run:

```bash
uv run pytest tests/service/crm/test_notes_service_boundaries.py tests/api/routers/crm/test_deal_notes_handler.py -q
```

Expected: FAIL until notes services and mapper use `CRMNote`.

---

## Task 9: Repoint Notes API to `CRMNote`

**Files:**
- Modify: `src/service/crm/notes/contracts.py`
- Modify: `src/service/crm/notes/read_service.py`
- Modify: `src/service/crm/notes/write_service.py`
- Modify: `src/api/routers/crm/deal_notes_handler.py`
- Modify: `tests/api/routers/crm/conftest.py`

- [ ] **Step 1: Update notes contracts**

Use `CRMNote` and a read record:

```python
from src.domain.models.organisation_tables.sync.crm import CRMNote


@dataclass(frozen=True)
class NoteReadRecord:
    """Note source row plus API display fields resolved outside crm_notes."""

    note: CRMNote


@dataclass(frozen=True)
class CreateUserNoteResult:
    """User note creation result with exact-duplicate idempotency semantics."""

    note: CRMNote
    created_new: bool
```

- [ ] **Step 2: Update read service**

Replace `CRMActivity` query with:

```python
c = cast(Any, CRMNote).__table__.c
sort_ts = func.coalesce(c.source_created_at, c.created_at)
base_where = and_(
    c.organisation_id == self.organisation_id,
    c.deal_id == deal_id,
    or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
)
count_stmt = select(func.count()).select_from(CRMNote).where(base_where)
stmt = select(CRMNote).where(base_where)
```

Return:

```python
rows: list[CRMNote] = list(result.scalars().all())
items = rows[:limit]
next_cursor = encode_cursor(last.source_created_at or last.created_at, last.note_id)
return [NoteReadRecord(note=note) for note in items], next_cursor, total_count, participants
```

- [ ] **Step 3: Update write service**

Create `CRMNote`:

```python
note = CRMNote(
    note_id=uuid4(),
    organisation_id=self.organisation_id,
    integration_id=None,
    deal_id=deal_id,
    title=subject,
    body=content,
    body_format="plain_text",
    author_user_id=None,
    author_name=context.actor_name,
    author_email=context.actor_email,
    visibility="TEAM",
    source_created_at=now,
    source_modified_at=now,
    note_metadata={
        "source": "user",
        "is_user_created": True,
        "created_via": context.created_via,
        "created_by_public_user_id": str(context.actor_user_id),
    },
)
self.org_session.add(note)
await self.org_session.flush()
await self.org_session.refresh(note)
await ActivityFeedProjectionService(self.org_session, self.organisation_id).upsert_note(note)
```

Update `_load_user_note()` to use `self.org_session.get(CRMNote, note_id)` and reject `integration_id is not None`.

Update duplicate lookup:

```python
stmt = select(CRMNote).where(
    c.organisation_id == self.organisation_id,
    c.deal_id == deal_id,
    c.integration_id.is_(None),
    c.body == content,
    or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
)
stmt = stmt.where(c.title.is_(None) if subject is None else c.title == subject)
```

- [ ] **Step 4: Update router mapper**

In `src/api/routers/crm/deal_notes_handler.py`:

```python
from src.domain.models.organisation_tables.sync.crm import CRMNote
from src.service.crm.activity_feed_projection_service import note_title_or_preview
from src.service.crm.notes.contracts import NoteReadRecord


def _map_note_to_schema(record: NoteReadRecord | CRMNote, source: str) -> DealNoteItem:
    note = record.note if isinstance(record, NoteReadRecord) else record
    assert note.deal_id is not None, "Note must have deal_id when mapping to DealNoteItem"
    is_user_created = note.integration_id is None

    return DealNoteItem(
        note_id=note.note_id,
        deal_id=note.deal_id,
        content=note.body,
        subject=note_title_or_preview(note),
        created_at=note.source_created_at or note.created_at,
        updated_at=note.source_modified_at or note.updated_at,
        source=source,
        is_user_created=is_user_created,
        author=NoteAuthor(
            id=str(note.author_user_id) if note.author_user_id else None,
            name=note.author_name,
            email=note.author_email,
        ),
        metadata=NoteMetadata(
            synced_at=note.source_modified_at if not is_user_created else None,
        ),
)
```

`author_user_id` is a `crm_users.user_id` field. Leave it null unless this service resolves the public actor through `InternalCRMUserService`; always store the public actor in `note_metadata["created_by_public_user_id"]` so API audit context is not lost.

- [ ] **Step 5: Update fake notes**

In `tests/api/routers/crm/conftest.py`, `FakeNote` should expose:

```python
self.note_id = activity_id
self.title = subject
self.body = body
self.author_name = owner_name
self.author_email = owner_email
self.author_user_id = None
self.source_modified_at = synced_at
self.note_metadata = {}
```

Return `NoteReadRecord(note=note)` from fake note listing.

- [ ] **Step 6: Run focused notes tests**

Run:

```bash
uv run pytest tests/service/crm/test_notes_service_boundaries.py tests/api/routers/crm/test_deal_notes_handler.py -q
```

Expected: PASS.

---

## Task 10: Update Notes API Documentation

**Files:**
- Modify: `docs/API Documentation/deals/deal-notes-api.md`

- [ ] **Step 1: Update storage contract**

Add under Overview:

```markdown
### Storage Contract

Deal notes are read from and written to `crm_notes`. The unified activity feed
uses `crm_activities` only as a feed-card projection and `crm_activity_links`
for timeline visibility.
```

- [ ] **Step 2: Document fallback subject behavior**

Add under Data Shapes:

```markdown
`subject` is returned as `crm_notes.title` when present. If a note has no title,
the API returns the first 30 characters of the normalized note body so list UIs
still have a compact display label.
```

- [ ] **Step 3: Run doc source check**

Run:

```bash
rg -n "crm_activities|activity_type|first 30|crm_notes" "docs/API Documentation/deals/deal-notes-api.md"
```

Expected: only intentional references remain: `crm_notes` as source table and `crm_activities` as projection table.

---

## Task 11: Focused Verification

**Files:**
- No edits.

- [ ] **Step 1: Re-run the preflight release-boundary checks**

Run:

```bash
rg -n "CRMActivity|activity_type|subject|body|due_date|reminder_date|completed_date|last_seen_at|properties|metrics" src/service src/agents src/workers tests
rg -n "TaskWriteService|create_task\\(" src/api src/agents src/service tests
```

Expected: any remaining activity-shaped task/note references are either outside this API-level scope and documented as release-gated follow-up work, or intentionally covered by compatibility adapters/tests.

- [ ] **Step 2: Run focused task and note API tests**

Run:

```bash
uv run pytest tests/api/routers/crm/test_tasks_handler.py tests/api/routers/crm/test_deal_notes_handler.py tests/service/crm/test_task_service_boundaries.py tests/service/crm/test_notes_service_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 3: Run agent/bot caller compatibility tests touched by `TaskWriteService`**

Run the focused tests for direct task creation callers that consume the write-service result:

```bash
uv run pytest tests/agents/tools/tasks/test_create_task.py tests/service/bots/form_handlers -q
```

Expected: PASS or a narrower equivalent if the bot form handler tests live under a different path on the implementation branch.

- [ ] **Step 4: Run schema/migration safety tests already present on this branch**

Run:

```bash
uv run pytest tests/test_ai_native_activity_schema.py tests/migrations/test_ai_native_activity_migration_contract.py -q
```

Expected: PASS.

- [ ] **Step 5: Check the org migration graph**

Run:

```bash
uv run alembic -c migrations/org_migrations/alembic.ini heads
```

Expected: exactly one org-migration head. If more than one head appears, stop and fix the migration graph before calling the API migration production-ready. Also confirm migration `066` is not part of the production rollout unless the ORM/provider-writer compatibility work has been completed.

- [ ] **Step 6: Run whitespace diff check**

Run:

```bash
git diff --check
```

Expected: no output.

---

## Self-Review

Spec coverage:

- Task API first: Tasks 1-7 migrate task read/write/router/docs before notes.
- Notes after tasks: Tasks 8-10 migrate notes and include the requested title fallback.
- Point APIs away from activity table: boundary tests require task and note services to select source tables, not `CRMActivity`.
- Preserve feed behavior: projection service keeps `crm_activities` and `crm_activity_links` updated as derived rows.
- Organization isolation: all read/write examples filter by `organisation_id`; deal loads still verify the org boundary.

Placeholder scan:

- No prohibited placeholder-style wording remains; the `TODO` token appears only as the existing CRM task-type value.
- Every code-changing task includes concrete code snippets and exact verification commands.

Type consistency:

- Task source row type is `CRMTask`; task read wrapper is `TaskReadRecord`.
- Note source row type is `CRMNote`; note read wrapper is `NoteReadRecord`.
- Public route IDs remain `task_id` and `note_id`.
- Feed projection uses `CRMTask.task_id` / `CRMNote.note_id` as `CRMActivity.activity_id` and `source_record_id`.
