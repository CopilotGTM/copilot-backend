"""CRM API response models for deal tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivityStatus,
    CRMActivityType,
)


class TaskListItem(BaseModel):
    """Individual task item in a list response."""

    task_id: UUID
    external_id: str
    subject: str | None
    body: str | None = None
    status: str | None
    task_type: str | None
    priority: str | None
    due_date: datetime | None
    completed_date: datetime | None
    reminder_date: datetime | None
    owner_external_id: str | None
    owner_name: str | None
    owner_email: str | None
    deal_id: UUID | None
    deal_external_id: str | None
    is_overdue: bool = Field(
        default=False,
        description="Computed flag: due_date < now and status != COMPLETED",
    )
    created_at: datetime
    updated_at: datetime
    synced_at: datetime

    model_config = {"from_attributes": True}


class TasksPage(BaseModel):
    """Paginated list of tasks."""

    items: list[TaskListItem]
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")


class TaskDetail(BaseModel):
    """Detailed task information including all available fields."""

    # Identity
    task_id: UUID
    external_id: str

    # Deal linkage
    deal_id: UUID | None
    deal_external_id: str | None

    # Core task fields
    subject: str | None
    body: str | None
    status: str | None
    task_type: str | None
    priority: str | None

    # Assignment
    owner_external_id: str | None
    owner_name: str | None
    owner_email: str | None

    # Timing
    due_date: datetime | None
    completed_date: datetime | None
    reminder_date: datetime | None
    source_created_at: datetime | None
    source_modified_at: datetime | None

    # Flags
    is_deleted_remote: bool
    is_overdue: bool = False

    # Payloads
    properties: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None

    # Integration metadata
    integration_id: UUID | None
    organisation_id: UUID

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

    @field_validator("subject", "body")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

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

    @field_validator("subject", "body")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, value: CRMActivityType | None) -> CRMActivityType | None:
        if value is None:
            return None
        if value not in {CRMActivityType.TASK, CRMActivityType.TODO}:
            msg = "task_type must be TASK or TODO"
            raise ValueError(msg)
        return value
