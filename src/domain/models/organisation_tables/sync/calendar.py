from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel
from src.domain.models.organisation_tables.sync.base import SyncedRecordMixin


class CalendarEvent(SyncedRecordMixin, table=True):
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_id", name="uq_calendar_event_external"),
        Index("ix_calendar_event_org_start", "organisation_id", "start_at"),
        Index("ix_calendar_event_owner", "organiser_email"),
    )

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
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
    timezone: str | None = Field(default=None)
    attendee_count: int = Field(default=0)

    attendees: list["CalendarAttendee"] = Relationship(back_populates="event")


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
