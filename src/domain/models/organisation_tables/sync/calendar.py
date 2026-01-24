from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.base import FlexibleSyncedRecordMixin

if TYPE_CHECKING:
    from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal


class AssociationStatus(str, Enum):
    """Status for deal-communication associations (calendar/email).

    Simplified model - no approval workflow needed:
    - ACTIVE: Matched and visible in timeline (default)
    - REMOVED: User removed it from timeline
    - CANCELLED: External event - meeting was cancelled
    """

    ACTIVE = "ACTIVE"  # Matched and visible (default)
    REMOVED = "REMOVED"  # User removed from timeline
    CANCELLED = "CANCELLED"  # External event: meeting was cancelled


class GenerationStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CalendarEvent(FlexibleSyncedRecordMixin, table=True):
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("organisation_id", "external_id", name="uq_calendar_event_external"),
        Index("ix_calendar_event_org_start", "organisation_id", "start_at"),
        Index("ix_calendar_event_owner", "organiser_email"),
    )

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB))
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    organiser_email: str | None = Field(default=None, index=True)
    organiser_name: str | None = Field(default=None)
    calendar_external_id: str | None = Field(default=None, index=True)
    title: str | None = Field(default=None)
    description: str | None = Field(default=None)
    location: str | None = Field(default=None)
    start_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    end_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    is_all_day: bool = Field(default=False)
    status: str | None = Field(default=None)
    conference_link: str | None = Field(default=None)
    online_meeting_id: str | None = Field(default=None, index=True)
    online_meeting_provider: str | None = Field(default=None)
    timezone: str | None = Field(default=None)
    attendee_count: int = Field(default=0)

    attendees: list["CalendarAttendee"] = Relationship(back_populates="event")
    user_states: list["CalendarEventUserState"] = Relationship(back_populates="event")
    deal_associations: list["CalendarEventDealAssociation"] = Relationship(back_populates="event")


class CalendarEventUserState(SQLModel, table=True):
    """
    Tracks which integrations (users) have synced each calendar event.
    Used for cleanup when a user disconnects their integration.
    """

    __tablename__ = "calendar_event_user_states"
    __table_args__ = (
        UniqueConstraint("event_id", "integration_id", name="uq_calendar_event_user_state"),
        Index("ix_calendar_user_state_integration", "integration_id"),
    )

    state_id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="calendar_events.event_id", index=True)
    integration_id: UUID = Field(
        foreign_key="knowledge_integrations.integration_id",
        index=True,
        description="The user's calendar integration that synced this event.",
    )
    external_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider's event ID for this specific user's calendar.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Whether this event was deleted from this user's calendar.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    event: "CalendarEvent" = Relationship(back_populates="user_states")


class CalendarAttendee(SQLModel, table=True):
    __tablename__ = "calendar_event_attendees"
    __table_args__ = (
        Index("ix_calendar_attendee_email", "email"),
        CheckConstraint(
            "response_status IN ('accepted','declined','tentative','needsAction','none')",
            name="ck_calendar_attendee_response",
        ),
        UniqueConstraint("event_id", "email", name="uq_calendar_attendee_event"),
    )

    attendee_id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: UUID = Field(foreign_key="calendar_events.event_id", index=True)
    email: str = Field(sa_column=Column(String(255), nullable=False))
    display_name: str | None = Field(default=None)
    response_status: str = Field(default="none")
    is_organiser: bool = Field(default=False)
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    event: "CalendarEvent" = Relationship(back_populates="attendees")


class CalendarEventDealAssociation(SQLModel, table=True):
    __tablename__ = "calendar_event_deal_associations"
    __table_args__ = (
        UniqueConstraint("event_id", "deal_id", name="uq_calendar_event_deal"),
        Index("ix_calendar_assoc_org_status", "organisation_id", "status"),
        Index("ix_calendar_assoc_deal", "deal_id"),
    )

    association_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    event_id: UUID = Field(foreign_key="calendar_events.event_id")
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id")

    # Enum columns stored as VARCHAR for flexible schema evolution
    status: AssociationStatus = Field(
        default=AssociationStatus.ACTIVE,
        sa_column=Column(String(32), nullable=False, server_default=text("'ACTIVE'")),
    )
    match_score: float = Field(default=0.0)

    # Meeting Prep (formerly Briefing)
    meeting_prep_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
    )
    meeting_prep_data: dict | None = Field(default=None, sa_column=Column(JSONB))

    # Meeting Summary (Post-meeting)
    meeting_summary_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
    )
    meeting_summary_data: dict | None = Field(default=None, sa_column=Column(JSONB))

    # Meeting Follow-up (Post-meeting actions)
    meeting_followup_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
    )
    meeting_followup_data: dict | None = Field(default=None, sa_column=Column(JSONB))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    event: "CalendarEvent" = Relationship(back_populates="deal_associations")
    deal: "CRMDeal" = Relationship()
