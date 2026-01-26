"""Deal timeline models for unified activity tracking across deals."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ..base_mixins import utcnow

EMPTY_JSONB_ARRAY = "'[]'::jsonb"
EMPTY_JSONB_SQL = "'{}'::jsonb"


class TimelineEventType(str, Enum):
    """Types of events that appear on the deal timeline."""

    # Communication Events
    EMAIL = "EMAIL"  # Email sent/received
    MEETING = "MEETING"  # Calendar meeting
    MESSAGE = "MESSAGE"  # Generic message (Slack/Teams/etc.)

    # Deal Lifecycle Events
    DEAL_CREATED = "DEAL_CREATED"
    STAGE_CHANGE = "STAGE_CHANGE"

    # Activity Events (future)
    TASK = "TASK"
    NOTE = "NOTE"
    CALL = "CALL"

    # System Events - Risk/Blocker
    BLOCKER_ADDED = "BLOCKER_ADDED"
    BLOCKER_RESOLVED = "BLOCKER_RESOLVED"
    BLOCKER_DISMISSED = "BLOCKER_DISMISSED"
    RISK_FLAGGED = "RISK_FLAGGED"

    # System Events - Stakeholder
    STAKEHOLDER_ADDED = "STAKEHOLDER_ADDED"
    STAKEHOLDER_REMOVED = "STAKEHOLDER_REMOVED"


class TimelineEventSource(str, Enum):
    """Source system that generated the event."""

    # Email sources
    EMAIL_GMAIL = "EMAIL_GMAIL"
    EMAIL_OUTLOOK = "EMAIL_OUTLOOK"

    # Calendar sources
    CALENDAR_GOOGLE = "CALENDAR_GOOGLE"
    CALENDAR_OUTLOOK = "CALENDAR_OUTLOOK"

    # Messaging sources
    SLACK = "SLACK"
    TEAMS = "TEAMS"

    # CRM sources
    CRM_HUBSPOT = "CRM_HUBSPOT"
    CRM_SALESFORCE = "CRM_SALESFORCE"

    # System
    SYSTEM = "SYSTEM"


class DealTimelineEvent(SQLModel, table=True):
    """
    Unified timeline index for all deal-related activities.

    This is a denormalized table optimized for fast chronological retrieval.
    Stores minimal data for timeline card rendering; full details fetched on demand.

    The API routes to the correct source storage using event_type + event_source.
    """

    __tablename__ = "deal_timeline_events"
    __table_args__ = (
        # Primary query pattern: all events for a deal, sorted by time
        Index("ix_timeline_deal_time", "deal_id", "event_timestamp"),
        # Filter by type within a deal
        Index("ix_timeline_deal_type_time", "deal_id", "event_type", "event_timestamp"),
        # Org-wide queries (admin dashboards)
        Index("ix_timeline_org_time", "organisation_id", "event_timestamp"),
        # Prevent duplicate entries for same source record
        UniqueConstraint(
            "deal_id", "event_type", "source_record_id", name="uq_timeline_event_source"
        ),
    )

    # Primary Key
    timeline_event_id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Organisation & Deal Context
    organisation_id: UUID = Field(index=True)
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="The deal this event belongs to.",
    )

    # Event Classification
    event_type: TimelineEventType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of timeline event (EMAIL, MEETING, MESSAGE, STAGE_CHANGE, etc.).",
    )
    event_source: TimelineEventSource = Field(
        sa_column=Column(String(32), nullable=False),
        description="Source system (EMAIL_GMAIL, CALENDAR_GOOGLE, SLACK, etc.).",
    )

    # Temporal Ordering (THE KEY FIELD FOR TIMELINE)
    event_timestamp: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the event occurred - used for chronological sorting.",
    )

    # Source Record Reference (for fetching full details)
    source_record_id: str = Field(
        sa_column=Column(String(512), nullable=False),
        description="ID of the source record (association_id, history_id, or external_id).",
    )

    # Timeline Card Preview Data (denormalized for fast rendering)
    title: str = Field(
        sa_column=Column(String(500), nullable=False),
        description="Display title for timeline card (e.g., email subject, meeting title).",
    )
    subtitle: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Secondary text (e.g., 'Stage: Discovery → Evaluation').",
    )

    # Direction/Status Indicators
    direction: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="INBOUND/OUTBOUND/INTERNAL for emails; null for other types.",
    )
    status: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Event status (e.g., SUGGESTED/CONFIRMED for associations).",
    )

    # Soft exclusion (hide from timeline without deleting)
    excluded_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=(
            "If set, this event is excluded from the default timeline view. "
            "Used when an item was previously indexed but later rejected/cancelled."
        ),
    )

    # Participants Preview (for timeline card)
    participant_names: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of participant names for preview (max 3-4 names).",
    )
    participant_count: int = Field(
        default=0,
        description="Total participant count (for '+N more' display).",
    )

    # Optional Metadata
    event_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional type-specific metadata for rendering.",
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


__all__ = [
    "TimelineEventType",
    "TimelineEventSource",
    "DealTimelineEvent",
]
