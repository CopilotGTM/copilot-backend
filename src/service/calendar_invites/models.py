"""DTOs and errors for user-scoped calendar invite creation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, field_validator

EMAIL_ADAPTER = TypeAdapter(EmailStr)
# Provider requests support large recipient lists, but the resolver must enforce
# this ceiling before performing one lookup per user-supplied attendee.
MAX_CALENDAR_INVITE_ATTENDEES = 100


class CalendarInviteError(Exception):
    """Base error surfaced by calendar invite services."""

    # Exception carries machine-readable error code for API and tool responses.

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CalendarInviteAttendee(BaseModel):
    """Invite attendee with normalized email and optional metadata."""

    email: str = Field(max_length=320)
    name: str | None = Field(default=None, max_length=255)
    optional: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        """Normalize and validate attendee email syntax for provider acceptance."""
        return str(EMAIL_ADAPTER.validate_python(value)).strip().lower()


class CalendarInviteCreateRequest(BaseModel):
    """Service-facing request contract for creating a calendar invite."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    organisation_id: UUID
    requested_by_user_id: UUID
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    timezone: str = Field(min_length=1, max_length=128)
    attendees: list[CalendarInviteAttendee] = Field(
        min_length=1,
        max_length=MAX_CALENDAR_INVITE_ATTENDEES,
    )
    body: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=500)
    deal_id: UUID | None = None
    sender_email: str | None = Field(default=None, max_length=320)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("title", "timezone", mode="before")
    @classmethod
    def clean_required_string(cls, value: str) -> str:
        """Normalize required string fields and reject blanks from chat payloads."""
        # Normalize values that arrive from chat/text input and reject blanks
        # so downstream DB + Graph payloads always have non-empty fields.
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("value is required")
        return cleaned

    @field_validator("start_at", "end_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        """Reject naive datetimes so downstream event scheduling is timezone-safe."""
        # Keep UTC-normalized datetime everywhere to avoid timezone mismatch
        # between service logic, payload construction, and Graph response checks.
        if value.tzinfo is None:
            raise ValueError("datetime must include timezone")
        return value.astimezone(UTC)

    @field_validator("end_at")
    @classmethod
    def require_end_after_start(cls, value: datetime, info) -> datetime:
        """Require a strictly positive invite duration before validation passes."""
        # Enforce temporal ordering at validation time so the service does not
        # persist impossible windows or call Graph with negative durations.
        start_at = info.data.get("start_at")
        if isinstance(start_at, datetime) and value <= start_at:
            raise ValueError("end_at must be after start_at")
        return value


class CalendarInviteCreateResult(BaseModel):
    """Result returned after successful invite creation and sync."""

    request_id: UUID | None = None
    event_id: UUID | None = None
    provider_event_id: str
    subject: str
    conference_link: str | None = None
    web_link: str | None = None


class CalendarMeetingUpdateRequest(BaseModel):
    """Complete desired state for one organizer-owned, non-recurring meeting."""

    organisation_id: UUID
    requested_by_user_id: UUID
    event_id: UUID
    expected_version: str | None = None
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    timezone: str = Field(min_length=1, max_length=128)
    attendees: list[CalendarInviteAttendee] = Field(
        default_factory=list,
        max_length=MAX_CALENDAR_INVITE_ATTENDEES,
    )
    body: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=500)
    body_changed: bool = Field(default=False, exclude=True)
    provider_body: str | None = Field(default=None, exclude=True)
    provider_body_content_type: str | None = Field(default=None, exclude=True)
    previous_body: str | None = Field(default=None, exclude=True)
    provider_etag: str | None = Field(default=None, exclude=True)

    _clean_required_string = field_validator("title", "timezone", mode="before")(
        CalendarInviteCreateRequest.clean_required_string.__func__
    )
    _require_aware_datetime = field_validator("start_at", "end_at")(
        CalendarInviteCreateRequest.require_aware_datetime.__func__
    )

    @field_validator("end_at")
    @classmethod
    def require_end_after_start(cls, value: datetime, info) -> datetime:
        """Reject meeting updates whose requested end does not follow the start."""
        start_at = info.data.get("start_at")
        if start_at and value <= start_at:
            raise ValueError("end_at must be after start_at")
        return value


class CalendarMeetingMutationResult(BaseModel):
    """Result of updating or cancelling one provider calendar event."""

    request_id: UUID | None = None
    event_id: UUID
    provider_event_id: str
    status: str
    provider_response: dict | None = None
