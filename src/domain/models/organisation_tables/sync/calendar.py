from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.base import OrgScopedRecordMixin

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


class CalendarEvent(OrgScopedRecordMixin, table=True):
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint(
            "external_id", "occurrence_anchor", name="uq_calendar_event_external_occurrence"
        ),
        Index("ix_calendar_event_org_start", "organisation_id", "start_at"),
        Index("ix_calendar_event_owner", "organiser_email"),
        Index("ix_calendar_event_occurrence_anchor", "occurrence_anchor"),
    )

    event_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the canonical calendar event row.",
    )
    external_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Shared global calendar UID (Google iCalUID or Microsoft iCalUId).",
    )
    provider: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Provider family that most recently supplied the canonical event payload.",
    )
    occurrence_anchor: str = Field(
        default="",
        sa_column=Column(String(64), nullable=False, server_default=text("''")),
        description="Recurring-occurrence discriminator. Empty for single events; normalized original start anchor for recurring instances.",
    )
    original_start_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Provider-supplied original occurrence start timestamp used to derive the occurrence anchor.",
    )
    data_payload: dict | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider event payload retained for diagnostics and future backfills.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this canonical row was last synchronized from any provider payload.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Provider-reported last modified timestamp for change detection.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last time this event was observed during sync, even if no material changes were written.",
    )
    organiser_email: str | None = Field(
        default=None,
        index=True,
        description="Organizer email address extracted from the provider event.",
    )
    organiser_name: str | None = Field(
        default=None,
        description="Organizer display name extracted from the provider event.",
    )
    calendar_external_id: str | None = Field(
        default=None,
        index=True,
        description="Provider-specific calendar identifier containing the event.",
    )
    title: str | None = Field(
        default=None,
        description="Human-readable meeting title or subject.",
    )
    description: str | None = Field(
        default=None,
        description="Meeting body/notes/description text from the provider.",
    )
    location: str | None = Field(
        default=None,
        description="Physical or virtual location label supplied by the provider.",
    )
    start_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Current scheduled start time for the meeting occurrence.",
    )
    end_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Current scheduled end time for the meeting occurrence.",
    )
    is_all_day: bool = Field(
        default=False,
        description="Whether the event is an all-day occurrence.",
    )
    status: str | None = Field(
        default=None,
        description="Normalized event status such as confirmed or cancelled.",
    )
    conference_link: str | None = Field(
        default=None,
        description="Join URL for the online meeting, if present.",
    )
    online_meeting_id: str | None = Field(
        default=None,
        index=True,
        description="Provider-specific online meeting identifier used for transcript or meeting APIs.",
    )
    online_meeting_provider: str | None = Field(
        default=None,
        description="Normalized online meeting platform name, for example Google Meet or Teams.",
    )
    timezone: str | None = Field(
        default=None,
        description="Provider-reported timezone for the event timestamps.",
    )
    attendee_count: int = Field(
        default=0,
        description="Count of attendees included in the latest synced payload.",
    )

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
        Index("ix_calendar_user_state_integration_external", "integration_id", "external_id"),
    )

    state_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the per-integration calendar user-state row.",
    )
    event_id: UUID = Field(
        foreign_key="calendar_events.event_id",
        index=True,
        description="Canonical calendar event this per-user state belongs to.",
    )
    integration_id: UUID = Field(
        foreign_key="knowledge_integrations.integration_id",
        index=True,
        description="Calendar integration that synced this mailbox-specific copy.",
    )
    external_id: str = Field(
        sa_column=Column(String(1024), nullable=False),
        description="Mailbox-specific provider event ID for this integration/user.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Whether this event copy has been deleted from this integration's mailbox.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this user-state row was first created.",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this user-state row was last updated.",
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

    attendee_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the attendee row.",
    )
    event_id: UUID = Field(
        foreign_key="calendar_events.event_id",
        index=True,
        description="Canonical calendar event this attendee belongs to.",
    )
    email: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Attendee email address used for matching and display.",
    )
    display_name: str | None = Field(
        default=None,
        description="Attendee display name from the provider payload.",
    )
    response_status: str = Field(
        default="none",
        description="Normalized RSVP status for the attendee.",
    )
    is_organiser: bool = Field(
        default=False,
        description="Whether this attendee row represents the organizer.",
    )
    data_payload: dict | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Raw attendee payload from the provider for audit and troubleshooting.",
    )

    event: "CalendarEvent" = Relationship(back_populates="attendees")


class CalendarEventDealAssociation(SQLModel, table=True):
    __tablename__ = "calendar_event_deal_associations"
    __table_args__ = (
        UniqueConstraint("event_id", "deal_id", name="uq_calendar_event_deal"),
        Index("ix_calendar_assoc_org_status", "organisation_id", "status"),
        Index("ix_calendar_assoc_deal", "deal_id"),
    )

    association_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the calendar-event-to-deal association.",
    )
    organisation_id: UUID = Field(
        index=True,
        description="Organisation that owns this event-to-deal association.",
    )
    event_id: UUID = Field(
        foreign_key="calendar_events.event_id",
        description="Canonical calendar event linked to the deal.",
    )
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        description="CRM deal matched to the calendar event.",
    )

    status: AssociationStatus = Field(
        default=AssociationStatus.ACTIVE,
        sa_column=Column(String(32), nullable=False, server_default=text("'ACTIVE'")),
        description="Lifecycle status of the event-to-deal association.",
    )
    match_score: float = Field(
        default=0.0,
        description="Confidence score for the event-to-deal match.",
    )

    meeting_prep_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
        description="Generation status for meeting prep content.",
    )
    meeting_prep_data: dict | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Generated meeting prep content for this deal association.",
    )

    meeting_summary_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
        description="Generation status for post-meeting summary content.",
    )
    meeting_summary_data: dict | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Generated post-meeting summary content for this deal association.",
    )

    meeting_followup_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
        description="Generation status for follow-up content after the meeting.",
    )
    meeting_followup_data: dict | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Generated follow-up recommendations or actions for this deal association.",
    )

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this event-to-deal association was first created.",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this event-to-deal association was last updated.",
    )

    event: "CalendarEvent" = Relationship(back_populates="deal_associations")
    deal: "CRMDeal" = Relationship()
