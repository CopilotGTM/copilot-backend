# CRM Tasks Feature Implementation Plan

> Historical note: this plan predates the task/note source-table migration.
> Current runtime code should use `crm_tasks` / `CRMTask` for task source rows
> and treat `crm_activities` / `CRMActivity` as the feed projection only.

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing CRM tasks API to support global task management with optional deal association, user assignment, reminder integration, and source tracking.

**Architecture:** Evolve the existing `tasks_handler.py` + `tasks_access_service.py` + `tasks_write_service.py` stack. Add new `GET /tasks` and `POST /tasks` endpoints (deal_id optional). Extend `CRMActivity` model with `source` and `assigned_to_user_id` columns. Link `UserReminder` to tasks via new `task_id` FK. One DB migration for all schema changes.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy/SQLModel, Alembic, PostgreSQL, Redis (reminder scheduling), Pydantic v2

**Worktree:** `D:\CopilotGTM\copilot-backend-deal-tasks` (branch: `feature/deal-tasks`)

**Design doc:** `docs/plans/2026-03-11-crm-tasks-feature-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/domain/models/organisation_tables/sync/crm/activities.py` | Add `source`, `assigned_to_user_id` fields + `CRMActivitySource` enum |
| Modify | `src/domain/models/organisation_tables/sync/crm/__init__.py` | Export new enum |
| Modify | `src/domain/models/organisation_tables/reminder_models.py` | Add `task_id` field to `UserReminder` |
| Create | `migrations/org_migrations/versions/<hash>_035_add_task_source_assignment_reminder_link.py` | DB migration |
| Modify | `src/api/schemas/crm_task_models.py` | New request/response schemas with `source`, `assigned_to`, `deal_id`, `reminder` |
| Modify | `src/service/crm/access/tasks_access_service.py` | Add `list_tasks()` (global, filterable) |
| Modify | `src/service/crm/internal/tasks_write_service.py` | Make `deal_id` optional, add reminder lifecycle, source tracking |
| Modify | `src/api/routers/crm/tasks_handler.py` | Add `GET /tasks`, `POST /tasks` endpoints, update existing endpoints |
| Modify | `src/api/fastapi_app.py` | Verify router mounting (may not need changes) |
| Create | `tests/api/routers/crm/test_tasks_global_handler.py` | Tests for global task endpoints |
| Modify | `tests/api/routers/crm/test_tasks_handler.py` | Update existing tests for new fields |

---

## Chunk 1: Database Layer (Model + Migration)

### Task 1: Add `CRMActivitySource` enum and new fields to CRMActivity model

**Files:**
- Modify: `src/domain/models/organisation_tables/sync/crm/activities.py:61-71` (after CRMActivityPriority enum)
- Modify: `src/domain/models/organisation_tables/sync/crm/activities.py:265-284` (after owner fields)
- Modify: `src/domain/models/organisation_tables/sync/crm/__init__.py`

- [ ] **Step 1: Add CRMActivitySource enum**

In `src/domain/models/organisation_tables/sync/crm/activities.py`, after `CRMActivityObjectType` (line 71):

```python
class CRMActivitySource(str, Enum):
    """Origin of the activity record."""

    MANUAL = "MANUAL"
    CRM_SYNC = "CRM_SYNC"
    AI_MEETING = "AI_MEETING"
    AI_INSIGHT = "AI_INSIGHT"
```

- [ ] **Step 2: Add `source` and `assigned_to_user_id` fields to CRMActivity**

In `src/domain/models/organisation_tables/sync/crm/activities.py`, after the `owner_id` field block (around line 284):

```python
    # ── Source tracking ──────────────────────────────────────────────────
    source: str | None = Field(
        sa_column=Column(
            String(50),
            nullable=True,
            index=False,
            comment="Origin of the task: MANUAL, CRM_SYNC, AI_MEETING, AI_INSIGHT",
        ),
        default=None,
        description="How this task was created.",
    )

    # ── Internal assignment ──────────────────────────────────────────────
    assigned_to_user_id: UUID | None = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            nullable=True,
            comment="Internal user this task is assigned to (public.users.id)",
        ),
        default=None,
        description="CopilotGTM user assigned to this task.",
    )
```

Also add these indexes to the `__table_args__` tuple (around line 111-149):

```python
    Index("ix_crm_activities_source", "organisation_id", "source"),
    Index("ix_crm_activities_assigned_to", "organisation_id", "assigned_to_user_id"),
```

- [ ] **Step 3: Export new enum from `__init__.py`**

In `src/domain/models/organisation_tables/sync/crm/__init__.py`, add `CRMActivitySource` to the imports and `__all__`.

