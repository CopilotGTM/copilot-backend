"""CRM API response models for deal tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field, field_validator, model_validator
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
)

VALID_REMINDER_DELIVERY_CHANNELS = {"teams", "slack"}


class TaskAssignee(BaseModel):
    """Canonical assignee metadata for task display and filtering."""

    kind: str = Field(
        description="Assignee source: internal_user, crm_owner, external, or unknown."
    )
    filter_key: str = Field(description="Stable key used by task assignee filters.")
    name: str | None = None
    email: str | None = None
    public_user_id: UUID | None = None
    crm_user_id: UUID | None = None


class TaskAssigneeOption(BaseModel):
    """Distinct assignee option for task filter controls."""

    assigned_to: TaskAssignee
    task_count: int = Field(default=0, ge=0)


class TaskAssigneesResponse(BaseModel):
    """Task assignee filter options."""

    assignees: list[TaskAssigneeOption]


class ReminderConfig(BaseModel):
    """Nested reminder configuration for task create/update."""

    enabled: bool = Field(description="Whether to create/keep a reminder for this task.")
    trigger_time: datetime | None = Field(
        default=None, description="When to trigger the reminder (UTC). Required if enabled."
    )
    recurrence: str = Field(
        default="NONE",
        description="Recurrence: NONE, DAILY, WEEKDAYS, WEEKLY, BIWEEKLY, MONTHLY.",
    )
    delivery_channel: str | None = Field(
        default=None,
        description="Reminder delivery channel: teams or slack.",
    )
    timezone: str | None = Field(
        default=None,
        description="IANA timezone for the reminder schedule, e.g. Asia/Kolkata.",
    )

    @model_validator(mode="after")
    def _validate_trigger_time(self) -> ReminderConfig:
        if self.enabled and not self.trigger_time:
            msg = "trigger_time is required when reminder is enabled"
            raise ValueError(msg)
        if self.enabled and self.trigger_time:
            trigger_time = (
                self.trigger_time
                if self.trigger_time.tzinfo
                else self.trigger_time.replace(tzinfo=UTC)
            )
            if trigger_time <= datetime.now(UTC):
                msg = "trigger_time must be in the future when reminder is enabled"
                raise ValueError(msg)
        if (
            self.delivery_channel is not None
            and self.delivery_channel not in VALID_REMINDER_DELIVERY_CHANNELS
        ):
            msg = (
                "delivery_channel must be one of: "
                f"{', '.join(sorted(VALID_REMINDER_DELIVERY_CHANNELS))}"
            )
            raise ValueError(msg)
        return self


class DealTimelineQueryParams(BaseModel):
    """Query parameters for deal timeline listing."""

    limit: int = Query(20, ge=1, le=100, description="Number of events per page")
    cursor: str | None = Query(None, description="Pagination cursor")
    event_types: str | None = Query(
        None, description="Comma-separated event types to filter (EMAIL,MEETING)"
    )
    start_date: datetime | None = Query(None, description="Filter events after this date")
    end_date: datetime | None = Query(None, description="Filter events before this date")
    include_excluded: bool = Query(False, description="Include excluded/hidden events")
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort order")


class TaskListItem(BaseModel):
    """Individual task item in a list response."""

    task_id: UUID
    subject: str | None
    body: str | None = None
    status: str | None
    task_type: str | None
    priority: str | None
    due_date: datetime | None
    completed_date: datetime | None
    reminder_date: datetime | None
    due_local_date: str | None = Field(
        default=None,
        description="Selected local calendar date for all-day/date-only tasks.",
    )
    due_timezone: str | None = Field(
        default=None,
        description="IANA timezone used to interpret due_local_date.",
    )
    is_all_day: bool = Field(
        default=False,
        description="True when the task due date should be displayed as a local date.",
    )
    # Assigned to (owner)
    owner_id: UUID | None
    owner_name: str | None
    owner_email: str | None
    assigned_to: TaskAssignee | None = Field(
        default=None,
        description="Canonical assignee used for display and filtering.",
    )
    # External assignee (non-app user)
    external_assignee_email: str | None = None
    external_assignee_name: str | None = None
    # Deal linkage
    deal_id: UUID | None
    deal_name: str | None = Field(default=None, description="Denormalized deal name.")
    # Source and creator
    source: str | None = None
    created_by_user_id: UUID | None = None
    # Computed
    is_overdue: bool = Field(
        default=False,
        description="Computed flag: due_date < now and status != COMPLETED",
    )
    has_reminder: bool = Field(default=False, description="Whether a reminder is linked.")
    is_read_only: bool = Field(
        default=False,
        description=(
            "True when the task originates from an external CRM sync and cannot be "
            "edited via the user API. Authoritative editability flag — the frontend "
            "should use this instead of inspecting `source`."
        ),
    )
    created_at: datetime
    updated_at: datetime
    synced_at: datetime

    model_config = {"from_attributes": True}


class TasksPage(BaseModel):
    """Paginated list of tasks."""

    items: list[TaskListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class TaskOption(BaseModel):
    """User-facing option metadata for task forms and views."""

    value: str = Field(..., description="""Stored canonical value.""")
    label: str = Field(..., description="""Frontend display label.""")
    description: str | None = Field(default=None, description="""Optional help text.""")


class TaskOptionsResponse(BaseModel):
    """Task option metadata returned by the backend."""

    statuses: list[TaskOption]
    priorities: list[TaskOption]
    task_types: list[TaskOption]
    sources: list[TaskOption]
    default_status: CRMActivityStatus = Field(default=CRMActivityStatus.NOT_STARTED)
    default_priority: CRMActivityPriority = Field(default=CRMActivityPriority.NONE)
    default_task_type: CRMActivityType = Field(default=CRMActivityType.TASK)
    default_source: CRMActivitySource = Field(default=CRMActivitySource.MANUAL)


class TaskDetail(BaseModel):
    """Detailed task information including all available fields."""

    # Identity
    task_id: UUID

    # Deal linkage
    deal_id: UUID | None
    deal_name: str | None = None

    # Core task fields
    subject: str | None
    body: str | None
    status: str | None
    task_type: str | None
    priority: str | None

    # Assignment (owner = who is assigned to do it)
    owner_id: UUID | None
    owner_name: str | None
    owner_email: str | None
    assigned_to: TaskAssignee | None = None

    # External assignee (non-app user)
    external_assignee_email: str | None = None
    external_assignee_name: str | None = None

    # Source and creator
    source: str | None = None
    created_by_user_id: UUID | None = None

    # Timing
    due_date: datetime | None
    completed_date: datetime | None
    reminder_date: datetime | None
    due_local_date: str | None = None
    due_timezone: str | None = None
    is_all_day: bool = False
    source_created_at: datetime | None
    source_modified_at: datetime | None

    # Flags
    is_deleted_remote: bool
    is_overdue: bool = False
    has_reminder: bool = False

    # Reminder detail (populated from linked UserReminder)
    reminder: dict | None = Field(
        default=None,
        description="Linked reminder: {reminder_id, status, trigger_time, recurrence}",
    )

    # Payloads
    properties: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None

    # Integration metadata
    integration_id: UUID | None
    organisation_id: UUID
    is_read_only: bool = Field(
        default=False,
        description=(
            "True when the task originates from an external CRM sync and cannot be "
            "edited via the user API. Authoritative editability flag — the frontend "
            "should use this instead of inspecting `source`."
        ),
    )

    # Timestamps
    synced_at: datetime
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CreateTaskRequest(BaseModel):
    """Request payload to create a new task."""

    subject: str | None = Field(
        default=None,
        max_length=500,
        description="Task subject/title (optional, max 500 characters).",
    )
    body: str | None = Field(
        default=None,
        max_length=10000,
        description="Task description/body (optional, max 10000 characters).",
    )
    status: CRMActivityStatus | None = Field(
        default=None,
        description="Task status (NOT_STARTED, IN_PROGRESS, COMPLETED, etc.).",
    )
    task_type: CRMActivityType | None = Field(
        default=None,
        description="Task type (TASK or TODO).",
    )
    priority: CRMActivityPriority | None = Field(
        default=None,
        description="Task priority (NONE, LOW, MEDIUM, HIGH, URGENT).",
    )
    due_date: datetime | None = Field(
        default=None,
        description="Due date/time for the task.",
    )
    is_all_day: bool | None = Field(
        default=None,
        description="Whether due_date is a date-only/all-day task in the user's timezone.",
    )
    reminder_date: datetime | None = Field(
        default=None,
        description="Reminder date/time for the task.",
    )
    completed_date: datetime | None = Field(
        default=None,
        description="Completion date/time for the task.",
    )
    owner_user_id: UUID | None = Field(
        default=None,
        description="Assign the task to a user (UUID from public.users).",
    )
    external_assignee_email: str | None = Field(
        default=None,
        max_length=255,
        description="Email of an external (non-app) person to assign this task to.",
    )
    external_assignee_name: str | None = Field(
        default=None,
        max_length=255,
        description="Display name of the external assignee.",
    )
    deal_id: UUID | None = Field(
        default=None,
        description="Link task to a deal. If null, creates a standalone task.",
    )
    reminder: ReminderConfig | None = Field(
        default=None,
        description="Optional reminder configuration.",
    )

    @field_validator("subject", "body")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    @field_validator("external_assignee_email")
    @classmethod
    def _validate_external_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip().lower()
        if not trimmed:
            return None
        if "@" not in trimmed or "." not in trimmed.split("@")[-1]:
            msg = "Invalid email address"
            raise ValueError(msg)
        return trimmed

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, value: CRMActivityType | None) -> CRMActivityType | None:
        if value is None:
            return None
        if value not in {CRMActivityType.TASK, CRMActivityType.TODO}:
            msg = "task_type must be TASK or TODO"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _require_subject_or_body(self) -> CreateTaskRequest:
        if not self.subject and not self.body:
            msg = "subject or body is required"
            raise ValueError(msg)
        if self.owner_user_id and self.external_assignee_email:
            msg = "Cannot assign to both an internal user and an external person. Choose one."
            raise ValueError(msg)
        return self


class UpdateTaskRequest(BaseModel):
    """Request payload to update an existing task."""

    subject: str | None = Field(
        default=None,
        max_length=500,
        description="Task subject/title (optional, max 500 characters).",
    )
    body: str | None = Field(
        default=None,
        max_length=10000,
        description="Task description/body (optional, max 10000 characters).",
    )
    status: CRMActivityStatus | None = Field(
        default=None,
        description="Task status (NOT_STARTED, IN_PROGRESS, COMPLETED, etc.).",
    )
    task_type: CRMActivityType | None = Field(
        default=None,
        description="Task type (TASK or TODO).",
    )
    priority: CRMActivityPriority | None = Field(
        default=None,
        description="Task priority (NONE, LOW, MEDIUM, HIGH, URGENT).",
    )
    due_date: datetime | None = Field(
        default=None,
        description="Due date/time for the task.",
    )
    is_all_day: bool | None = Field(
        default=None,
        description="Whether due_date is a date-only/all-day task in the user's timezone.",
    )
    reminder_date: datetime | None = Field(
        default=None,
        description="Reminder date/time for the task.",
    )
    completed_date: datetime | None = Field(
        default=None,
        description="Completion date/time for the task.",
    )
    owner_user_id: UUID | None = Field(
        default=None,
        description="Reassign the task to a user (UUID from public.users).",
    )
    external_assignee_email: str | None = Field(
        default=None,
        max_length=255,
        description="Email of an external (non-app) person to assign this task to.",
    )
    external_assignee_name: str | None = Field(
        default=None,
        max_length=255,
        description="Display name of the external assignee.",
    )
    deal_id: UUID | None = Field(
        default=None,
        description="Link or unlink task from a deal. Pass null to remove.",
    )
    reminder: ReminderConfig | None = Field(
        default=None,
        description="Create, update, or remove reminder. Set enabled=false to remove.",
    )

    @field_validator("subject", "body")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    @field_validator("external_assignee_email")
    @classmethod
    def _validate_external_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip().lower()
        if not trimmed:
            return None
        if "@" not in trimmed or "." not in trimmed.split("@")[-1]:
            msg = "Invalid email address"
            raise ValueError(msg)
        return trimmed

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, value: CRMActivityType | None) -> CRMActivityType | None:
        if value is None:
            return None
        if value not in {CRMActivityType.TASK, CRMActivityType.TODO}:
            msg = "task_type must be TASK or TODO"
            raise ValueError(msg)
        return value


class GlobalTaskListParams(BaseModel):
    """Query parameters for global task listing."""

    deal_id: UUID | None = Field(default=None, description="Filter by deal")
    statuses: list[str] | None = Field(default=None, description="Filter by statuses")
    priorities: list[str] | None = Field(default=None, description="Filter by priorities")
    source: list[str] | None = Field(default=None, description="Filter by source")
    owner_user_id: UUID | None = Field(default=None, description="Filter by assignee")
    assigned_to_key: str | None = Field(
        default=None,
        description="Canonical assignee filter key returned by /tasks/assignees.",
    )
    created_by_user_id: UUID | None = Field(default=None, description="Filter by creator")
    due_from: datetime | None = Field(default=None, description="Due date range start")
    due_to: datetime | None = Field(default=None, description="Due date range end")
    is_overdue: bool | None = Field(default=None, description="Filter overdue tasks only")
    has_due_date: bool | None = Field(default=None, description="Filter by has/no due date")
    include_completed: bool = Field(default=False, description="Include completed tasks")
    sort_by: str = Field(default="due_date", description="Sort field")
    sort_order: str = Field(default="asc", pattern="^(asc|desc)$", description="Sort direction")
    page: int = Field(default=1, ge=1, description="Page number")
    limit: int = Field(default=50, ge=1, le=100, description="Page size")
