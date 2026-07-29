"""API contracts for the top-level Meetings tab."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from src.service.calendar_invites.models import CalendarInviteAttendee


class MeetingsView(StrEnum):
    """Supported top-level Meetings tab list views."""

    TODAY = "today"
    UPCOMING = "upcoming"
    PAST = "past"
    RECORDINGS = "recordings"


class MeetingLinkedRecordType(StrEnum):
    """Linked CRM record types supported in Meetings V1."""

    DEAL = "DEAL"


class MeetingAttendeeItem(BaseModel):
    """Display-ready attendee information for list rows and drawers."""

    email: str
    name: str | None = None
    response_status: str | None = None
    is_organiser: bool = False


class MeetingLinkedRecord(BaseModel):
    """Compact linked CRM record display payload."""

    record_type: MeetingLinkedRecordType
    record_id: UUID
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeetingBotStatus(BaseModel):
    """Computed bot state for one calendar event."""

    status: str
    display_status: str | None = None
    reason: str | None = None
    is_external: bool = False
    provider_session_id: str | None = None
    can_invite: bool = False
    can_remove: bool = False


class MeetingListItem(BaseModel):
    """Meeting row returned by the top-level Meetings tab."""

    event_id: UUID
    title: str | None
    start_at: datetime
    end_at: datetime
    is_all_day: bool = False
    duration_minutes: int | None = None
    platform: str | None = None
    platform_label: str = "Not available"
    conference_link: str | None = None
    status: str | None = None
    attendees: list[MeetingAttendeeItem] = Field(default_factory=list)
    attendee_count: int = 0
    linked_record: MeetingLinkedRecord | None = None
    bot: MeetingBotStatus
    recording_status: str | None = None
    recording_availability: str = "Not available"
    transcript_status: str | None = None
    has_recording: bool = False
    has_summary: bool = False
    meeting_summary_status: str | None = None
    meeting_summary_data: dict[str, Any] | None = None
    meeting_summary_generated_at: datetime | None = None
    has_prep: bool = False
    next_action: str | None = None


class MeetingsPage(BaseModel):
    """Paginated Meetings tab list response."""

    items: list[MeetingListItem]
    total_count: int
    page: int
    limit: int


class MeetingDetail(MeetingListItem):
    """Drawer payload for one meeting."""

    description: str | None = None
    organiser_name: str | None = None
    organiser_email: str | None = None
    linked_record_detail: dict[str, Any] | None = None
    suggested_actions: list[dict[str, Any]] = Field(default_factory=list)


class LinkMeetingRequest(BaseModel):
    """Request to set the active CRM link for one calendar event."""

    record_type: MeetingLinkedRecordType
    record_id: UUID


class CreateMeetingRequest(BaseModel):
    """REST wrapper payload for creating a provider-backed calendar meeting."""

    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    timezone: str = Field(min_length=1, max_length=128)
    attendees: list[CalendarInviteAttendee] = Field(default_factory=list, max_length=100)
    body: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=500)
    sender_email: str | None = Field(default=None, max_length=320)
    idempotency_key: str | None = Field(default=None, max_length=128)
    link: LinkMeetingRequest | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetime must include timezone")
        return value

    @model_validator(mode="after")
    def _require_positive_duration(self) -> CreateMeetingRequest:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class CreateMeetingResponse(BaseModel):
    """Result returned after a calendar invite creates a synced event."""

    event_id: UUID
    provider_event_id: str
    conference_link: str | None = None
    meeting: MeetingDetail
    bot_dispatch: dict[str, Any] | None = None


class MeetingTranscriptSegment(BaseModel):
    """Compact transcript segment exposed by recording detail."""

    start_time: float | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    text: str


class MeetingRecordingResponse(BaseModel):
    """Recall-bot recording detail for the recording intelligence page."""

    event_id: UUID
    recording_status: str | None = None
    transcript_status: str | None = None
    playback_url: str | None = None
    download_url: str | None = None
    video_blob_path: str | None = None
    video_blob_url: str | None = None
    video_content_type: str | None = None
    video_size_bytes: int | None = None
    video_filename: str | None = None
    provider_session_id: str | None = None
    transcript_artifact: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    transcript_segments: list[MeetingTranscriptSegment] = Field(default_factory=list)
