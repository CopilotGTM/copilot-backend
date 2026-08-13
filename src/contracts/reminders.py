"""Shared reminder contracts for API, service, agent, and worker layers."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderRunStatus,
    ReminderStatus,
    ReminderType,
)

VALID_DELIVERY_CHANNELS = {"teams", "slack", "whatsapp"}


class StaticReminderCreateRequest(BaseModel):
    """Request model for creating a static reminder from the user API."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., max_length=255, description="Short label for the reminder")
    message_content: str = Field(..., description="Text to deliver when the reminder fires")
    trigger_time: datetime = Field(..., description="When to fire, ISO 8601 with timezone")
    timezone: str = Field(..., max_length=50, description="IANA timezone, e.g. 'Asia/Kolkata'")
    recurrence: ReminderRecurrence = Field(
        default=ReminderRecurrence.NONE, description="Recurrence pattern"
    )
    recurrence_end_at: datetime | None = Field(
        None, description="Stop recurring after this UTC datetime"
    )
    delivery_channel: str = Field(
        ..., description="Where to deliver: 'teams', 'slack', or 'whatsapp'"
    )

    @model_validator(mode="after")
    def validate_channel(self) -> StaticReminderCreateRequest:
        """Reject delivery channels that the reminder dispatcher cannot route."""
        if self.delivery_channel not in VALID_DELIVERY_CHANNELS:
            msg = f"delivery_channel must be one of: {', '.join(sorted(VALID_DELIVERY_CHANNELS))}"
            raise ValueError(msg)
        return self


class StaticReminderUpdateRequest(BaseModel):
    """Request model for updating static reminder fields from the user API."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, max_length=255)
    message_content: str | None = None
    trigger_time: datetime | None = None
    timezone: str | None = Field(None, max_length=50)
    recurrence: ReminderRecurrence | None = None
    recurrence_end_at: datetime | None = None
    delivery_channel: str | None = None

    @model_validator(mode="after")
    def validate_channel(self) -> StaticReminderUpdateRequest:
        """Reject unsupported delivery channels when an update supplies one."""
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
    consecutive_failures: int = Field(
        default=0,
        description="Number of back-to-back failed attempts since the last success.",
    )
    last_error: str | None = Field(
        default=None,
        description="Most recent execution error message, if any.",
    )
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