- [ ] **Step 4: Verify model loads without errors**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run python -c "from src.domain.models.organisation_tables.sync.crm.activities import CRMActivity, CRMActivitySource; print('OK', CRMActivitySource.MANUAL)"`

Expected: `OK MANUAL`

- [ ] **Step 5: Commit**

```bash
git add src/domain/models/organisation_tables/sync/crm/activities.py src/domain/models/organisation_tables/sync/crm/__init__.py
git commit -m "feat(tasks): add source enum and assigned_to_user_id to CRMActivity model"
```

---

### Task 2: Add `task_id` to UserReminder model

**Files:**
- Modify: `src/domain/models/organisation_tables/reminder_models.py:129-138` (after delivery fields)

- [ ] **Step 1: Add `task_id` field to UserReminder**

In `src/domain/models/organisation_tables/reminder_models.py`, after the `delivery_context` field (around line 138):

```python
    # ── Task linkage ─────────────────────────────────────────────────────
    task_id: UUID | None = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            nullable=True,
            index=True,
            comment="Linked CRM activity/task ID (crm_activities.activity_id)",
        ),
        default=None,
        description="Task this reminder is linked to.",
    )
```

- [ ] **Step 2: Verify model loads**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run python -c "from src.domain.models.organisation_tables.reminder_models import UserReminder; print('OK', [c.name for c in UserReminder.__table__.columns if 'task' in c.name])"`

Expected: `OK ['task_id']`

- [ ] **Step 3: Commit**

```bash
git add src/domain/models/organisation_tables/reminder_models.py
git commit -m "feat(reminders): add task_id linkage to UserReminder model"
```

---

### Task 3: Create database migration

**Files:**
- Create: `migrations/org_migrations/versions/<hash>_035_add_task_source_assignment_reminder_link.py`

