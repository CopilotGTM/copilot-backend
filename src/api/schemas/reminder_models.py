"""Pydantic schemas for Reminder API endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field, model_validator
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderRunStatus,
    ReminderStatus,
    ReminderType,
)

VALID_DELIVERY_CHANNELS = {"email", "teams", "slack"}


class ReminderCreateRequest(BaseModel):
    """Request model for creating a reminder."""

    title: str = Field(..., max_length=255, description="Short label for the reminder")
    reminder_type: ReminderType = Field(..., description="'static' or 'agentic'")
    message_content: str | None = Field(
        None, description="Text to deliver (required for static reminders)"
    )
    agent_prompt: str | None = Field(
        None, description="AI instruction to execute (required for agentic reminders)"
    )
    deal_id: UUID | None = Field(None, description="Optional deal scope for agentic reminders")
    trigger_time: datetime = Field(..., description="When to fire, ISO 8601 with timezone")
    timezone: str = Field(..., max_length=50, description="IANA timezone, e.g. 'Asia/Kolkata'")
    recurrence: ReminderRecurrence = Field(
        default=ReminderRecurrence.NONE, description="Recurrence pattern"
    )
    recurrence_end_at: datetime | None = Field(
        None, description="Stop recurring after this UTC datetime"
    )
    delivery_channel: str = Field(..., description="Where to deliver: 'email', 'teams', or 'slack'")

    @model_validator(mode="after")
    def validate_type_content(self) -> ReminderCreateRequest:
        if self.reminder_type == ReminderType.STATIC:
            if not self.message_content:
                msg = "message_content is required for static reminders"
                raise ValueError(msg)
        elif self.reminder_type == ReminderType.AGENTIC and not self.agent_prompt:
            msg = "agent_prompt is required for agentic reminders"
            raise ValueError(msg)
        if self.delivery_channel not in VALID_DELIVERY_CHANNELS:
            msg = f"delivery_channel must be one of: {', '.join(sorted(VALID_DELIVERY_CHANNELS))}"
            raise ValueError(msg)
        return self


class ReminderUpdateRequest(BaseModel):
    """Request model for updating a reminder. All fields optional."""

    title: str | None = Field(None, max_length=255)
    message_content: str | None = None
    agent_prompt: str | None = None
    deal_id: UUID | None = None
    trigger_time: datetime | None = None
    timezone: str | None = Field(None, max_length=50)
    recurrence: ReminderRecurrence | None = None
    recurrence_end_at: datetime | None = None
    delivery_channel: str | None = None

    @model_validator(mode="after")
    def validate_channel(self) -> ReminderUpdateRequest:
        if self.delivery_channel and self.delivery_channel not in VALID_DELIVERY_CHANNELS:
            msg = f"delivery_channel must be one of: {', '.join(sorted(VALID_DELIVERY_CHANNELS))}"
            raise ValueError(msg)
        return self


class ReminderListItem(BaseModel):
    """Response model for a reminder in list view."""

    reminder_id: UUID = Field(description="Unique identifier for the reminder.")
    title: str = Field(description="Short user-facing label for the reminder.")
    reminder_type: ReminderType = Field(description="Whether the reminder is static or agentic.")
    status: ReminderStatus = Field(description="Current lifecycle state of the reminder.")
    next_trigger_at: datetime = Field(description="Next scheduled trigger in UTC.")
    next_trigger_local_at: datetime = Field(
        description="Next scheduled trigger converted to the user's timezone."
    )
    schedule_summary: str = Field(
        description="Human-friendly schedule label for cards and list rows."
    )
    timezone: str = Field(description="IANA timezone used for schedule calculations.")
    recurrence: ReminderRecurrence = Field(description="Recurrence cadence for the reminder.")
    delivery_channel: str = Field(description="Channel the reminder will be delivered to.")
    is_running: bool = Field(
        default=False,
        description="True while an execution attempt is currently in progress.",
    )
    current_run_status: ReminderRunStatus | None = Field(
        default=None,
        description="Current execution status when a run is active.",
    )
    can_pause: bool = Field(
        default=False,
        description="Whether the UI should allow a pause action right now.",
    )
    can_cancel: bool = Field(
        default=False,
        description="Whether the UI should allow a cancel action right now.",
    )
    can_delete: bool = Field(
        default=False,
        description="Whether the UI should allow a delete action right now.",
    )
    last_triggered_at: datetime | None = Field(
        default=None,
        description="Timestamp of the last successful execution.",
    )
    trigger_count: int = Field(
        default=0,
        description="Number of successful executions.",
    )
    created_at: datetime = Field(description="When the reminder was created.")

    model_config = {"from_attributes": True}


class ReminderDetail(BaseModel):
    """Full reminder detail response."""

    reminder_id: UUID = Field(description="Unique identifier for the reminder.")
    title: str = Field(description="Short user-facing label for the reminder.")
    reminder_type: ReminderType = Field(description="Whether the reminder is static or agentic.")
    status: ReminderStatus = Field(description="Current lifecycle state of the reminder.")

    # Content
    message_content: str | None = Field(
        default=None,
        description="Static reminder message content, when applicable.",
    )
    agent_prompt: str | None = Field(
        default=None,
        description="Agent prompt used for agentic reminders, when applicable.",
    )
    deal_id: UUID | None = Field(
        default=None,
        description="Optional deal scope for agentic reminders.",
    )

    # Schedule
    next_trigger_at: datetime = Field(description="Next scheduled trigger in UTC.")
    next_trigger_local_at: datetime = Field(
        description="Next scheduled trigger converted to the user's timezone."
    )
    schedule_summary: str = Field(description="Human-friendly schedule label for detail views.")
    timezone: str = Field(description="IANA timezone used for schedule calculations.")
    recurrence: ReminderRecurrence = Field(description="Recurrence cadence for the reminder.")
    recurrence_end_at: datetime | None = Field(
        default=None,
        description="Last allowed recurrence time in UTC, if set.",
    )

    # Delivery
    delivery_channel: str = Field(description="Channel the reminder will be delivered to.")

    # Tracking
    is_running: bool = Field(
        default=False,
        description="True while an execution attempt is currently in progress.",
    )
    current_run_status: ReminderRunStatus | None = Field(
        default=None,
        description="Current execution status when a run is active.",
    )
    can_pause: bool = Field(
        default=False,
        description="Whether the UI should allow a pause action right now.",
    )
    can_cancel: bool = Field(
        default=False,
        description="Whether the UI should allow a cancel action right now.",
    )
    can_delete: bool = Field(
        default=False,
        description="Whether the UI should allow a delete action right now.",
    )
    last_triggered_at: datetime | None = Field(
        default=None,
        description="Timestamp of the last successful execution.",
    )
    trigger_count: int = Field(
        default=0,
        description="Number of successful executions.",
    )
    consecutive_failures: int = Field(
        default=0,
        description="Number of back-to-back failed attempts since the last success.",
    )
    last_error: str | None = Field(
        default=None,
        description="Most recent execution error message, if any.",
    )

    # Timestamps
    created_at: datetime = Field(description="When the reminder was created.")
    updated_at: datetime = Field(description="When the reminder was last updated.")

    model_config = {"from_attributes": True}


class ReminderRunListItem(BaseModel):
    """Single execution history row for a reminder."""

    run_id: UUID = Field(description="Unique identifier for this execution attempt.")
    attempt_number: int = Field(description="Sequential attempt number for this reminder.")
    status: ReminderRunStatus = Field(description="Outcome of this execution attempt.")
    triggered_for_at: datetime = Field(description="Scheduled time this run was executing for.")
    started_at: datetime = Field(description="When the execution attempt started.")
    completed_at: datetime | None = Field(
        default=None,
        description="When the execution attempt finished, if completed.",
    )
    input_snapshot: str = Field(
        description="Exact reminder input executed for this run: static message or agent prompt."
    )
    result_text: str | None = Field(
        default=None,
        description="Human-readable output produced by the run, if any.",
    )
    error_message: str | None = Field(
        default=None,
        description="Failure reason captured for this run, if any.",
    )


class ReminderHistoryResponse(BaseModel):
    """Paginated reminder execution history."""

    items: list[ReminderRunListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class ReminderListResponse(BaseModel):
    """Paginated list of reminders."""

    items: list[ReminderListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class ReminderListQueryParams(BaseModel):
    """Query parameters for listing reminders."""

    limit: int = Query(20, ge=1, le=100, description="Items per page")
    page: int = Query(1, ge=1, description="Page number (1-indexed)")
    status: str | None = Query(
        None, description="Comma-separated status filter (active,paused,completed)"
    )
    reminder_type: ReminderType | None = Query(None, description="Filter by static or agentic")
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort by next_trigger_at")


class ReminderHistoryQueryParams(BaseModel):
    """Query parameters for reminder execution history."""

    limit: int = Query(20, ge=1, le=100, description="Items per page")
    page: int = Query(1, ge=1, description="Page number (1-indexed)")
