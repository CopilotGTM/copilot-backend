"""Persistent audit model for user-owned calendar invite requests.

Every requested invite is recorded here before sync/write attempts can reach
Microsoft Graph so the API can show request history, status transitions, and
failure diagnostics per org + user.
"""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from src.domain.models.organisation_tables.base_mixins import utcnow


class CalendarInviteRequestStatus(str, Enum):
    """Invite lifecycle states for the audit table.

    PENDING exists only while metadata is prepared, SENDING means Graph call has
    started, SENT confirms a provider event id was returned, and FAILED stores the
    last user-visible error code/message pair.
    """

    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class CalendarInviteRequest(SQLModel, table=True):
    """Stored audit record for one outbound calendar invite attempt."""

    __tablename__ = "calendar_invite_requests"
    __table_args__ = (
        # Enforce idempotency per org/user pair so retries cannot persist multiple
        # audit rows for one intended invite request.
        UniqueConstraint(
            "organisation_id",
            "requested_by_user_id",
            "idempotency_key",
            name="uq_calendar_invite_request_idempotency",
        ),
        # Provider requires start/end ordering guarantees; enforce at write time so
        # malformed payloads do not create unusable calendar_events later.
        CheckConstraint("end_at > start_at", name="ck_calendar_invite_end_after_start"),
        # Limit statuses to the invite state machine used by the service layer.
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED')",
            name="ck_calendar_invite_status",
        ),
        Index("ix_calendar_invite_org_status", "organisation_id", "status"),
    )

    request_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    requested_by_user_id: UUID = Field(index=True)
    integration_id: UUID = Field(foreign_key="knowledge_integrations.integration_id", index=True)
    deal_id: UUID | None = Field(default=None, foreign_key="crm_deals.deal_id", index=True)
    event_id: UUID | None = Field(default=None, foreign_key="calendar_events.event_id", index=True)
    provider_event_id: str | None = Field(default=None, sa_column=Column(String(255), index=True))
    idempotency_key: str = Field(sa_column=Column(String(128), nullable=False))
    status: CalendarInviteRequestStatus = Field(
        default=CalendarInviteRequestStatus.PENDING,
        sa_column=Column(String(32), nullable=False, server_default=text("'PENDING'")),
    )
    title: str = Field(sa_column=Column(String(500), nullable=False))
    body: str | None = Field(default=None, sa_column=Column(Text))
    location: str | None = Field(default=None, sa_column=Column(String(500)))
    start_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    end_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    timezone: str = Field(sa_column=Column(String(128), nullable=False))
    provider_timezone: str = Field(sa_column=Column(String(128), nullable=False))
    attendees: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    conference_link: str | None = Field(default=None, sa_column=Column(Text))
    provider_response: dict | None = Field(default=None, sa_column=Column(JSONB))
    error_code: str | None = Field(default=None, sa_column=Column(String(128)))
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