- [ ] **Step 1: Generate migration**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run alembic -c migrations/org_migrations/alembic.ini revision -m "035_add_task_source_assignment_reminder_link"`

If autogenerate is not available, create manually with revision chained from `2701960d485f` (034):

```python
"""035 add task source, assignment, and reminder link columns.

Revision ID: <generated>
Revises: 2701960d485f
Create Date: 2026-03-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "<generated>"
down_revision: str | None = "2701960d485f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- crm_activities: source tracking
    op.add_column("crm_activities", sa.Column("source", sa.String(50), nullable=True))
    op.execute(
        "UPDATE crm_activities SET source = 'CRM_SYNC' WHERE integration_id IS NOT NULL"
    )
    op.execute(
        "UPDATE crm_activities SET source = 'MANUAL' WHERE integration_id IS NULL"
    )
    op.create_index(
        "ix_crm_activities_source", "crm_activities", ["organisation_id", "source"]
    )

    # -- crm_activities: internal assignment
    op.add_column(
        "crm_activities", sa.Column("assigned_to_user_id", sa.UUID(), nullable=True)
    )
    op.execute(
        "UPDATE crm_activities SET assigned_to_user_id = owner_id WHERE owner_id IS NOT NULL"
    )
    op.create_index(
        "ix_crm_activities_assigned_to",
        "crm_activities",
        ["organisation_id", "assigned_to_user_id"],
    )

    # -- user_reminders: task linkage
    op.add_column("user_reminders", sa.Column("task_id", sa.UUID(), nullable=True))
    op.create_index("ix_user_reminders_task", "user_reminders", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_user_reminders_task", table_name="user_reminders")
    op.drop_column("user_reminders", "task_id")
    op.drop_index("ix_crm_activities_assigned_to", table_name="crm_activities")
    op.drop_column("crm_activities", "assigned_to_user_id")
    op.drop_index("ix_crm_activities_source", table_name="crm_activities")
    op.drop_column("crm_activities", "source")
```

- [ ] **Step 2: Verify migration syntax**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run python -c "import importlib.util; spec = importlib.util.spec_from_file_location('m', 'migrations/org_migrations/versions/<filename>.py'); mod = importlib.util.module_from_spec(spec); print('Syntax OK')"`

- [ ] **Step 3: Commit**

```bash
git add migrations/org_migrations/versions/
git commit -m "feat(migration): 035 add task source, assignment, and reminder link"
```

---

## Chunk 2: Schema Layer (Request/Response Models)

### Task 4: Update Pydantic schemas for new fields and global task support

**Files:**
- Modify: `src/api/schemas/crm_task_models.py`

- [ ] **Step 1: Add ReminderConfig nested schema and update TaskListItem**

Add after imports (line 15):

```python
from src.domain.models.organisation_tables.sync.crm import CRMActivitySource


class ReminderConfig(BaseModel):
    """Nested reminder configuration for task create/update."""

    enabled: bool = Field(description="Whether to create/keep a reminder for this task.")
    trigger_time: datetime | None = Field(
        default=None, description="When to trigger the reminder (UTC). Required if enabled."
    )
    recurrence: str = Field(
        default="NONE",
        description="Recurrence pattern: NONE, DAILY, WEEKDAYS, WEEKLY, BIWEEKLY, MONTHLY.",
    )

    @model_validator(mode="after")
    def _validate_trigger_time(self) -> ReminderConfig:
        if self.enabled and not self.trigger_time:
            msg = "trigger_time is required when reminder is enabled"
            raise ValueError(msg)
        return self
```

- [ ] **Step 2: Add new fields to TaskListItem**

In `TaskListItem` (line 32), add after `deal_external_id` (line 49):

```python
    deal_name: str | None = None
    source: str | None = None
    assigned_to_user_id: UUID | None = None
    assigned_to_name: str | None = None
    assigned_to_email: str | None = None
    has_reminder: bool = False
```

- [ ] **Step 3: Add new fields to TaskDetail**

In `TaskDetail` (line 71), add after `owner_email` (line 92):

```python
    # Assignment
    assigned_to_user_id: UUID | None = None
    assigned_to_name: str | None = None
    assigned_to_email: str | None = None

    # Source
    source: str | None = None

    # Reminder info (populated from linked UserReminder)
    has_reminder: bool = False
    reminder: dict | None = Field(
        default=None,
        description="Linked reminder details: {reminder_id, status, trigger_time, recurrence}",
    )
```

- [ ] **Step 4: Update CreateTaskRequest with deal_id and reminder**

In `CreateTaskRequest` (line 122), add `deal_id` and `reminder` fields:

```python
    deal_id: UUID | None = Field(
        default=None,
        description="Link task to a deal. If null, creates a standalone task.",
    )
    reminder: ReminderConfig | None = Field(
        default=None,
        description="Optional reminder configuration.",
    )
```

- [ ] **Step 5: Update UpdateTaskRequest with deal_id, assigned_to_user_id, and reminder**

In `UpdateTaskRequest` (line 190), add:

```python
    deal_id: UUID | None = Field(
        default=None,
        description="Link or unlink task from a deal. Pass null to remove deal association.",
    )
    assigned_to_user_id: UUID | None = Field(
        default=None,
        description="Reassign the task to a different user.",
    )
    reminder: ReminderConfig | None = Field(
        default=None,
        description="Create, update, or remove reminder. Set enabled=false to remove.",
    )
```

- [ ] **Step 6: Add GlobalTaskListParams query model**

Add at the end of the file:

```python
class GlobalTaskListParams(BaseModel):
    """Query parameters for global task listing."""

    deal_id: UUID | None = Field(default=None, description="Filter by deal")
    statuses: list[str] | None = Field(default=None, description="Filter by statuses")
    priorities: list[str] | None = Field(default=None, description="Filter by priorities")
    assigned_to_user_id: UUID | None = Field(default=None, description="Filter by assignee")
    source: list[str] | None = Field(default=None, description="Filter by source")
    due_from: datetime | None = Field(default=None, description="Due date range start")
    due_to: datetime | None = Field(default=None, description="Due date range end")
    is_overdue: bool | None = Field(default=None, description="Filter overdue tasks only")
    has_due_date: bool | None = Field(default=None, description="Filter by has/no due date")
    include_completed: bool = Field(default=False, description="Include completed tasks")
    sort_by: str = Field(default="due_date", description="Sort field")
    sort_order: str = Field(default="asc", pattern="^(asc|desc)$", description="Sort direction")
    page: int = Field(default=1, ge=1, description="Page number")
    limit: int = Field(default=50, ge=1, le=100, description="Page size")
```

- [ ] **Step 7: Verify schemas import correctly**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run python -c "from src.api.schemas.crm_task_models import ReminderConfig, GlobalTaskListParams, CreateTaskRequest; print('OK')"`

Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add src/api/schemas/crm_task_models.py
git commit -m "feat(tasks): add global task schemas with reminder config and assignment"
```

---

## Chunk 3: Service Layer

### Task 5: Add global list method to TasksAccessService

**Files:**
- Modify: `src/service/crm/access/tasks_access_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/service/crm/access/test_tasks_access_service.py`:

```python
"""Tests for TasksAccessService.list_tasks() global query."""

import pytest


class TestListTasks:
    """Tests for the global list_tasks method."""

    def test_list_tasks_import(self):
        """Verify list_tasks method exists on TasksAccessService."""
        from src.service.crm.access.tasks_access_service import TasksAccessService

        assert hasattr(TasksAccessService, "list_tasks")
```

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run pytest tests/service/crm/access/test_tasks_access_service.py -v`

Expected: FAIL — `AssertionError: list_tasks not found`

- [ ] **Step 2: Implement `list_tasks` method**

In `src/service/crm/access/tasks_access_service.py`, add after `list_tasks_by_deal` (after line 93):

```python
    async def list_tasks(
        self,
        *,
        deal_id: UUID | None = None,
        limit: int = 50,
        page: int = 1,
        statuses: list[str] | None = None,
        priorities: list[str] | None = None,
        assigned_to_user_id: UUID | None = None,
        source: list[str] | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        is_overdue: bool | None = None,
        has_due_date: bool | None = None,
        include_completed: bool = False,
        sort_by: str = "due_date",
        sort_order: str = "asc",
    ) -> tuple[list[CRMActivity], int]:
        """List tasks across the org with flexible filtering.

        Used by the global /tasks page and the deal detail page (via deal_id filter).
        Only returns TASK/TODO activity types. Excludes soft-deleted records.
        """

        base = (
            select(CRMActivity)
            .where(CRMActivity.organisation_id == self._organisation_id)
            .where(CRMActivity.is_deleted.is_(False))
            .where(
                or_(
                    CRMActivity.activity_type.in_(_TASK_ACTIVITY_TYPES),
                    CRMActivity.activity_type.is_(None),
                )
            )
        )

        if deal_id is not None:
            base = base.where(CRMActivity.deal_id == deal_id)

        if statuses:
            upper = [s.upper() for s in statuses]
            base = base.where(CRMActivity.status.in_(upper))
        elif not include_completed:
            base = base.where(
                or_(
                    CRMActivity.status != "COMPLETED",
                    CRMActivity.status.is_(None),
                )
            )

        if priorities:
            upper = [p.upper() for p in priorities]
            base = base.where(CRMActivity.priority.in_(upper))

        if assigned_to_user_id is not None:
            base = base.where(CRMActivity.assigned_to_user_id == assigned_to_user_id)

        if source:
            upper = [s.upper() for s in source]
            base = base.where(CRMActivity.source.in_(upper))

        if due_from is not None:
            base = base.where(CRMActivity.due_date >= due_from)

        if due_to is not None:
            base = base.where(CRMActivity.due_date <= due_to)

        if is_overdue is True:
            now = datetime.now(UTC)
            base = base.where(CRMActivity.due_date < now).where(
                CRMActivity.status != "COMPLETED"
            )

        if has_due_date is True:
            base = base.where(CRMActivity.due_date.isnot(None))
        elif has_due_date is False:
            base = base.where(CRMActivity.due_date.is_(None))

        # Sorting
        sort_col_map = {
            "due_date": CRMActivity.due_date,
            "priority": CRMActivity.priority,
            "created_at": CRMActivity.created_at,
            "updated_at": CRMActivity.updated_at,
        }
        sort_col = sort_col_map.get(sort_by, CRMActivity.due_date)
        if sort_order == "desc":
            base = base.order_by(sort_col.desc().nulls_last())
        else:
            base = base.order_by(sort_col.asc().nulls_last())

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_q)).scalar() or 0

        offset = (page - 1) * limit
        rows_q = base.offset(offset).limit(limit)
        result = await self._session.execute(rows_q)
        tasks = list(result.scalars().all())

        return tasks, total
```

Also add `from datetime import UTC, datetime` to the imports at the top if not present.

- [ ] **Step 3: Run test to verify it passes**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run pytest tests/service/crm/access/test_tasks_access_service.py -v`

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/service/crm/access/tasks_access_service.py tests/service/crm/access/test_tasks_access_service.py
git commit -m "feat(tasks): add global list_tasks method to TasksAccessService"
```

---

### Task 6: Update TasksWriteService — optional deal_id, reminder lifecycle, source tracking

**Files:**
- Modify: `src/service/crm/internal/tasks_write_service.py`

- [ ] **Step 1: Update `create_task` to accept optional deal_id**

The current signature is `create_task(deal_id: UUID, payload)`. Change to:

```python
    async def create_task(
        self, payload: dict[str, Any], *, deal_id: UUID | None = None
    ) -> CRMActivity:
```

Move `deal_id` from a positional arg to an optional kwarg. Update the body:

- If `deal_id` is provided: load and validate the deal (existing logic)
- If `deal_id` is None: skip deal validation, leave deal fields null
- Set `source = "MANUAL"` on all user-created tasks
- Set `assigned_to_user_id` from payload (default to `self._actor_user_id`)
- Handle `reminder` dict from payload → create UserReminder

Key changes to the method body:

```python
        # Deal linkage (optional)
        deal = None
        if deal_id:
            deal = await self._load_deal(deal_id)

        # Assignment
        assign_user_id = payload.get("assigned_to_user_id") or self._actor_user_id
        assigned_ext_id, assigned_name, assigned_email = await self._resolve_owner(
            assign_user_id
        )

        task = CRMActivity(
            organisation_id=self._organisation_id,
            external_id=str(uuid4()),
            subject=payload.get("subject"),
            body=payload.get("body"),
            status=self._normalize_enum(payload.get("status")) or "NOT_STARTED",
            activity_type=self._normalize_enum(payload.get("task_type")) or "TASK",
            priority=self._normalize_enum(payload.get("priority")) or "MEDIUM",
            due_date=payload.get("due_date"),
            reminder_date=payload.get("reminder_date"),
            completed_date=payload.get("completed_date"),
            # Deal fields (null if standalone)
            deal_id=deal.deal_id if deal else None,
            deal_external_id=deal.external_id if deal else None,
            related_object_type="DEAL" if deal else None,
            related_external_id=deal.external_id if deal else None,
            related_local_id=deal.deal_id if deal else None,
            related_name=deal.name if deal else None,
            # Assignment
            owner_external_id=assigned_ext_id,
            owner_name=assigned_name,
            owner_email=assigned_email,
            owner_id=assign_user_id,
            assigned_to_user_id=assign_user_id,
            # Source
            source="MANUAL",
            # Metadata
            synced_at=datetime.now(UTC),
            properties={
                "is_user_created": True,
                "created_via": "api",
                "created_by": str(self._actor_user_id),
            },
        )
        self._session.add(task)
        await self._session.flush()
```

- [ ] **Step 2: Add reminder creation helper**

Add a private method to create a linked UserReminder:

```python
    async def _create_task_reminder(
        self, task: CRMActivity, reminder_config: dict[str, Any]
    ) -> None:
        """Create a UserReminder linked to a task."""
        from src.domain.models.organisation_tables.reminder_models import (
            ReminderRecurrence,
            ReminderStatus,
            ReminderType,
            UserReminder,
        )
        from src.infra.messaging.reminder_schedule import schedule_reminder

        trigger_time = reminder_config["trigger_time"]
        recurrence_str = reminder_config.get("recurrence", "NONE").upper()

        reminder = UserReminder(
            organisation_id=self._organisation_id,
            user_id=task.assigned_to_user_id or self._actor_user_id,
            reminder_type=ReminderType.STATIC.value,
            title=f"Task: {task.subject or 'Untitled'}",
            message_content=f"Task reminder: {task.subject or 'Untitled'}\n\n{task.body or ''}".strip(),
            next_trigger_at=trigger_time,
            user_timezone="UTC",
            recurrence=recurrence_str,
            delivery_channel="teams",
            delivery_context={},
            status=ReminderStatus.ACTIVE.value,
            task_id=task.activity_id,
        )
        self._session.add(reminder)
        await self._session.flush()

        # Set reminder_date on the task for display
        task.reminder_date = trigger_time

        # Schedule in Redis
        try:
            await schedule_reminder(
                str(self._organisation_id),
                str(reminder.reminder_id),
                int(trigger_time.timestamp()),
            )
        except Exception:
            logger.warning("Failed to schedule reminder in Redis", exc_info=True)
```

- [ ] **Step 3: Add reminder cancellation helper**

```python
    async def _cancel_task_reminders(self, task_id: UUID) -> None:
        """Cancel all active reminders linked to a task."""
        from src.domain.models.organisation_tables.reminder_models import UserReminder
        from src.infra.messaging.reminder_schedule import unschedule_reminder

        stmt = (
            select(UserReminder)
            .where(UserReminder.task_id == task_id)
            .where(UserReminder.organisation_id == self._organisation_id)
            .where(UserReminder.status.in_(["ACTIVE", "PAUSED", "PENDING_CONFIRMATION"]))
        )
        result = await self._session.execute(stmt)
        reminders = list(result.scalars().all())

        for reminder in reminders:
            reminder.status = "CANCELLED"
            try:
                await unschedule_reminder(
                    str(self._organisation_id), str(reminder.reminder_id)
                )
            except Exception:
                logger.warning("Failed to unschedule reminder %s", reminder.reminder_id)

        task_activity = await self._session.get(CRMActivity, task_id)
        if task_activity:
            task_activity.reminder_date = None
```

- [ ] **Step 4: Wire reminder into create_task and update_task**

In `create_task`, after `await self._session.flush()`:

```python
        # Create linked reminder if requested
        reminder_config = payload.get("reminder")
        if reminder_config and reminder_config.get("enabled"):
            await self._create_task_reminder(task, reminder_config)
```

In `update_task`, handle reminder updates:

```python
        # Handle reminder updates
        reminder_config = payload.get("reminder")
        if reminder_config is not None:
            if reminder_config.get("enabled"):
                # Cancel existing, create new
                await self._cancel_task_reminders(task.activity_id)
                await self._create_task_reminder(task, reminder_config)
            else:
                # Remove reminder
                await self._cancel_task_reminders(task.activity_id)
```

- [ ] **Step 5: Update update_task for deal_id and assigned_to_user_id changes**

In `update_task`, add handling for deal reassociation and user reassignment:

```python
        # Deal reassociation
        if "deal_id" in payload:
            new_deal_id = payload["deal_id"]
            if new_deal_id is None:
                # Remove deal association
                task.deal_id = None
                task.deal_external_id = None
                task.related_object_type = None
                task.related_external_id = None
                task.related_local_id = None
                task.related_name = None
            else:
                deal = await self._load_deal(new_deal_id)
                task.deal_id = deal.deal_id
                task.deal_external_id = deal.external_id
                task.related_object_type = "DEAL"
                task.related_external_id = deal.external_id
                task.related_local_id = deal.deal_id
                task.related_name = deal.name

        # Reassignment
        if "assigned_to_user_id" in payload and payload["assigned_to_user_id"] is not None:
            new_user_id = payload["assigned_to_user_id"]
            ext_id, name, email = await self._resolve_owner(new_user_id)
            task.assigned_to_user_id = new_user_id
            task.owner_external_id = ext_id
            task.owner_name = name
            task.owner_email = email
            task.owner_id = new_user_id
```

- [ ] **Step 6: Update delete_task to cancel reminders**

In `delete_task`, before setting `is_deleted = True`:

```python
        await self._cancel_task_reminders(task.activity_id)
```

- [ ] **Step 7: Verify service imports**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run python -c "from src.service.crm.internal.tasks_write_service import TasksWriteService; print('OK')"`

Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add src/service/crm/internal/tasks_write_service.py
git commit -m "feat(tasks): optional deal_id, reminder lifecycle, source tracking in write service"
```

---

## Chunk 4: API Handler Layer

### Task 7: Add global task endpoints to tasks_handler.py

**Files:**
- Modify: `src/api/routers/crm/tasks_handler.py`

- [ ] **Step 1: Add import for new schema**

Add to imports:

```python
from src.api.schemas.crm_task_models import GlobalTaskListParams, ReminderConfig
```

- [ ] **Step 2: Add `GET /tasks` endpoint**

Add after the existing `list_deal_tasks` endpoint (after line 146):

```python
@tasks_router.get(
    "/tasks",
    response_model=GenericResponse[TasksPage],
    tags=["crm", "tasks"],
    summary="List tasks globally or filtered by deal",
    description=(
        "Retrieve a paginated list of tasks across all deals or filtered by deal_id. "
        "Supports filtering by status, priority, assignee, source, date range, and overdue state."
    ),
)
async def list_tasks(
    response: FastAPIResponse,
    deal_id: UUID | None = Query(None, description="Filter by deal ID"),
    statuses: list[str] | None = Query(None, description="Filter by statuses"),
    priorities: list[str] | None = Query(None, description="Filter by priorities"),
    assigned_to_user_id: UUID | None = Query(None, description="Filter by assignee"),
    source: list[str] | None = Query(None, description="Filter by source"),
    due_from: datetime | None = Query(None, description="Due date range start (UTC)"),
    due_to: datetime | None = Query(None, description="Due date range end (UTC)"),
    is_overdue: bool | None = Query(None, description="Filter overdue tasks"),
    has_due_date: bool | None = Query(None, description="Filter tasks with/without due date"),
    include_completed: bool = Query(False, description="Include completed tasks"),
    sort_by: str = Query("due_date", description="Sort field"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$", description="Sort direction"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    service: TasksAccessService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TasksPage]:
    """List tasks globally with optional deal filtering."""

    try:
        tasks, total_count = await service.list_tasks(
            deal_id=deal_id,
            limit=limit,
            page=page,
            statuses=statuses,
            priorities=priorities,
            assigned_to_user_id=assigned_to_user_id,
            source=source,
            due_from=due_from,
            due_to=due_to,
            is_overdue=is_overdue,
            has_due_date=has_due_date,
            include_completed=include_completed,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        items = []
        for task in tasks:
            item = TaskListItem.model_validate(task, from_attributes=True)
            item = item.model_copy(
                update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
            )
            items.append(item)

        total_pages = (total_count + limit - 1) // limit if limit else 0
        payload = TasksPage(
            items=items, page=page, limit=limit,
            total_count=total_count, total_pages=total_pages,
        )
        return Response.success_response(message="Tasks retrieved successfully", data=payload)

    except TasksAccessServiceError as exc:
        logger.warning("Tasks access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing tasks: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
```

- [ ] **Step 3: Add `POST /tasks` endpoint (standalone, optional deal_id)**

Add after the new `list_tasks` endpoint:

```python
@tasks_router.post(
    "/tasks",
    response_model=GenericResponse[TaskDetail],
    status_code=201,
    tags=["crm", "tasks"],
    summary="Create a task",
    description="Create a task with optional deal association and reminder.",
)
async def create_task_global(
    request: CreateTaskRequest,
    response: FastAPIResponse,
    service: TasksWriteService = Depends(get_tasks_write_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Create a new task. If deal_id is provided, links to that deal."""

    try:
        payload = request.model_dump(exclude_unset=True)
        deal_id = payload.pop("deal_id", None)
        task = await service.create_task(payload, deal_id=deal_id)
        await service.session.commit()
        detail = TaskDetail.model_validate(task, from_attributes=True)
        detail = detail.model_copy(
            update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
        )
        return Response.created_response(
            message="Task created successfully", data=detail, response=response,
        )
    except TasksWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Task write error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error creating task: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
