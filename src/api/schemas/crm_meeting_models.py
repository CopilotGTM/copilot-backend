"""Response models for the top-level CRM meetings API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CRMMeetingDealSummary(BaseModel):
    """Deal enrichment for a user-owned calendar meeting."""

    deal_id: UUID
    name: str | None = None
    company_name: str | None = None
    amount: float | None = None
    currency: str | None = None
    stage_name: str | None = None
    stage_label: str | None = None

    model_config = {"from_attributes": True}


class CRMMeetingRecordingState(BaseModel):
    """Safe recording-bot state exposed to clients."""

    session_id: UUID | None = None
    status: str = "NOT_REQUESTED"
    display_status: str = "not_requested"
    transcript_status: str | None = None
    provider: str | None = None
    provider_status: str | None = None
    join_at: datetime | None = None
    sent_to_provider_at: datetime | None = None


class CRMMeetingListItem(BaseModel):
    """One meeting in the authenticated user's CRM meeting list."""

    event_id: UUID
    title: str | None = None
    start_at: datetime
    end_at: datetime
    status: str | None = None
    location: str | None = None
    conference_link: str | None = None
    online_meeting_provider: str | None = None
    organiser_name: str | None = None
    organiser_email: str | None = None
    attendee_count: int = 0
    linked_deals: list[CRMMeetingDealSummary] = Field(default_factory=list)
    recording: CRMMeetingRecordingState
    can_invite_bot: bool
    can_remove_bot: bool


class CRMMeetingsPage(BaseModel):
    """Paginated response for top-level CRM meetings."""

    items: list[CRMMeetingListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int
