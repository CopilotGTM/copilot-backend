"""Pydantic schemas for Reminder API endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field, model_validator
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
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

    reminder_id: UUID
    title: str
    reminder_type: ReminderType
    status: ReminderStatus
    next_trigger_at: datetime
    timezone: str
    recurrence: ReminderRecurrence
    delivery_channel: str
    last_triggered_at: datetime | None = None
    trigger_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class ReminderDetail(BaseModel):
    """Full reminder detail response."""

    reminder_id: UUID
    title: str
    reminder_type: ReminderType
    status: ReminderStatus

    # Content
    message_content: str | None = None
    agent_prompt: str | None = None
    deal_id: UUID | None = None

    # Schedule
    next_trigger_at: datetime
    timezone: str
    recurrence: ReminderRecurrence
    recurrence_end_at: datetime | None = None

    # Delivery
    delivery_channel: str

    # Tracking
    last_triggered_at: datetime | None = None
    trigger_count: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None

    # Timestamps
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