```

- [ ] **Step 4: Update existing `create_task` endpoint for backwards compat**

Update the existing `POST /deals/{deal_id}/tasks` handler to delegate to the write service with the new signature:

```python
        task = await service.create_task(
            request.model_dump(exclude_unset=True), deal_id=deal_id
        )
```

- [ ] **Step 5: Verify endpoints register**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run python -c "from src.api.routers.crm.tasks_handler import tasks_router; routes = [r.path for r in tasks_router.routes]; print(routes)"`

Expected: Should include `/tasks`, `/tasks/{task_id}`, `/deals/{deal_id}/tasks`

- [ ] **Step 6: Commit**

```bash
git add src/api/routers/crm/tasks_handler.py
git commit -m "feat(tasks): add global GET /tasks and POST /tasks endpoints"
```

---

## Chunk 5: Tests

### Task 8: Write tests for global task endpoints

**Files:**
- Create: `tests/api/routers/crm/test_tasks_global_handler.py`

- [ ] **Step 1: Write handler-level tests**

```python
"""Tests for global task endpoints (GET /tasks, POST /tasks)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.routers.crm.tasks_handler import (
    get_tasks_access_service,
    get_tasks_write_service,
    tasks_router,
)
from src.api.dependencies.organisation_dependencies import get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write


ORG_ID = uuid4()
USER_ID = uuid4()
DEAL_ID = uuid4()


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(tasks_router, prefix="/users/crm")
    return app


def _fake_user():
    return MagicMock(organisation_id=ORG_ID, user_id=USER_ID)


def _make_fake_task(
    *,
    task_id=None,
    subject="Test task",
    deal_id=None,
    status="NOT_STARTED",
    priority="MEDIUM",
    source="MANUAL",
    due_date=None,
):
    task = MagicMock()
    task.activity_id = task_id or uuid4()
    task.task_id = task.activity_id
    task.external_id = str(uuid4())
    task.subject = subject
    task.body = None
    task.status = status
    task.activity_type = "TASK"
    task.task_type = "TASK"
    task.priority = priority
    task.source = source
    task.due_date = due_date
    task.completed_date = None
    task.reminder_date = None
    task.owner_external_id = None
    task.owner_name = None
    task.owner_email = None
    task.owner_id = None
    task.assigned_to_user_id = USER_ID
    task.assigned_to_name = "Test User"
    task.assigned_to_email = "test@example.com"
    task.deal_id = deal_id
    task.deal_external_id = None
    task.deal_name = None
    task.is_deleted = False
    task.is_deleted_remote = False
    task.is_overdue = False
    task.is_all_day = False
    task.has_reminder = False
    task.properties = {"is_user_created": True}
    task.metrics = {}
    task.integration_id = None
    task.organisation_id = ORG_ID
    task.synced_at = datetime.now(UTC)
    task.last_seen_at = None
    task.source_created_at = None
    task.source_modified_at = None
    task.created_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    return task


class TestListTasksGlobal:
    """Tests for GET /users/crm/tasks."""

    @pytest.mark.anyio
    async def test_list_tasks_empty(self):
        app = _make_app()
        mock_service = AsyncMock()
        mock_service.list_tasks = AsyncMock(return_value=([], 0))
        mock_service.close = AsyncMock()

        app.dependency_overrides[get_current_user] = _fake_user
        app.dependency_overrides[get_tasks_access_service] = lambda: mock_service
        app.dependency_overrides[rate_limit_user_read] = lambda: None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/users/crm/tasks")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_count"] == 0
        assert data["items"] == []

    @pytest.mark.anyio
    async def test_list_tasks_with_deal_filter(self):
        app = _make_app()
        task = _make_fake_task(deal_id=DEAL_ID)
        mock_service = AsyncMock()
        mock_service.list_tasks = AsyncMock(return_value=([task], 1))
        mock_service.close = AsyncMock()

        app.dependency_overrides[get_current_user] = _fake_user
        app.dependency_overrides[get_tasks_access_service] = lambda: mock_service
        app.dependency_overrides[rate_limit_user_read] = lambda: None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/users/crm/tasks?deal_id={DEAL_ID}")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_count"] == 1

    @pytest.mark.anyio
    async def test_list_tasks_overdue_filter(self):
        app = _make_app()
        overdue_task = _make_fake_task(
            due_date=datetime.now(UTC) - timedelta(days=1),
            status="NOT_STARTED",
        )
        mock_service = AsyncMock()
        mock_service.list_tasks = AsyncMock(return_value=([overdue_task], 1))
        mock_service.close = AsyncMock()

        app.dependency_overrides[get_current_user] = _fake_user
        app.dependency_overrides[get_tasks_access_service] = lambda: mock_service
        app.dependency_overrides[rate_limit_user_read] = lambda: None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/users/crm/tasks?is_overdue=true")

        assert resp.status_code == 200


class TestCreateTaskGlobal:
    """Tests for POST /users/crm/tasks."""

    @pytest.mark.anyio
    async def test_create_standalone_task(self):
        app = _make_app()
        task = _make_fake_task(subject="Standalone task")
        mock_service = AsyncMock()
        mock_service.create_task = AsyncMock(return_value=task)
        mock_service.session = AsyncMock()
        mock_service.session.commit = AsyncMock()
        mock_service.session.rollback = AsyncMock()
        mock_service.close = AsyncMock()

        app.dependency_overrides[get_current_user] = _fake_user
        app.dependency_overrides[get_tasks_write_service] = lambda: mock_service
        app.dependency_overrides[rate_limit_user_write] = lambda: None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/users/crm/tasks",
                json={"subject": "Standalone task"},
            )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_create_task_with_deal(self):
        app = _make_app()
        task = _make_fake_task(subject="Deal task", deal_id=DEAL_ID)
        mock_service = AsyncMock()
        mock_service.create_task = AsyncMock(return_value=task)
        mock_service.session = AsyncMock()
        mock_service.session.commit = AsyncMock()
        mock_service.session.rollback = AsyncMock()
        mock_service.close = AsyncMock()

        app.dependency_overrides[get_current_user] = _fake_user
        app.dependency_overrides[get_tasks_write_service] = lambda: mock_service
        app.dependency_overrides[rate_limit_user_write] = lambda: None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/users/crm/tasks",
                json={"subject": "Deal task", "deal_id": str(DEAL_ID)},
            )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_create_task_requires_subject_or_body(self):
        app = _make_app()
        mock_service = AsyncMock()
        mock_service.close = AsyncMock()

        app.dependency_overrides[get_current_user] = _fake_user
        app.dependency_overrides[get_tasks_write_service] = lambda: mock_service
        app.dependency_overrides[rate_limit_user_write] = lambda: None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/users/crm/tasks", json={})

        assert resp.status_code == 422
```

