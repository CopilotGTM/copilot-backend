"""Deal Timeline API response models for timeline events."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field


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


class EmailEventData(BaseModel):
    """Email-specific data for timeline events."""

    # Source ID for API calls (e.g., fetching full email details)
    email_id: str | None = Field(
        None, description="Internal UUID of the email record for API calls"
    )

    # Provider UI metadata (derived from TimelineEvent.event_source)
    provider_logo_url: str | None = None

    # --- List Preview (always present) ---
    subject: str | None = None
    snippet: str | None = None
    from_email: str | None = None

    # --- Detail Enrichment (populated on detail fetch) ---
    from_name: str | None = None
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    thread_id: str | None = None

    # Body content
    body_text: str | None = None
    body_html: str | None = None
    body_truncated: bool | None = None
    body_length: int | None = None

    # Attachments
    has_attachments: bool | None = None
    attachments: list[dict[str, Any]] | None = None  # [{name, size, mime_type}]

    # Timing
    received_at: datetime | None = None
    sent_at: datetime | None = None
    direction: str | None = None  # INBOUND/OUTBOUND


class MeetingSummary(BaseModel):
    """Meeting summary block with status and data."""

    status: str | None = Field(None, description="PENDING, PROCESSING, COMPLETED, FAILED")
    data: dict[str, Any] | None = None


class MeetingEventData(BaseModel):
    """Meeting-specific data for timeline events."""

    # Source ID for API calls (e.g., summary generation, prep generation)
    calendar_event_id: str | None = Field(
        None, description="Internal UUID of the calendar event for API calls"
    )

    # Provider UI metadata (derived from TimelineEvent.event_source)
    provider_logo_url: str | None = None

    # --- List Preview (always present) ---
    title: str | None = None
    duration_minutes: int | None = None
    attendee_count: int | None = None

    # --- Detail Enrichment (populated on detail fetch) ---
    attendees: list[str] | None = None
    meeting_summary: MeetingSummary | None = None


class StageChangeEventData(BaseModel):
    """Stage change event data for timeline events."""

    # Provider UI metadata
    provider_logo_url: str | None = None

    # Stage transition - internal IDs (UUIDs)
    old_stage_id: str | None = None  # Internal UUID for previous stage
    new_stage_id: str | None = None  # Internal UUID for new stage

    # Display labels for frontend (human-readable names)
    old_stage_label: str | None = None  # Display label for old stage
    new_stage_label: str | None = None  # Display label for new stage

    # Legacy: external IDs (provider's identifiers) for backwards compatibility
    old_stage: str | None = None  # Previous stage external ID
    new_stage: str | None = None  # New stage external ID

    # Deal context (included for frontend convenience)
    deal_name: str | None = None
    company_name: str | None = None
    amount: float | None = None
    currency: str | None = None
    pipeline_name: str | None = None


class DealCreatedEventData(BaseModel):
    """Deal created event data for timeline events."""

    # Provider UI metadata
    provider_logo_url: str | None = None

    # Deal information
    deal_name: str | None = None
    initial_stage: str | None = None
    pipeline_name: str | None = None
    amount: float | None = None
    currency: str | None = None

    # Company information
    company_name: str | None = None
    company_domain: str | None = None

    # Owner information
    owner_name: str | None = None

    # Primary contact
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None


class TimelineEvent(BaseModel):
    """Unified timeline event model for both list and detail views.

    List API returns preview data in event_data.
    Detail API returns same structure with enriched event_data.
    """

    id: UUID = Field(..., description="Timeline event ID")
    deal_id: UUID
    event_type: str = Field(..., description="EMAIL or MEETING")
    event_source: str = Field(..., description="EMAIL_GMAIL, CALENDAR_GOOGLE, etc.")
    event_timestamp: datetime
    event_data: dict[str, Any] = Field(
        ...,
        description="Type-specific data; shape varies by event_type (EmailEventData or MeetingEventData)",
    )
    excluded_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class TimelinePage(BaseModel):
    """Paginated list of timeline events."""

    deal_id: UUID
    items: list[TimelineEvent]
    total_count: int
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")
