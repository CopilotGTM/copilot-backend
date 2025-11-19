"""CRM API response models for deal tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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
    integration_id: UUID
    organisation_id: UUID

    # Timestamps
    synced_at: datetime
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TasksStats(BaseModel):
    """Aggregate statistics for tasks."""

    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_type: dict[str, int]
    overdue_count: int = 0
    due_today_count: int = 0
    due_this_week_count: int = 0
    completed_percentage: float = Field(description="Percentage of tasks marked completed")


class TasksGroupedByStatus(BaseModel):
    """Tasks grouped by status for UI display."""

    new: list[TaskListItem] = Field(default_factory=list)
    pending: list[TaskListItem] = Field(default_factory=list)
    completed: list[TaskListItem] = Field(default_factory=list)
    progress_percentage: float = 0.0