- [ ] **Step 2: Run tests**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run pytest tests/api/routers/crm/test_tasks_global_handler.py -v`

Expected: All tests PASS (or fail initially if code isn't wired yet — fix iteratively)

- [ ] **Step 3: Commit**

```bash
git add tests/api/routers/crm/test_tasks_global_handler.py
git commit -m "test(tasks): add tests for global task CRUD endpoints"
```

---

### Task 9: Update existing tests for new fields

**Files:**
- Modify: `tests/api/routers/crm/test_tasks_handler.py`

- [ ] **Step 1: Update mock task factory in existing tests**

Add `source`, `assigned_to_user_id`, `assigned_to_name`, `assigned_to_email`, `deal_name`, `has_reminder` fields to any mock task objects in `test_tasks_handler.py` to prevent validation errors with the updated schemas.

- [ ] **Step 2: Run full test suite**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run pytest tests/api/routers/crm/test_tasks_handler.py tests/api/routers/crm/test_tasks_global_handler.py -v`

Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/api/routers/crm/test_tasks_handler.py
git commit -m "test(tasks): update existing task tests for new schema fields"
```

---

## Chunk 6: Final Wiring and Lint

### Task 10: Final integration and quality checks

**Files:**
- Possibly modify: `src/api/fastapi_app.py` (if router mount needs updating)

- [ ] **Step 1: Verify router mounting**

The tasks_router is already mounted at `/users/crm` (line 191 of fastapi_app.py). The new `/tasks` and `/tasks/{task_id}` routes will automatically be available at `/users/crm/tasks` and `/users/crm/tasks/{task_id}`. No changes needed.

- [ ] **Step 2: Run ruff lint**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run ruff check src/api/routers/crm/tasks_handler.py src/api/schemas/crm_task_models.py src/service/crm/access/tasks_access_service.py src/service/crm/internal/tasks_write_service.py src/domain/models/organisation_tables/sync/crm/activities.py src/domain/models/organisation_tables/reminder_models.py`

