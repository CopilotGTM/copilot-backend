"""Meeting-level CRM link and bot preference models.

These tables support the top-level Meetings tab without replacing the existing
deal-specific calendar association table. Deal links are mirrored there for
legacy deal jobs; lead/contact links stay isolated to this V1 meeting surface.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, Index, String, text
from sqlmodel import Field, SQLModel

from ..base_mixins import TimestampMixin


class MeetingRecordLinkType(StrEnum):
    """CRM record types supported by Meetings V1."""

    DEAL = "DEAL"
    LEAD = "LEAD"
    CONTACT = "CONTACT"


class MeetingBotPreference(StrEnum):
    """Per-meeting bot attendance override."""

    AUTO_INVITE = "AUTO_INVITE"
    ASK_BEFORE = "ASK_BEFORE"
    DO_NOT_INVITE = "DO_NOT_INVITE"


class MeetingRecordLink(TimestampMixin, SQLModel, table=True):
    """Active CRM record link for one calendar event in the Meetings tab.

    The partial unique index in the migration enforces one active link per
    org/event while preserving inactive rows as a lightweight audit trail.
    """

    __tablename__ = "meeting_record_links"
    __table_args__ = (
        Index("ix_meeting_record_links_org_event", "organisation_id", "calendar_event_id"),
        Index("ix_meeting_record_links_org_record", "organisation_id", "record_type", "record_id"),
    )

    link_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    calendar_event_id: UUID = Field(foreign_key="calendar_events.event_id", index=True)
    record_type: MeetingRecordLinkType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Meetings V1 CRM record type: DEAL, LEAD, or CONTACT.",
    )
    record_id: UUID = Field(index=True)
    linked_by_user_id: UUID | None = Field(default=None, index=True)
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )


class MeetingBotPreferenceOverride(TimestampMixin, SQLModel, table=True):
    """User-scoped bot preference override for one calendar event."""

    __tablename__ = "meeting_bot_preferences"
    __table_args__ = (
        Index(
            "ix_meeting_bot_preferences_org_event",
            "organisation_id",
            "calendar_event_id",
        ),
    )

    preference_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    calendar_event_id: UUID = Field(foreign_key="calendar_events.event_id", index=True)
    user_id: UUID = Field(index=True)
    preference: MeetingBotPreference = Field(sa_column=Column(String(32), nullable=False))


__all__ = [
    "MeetingBotPreference",
    "MeetingBotPreferenceOverride",
    "MeetingRecordLink",
    "MeetingRecordLinkType",
]