Fix any lint errors found.

- [ ] **Step 3: Run ruff format**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run ruff format src/api/routers/crm/tasks_handler.py src/api/schemas/crm_task_models.py src/service/crm/access/tasks_access_service.py src/service/crm/internal/tasks_write_service.py`

- [ ] **Step 4: Run mypy on changed files**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run mypy src/api/routers/crm/tasks_handler.py src/api/schemas/crm_task_models.py src/service/crm/access/tasks_access_service.py src/service/crm/internal/tasks_write_service.py`

Fix any type errors.

- [ ] **Step 5: Run all task-related tests**

Run: `cd D:/CopilotGTM/copilot-backend-deal-tasks && uv run pytest tests/api/routers/crm/test_tasks_handler.py tests/api/routers/crm/test_tasks_global_handler.py -v`

Expected: All tests PASS

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "chore(tasks): lint and type check fixes"
```

---

## Summary

| Task | Description | Files Changed |
|------|-------------|---------------|
| 1 | Add CRMActivitySource enum + new model fields | activities.py, __init__.py |
| 2 | Add task_id to UserReminder | reminder_models.py |
| 3 | Database migration (035) | new migration file |
| 4 | Update Pydantic schemas | crm_task_models.py |
| 5 | Add global list_tasks to access service | tasks_access_service.py |
| 6 | Update write service (optional deal, reminders, source) | tasks_write_service.py |
| 7 | Add global API endpoints | tasks_handler.py |
| 8 | Write global task tests | test_tasks_global_handler.py |
| 9 | Update existing tests | test_tasks_handler.py |
| 10 | Lint, type check, final verification | various |
