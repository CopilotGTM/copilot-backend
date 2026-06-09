"""
Meeting Intelligence Models.

Source-agnostic schema for storing meeting transcripts from ANY provider:
- Microsoft Teams (Graph API)
- Zoom (Cloud Recording API)
- Google Meet
- Gong (Revenue Intelligence API)
- Native bot (our own recording)
- Manual upload

Design Philosophy:
- CalendarEvent = meeting metadata (title, time, attendees, online_meeting_id)
- CalendarAttendee = who was invited/attended
- CalendarEventDealAssociation = deal link + AI insights (summary, action items in JSONB)
- Meeting (NEW) = transcript processing status + full_transcript text
- TranscriptSegment (NEW) = individual utterances with speaker attribution

Provider-specific data goes in:
- Meeting.source_data (JSONB) - raw API response, provider metadata
- TranscriptSegment.speaker_external_id - provider's speaker ID
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from ..base_mixins import TimestampMixin
from .calendar import GenerationStatus

# =============================================================================
# Enums
# =============================================================================


class MeetingSource(str, Enum):
    """Source of meeting transcript data. Schema works for ALL providers."""

    # Native calendar integrations
    TEAMS_TRANSCRIPT = "teams_transcript"  # MS Graph API transcript
    ZOOM_RECORDING = "zoom_recording"  # Zoom Cloud Recording API
    GOOGLE_MEET = "google_meet"  # Google Meet

    # Revenue Intelligence tools
    GONG = "gong"  # Gong API
    CHORUS = "chorus"  # Chorus.ai (ZoomInfo)

    # Custom sources
    NATIVE_BOT = "native_bot"  # Our bot recorded and transcribed
    MANUAL_UPLOAD = "manual_upload"  # User uploaded transcript


class MeetingStatus(str, Enum):
    """Meeting transcript processing status."""

    PENDING = "pending"  # Meeting ended, waiting for transcript
    FETCHING = "fetching"  # Transcript fetch in progress
    PROCESSING = "processing"  # Transcript received, being processed
    READY = "ready"  # Fully processed
    FAILED = "failed"  # Processing failed
    NOT_AVAILABLE = "not_available"  # No transcript available for this meeting


# =============================================================================
# Models
# =============================================================================


class Meeting(TimestampMixin, SQLModel, table=True):
    """
    Meeting transcript record.

    Extends CalendarEvent with transcript data. Links to CalendarEvent
    via calendar_event_id for meeting metadata (title, attendees, deal links).

    Storage Strategy:
    -----------------
    1. `full_transcript` (Text) - Concatenated plain text for LLM summarization.
       Built as: "Speaker1: text\\nSpeaker2: text\\n..."
       Used for: Quick access, search, passing to AI for summary generation.

    2. `TranscriptSegment` rows - Individual utterances with speaker attribution.
       Used for: Speaker analytics, timeline scrubbing, "who said what" queries.

    Why both?
    - LLMs need full text (no joins needed for 50+ segments)
    - Analytics need structured data (talk time per speaker, segment search)
    """

    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("calendar_event_id", name="uq_meeting_calendar_event"),
        Index("ix_meeting_org_status", "organisation_id", "status"),
    )

    meeting_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)

    # Link to calendar event (REQUIRED - this is the source of truth)
    calendar_event_id: UUID = Field(
        foreign_key="calendar_events.event_id",
        index=True,
        description="Calendar event this meeting transcript belongs to.",
    )

    # Transcript source and status
    source: MeetingSource = Field(
        default=MeetingSource.TEAMS_TRANSCRIPT,
        sa_column=Column(String(32), nullable=False, server_default=text("'teams_transcript'")),
    )
    status: MeetingStatus = Field(
        default=MeetingStatus.PENDING,
        sa_column=Column(String(32), nullable=False, server_default=text("'pending'")),
    )
    status_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Error message if status is FAILED.",
    )

    # Transcript metadata
    transcript_fetched_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    transcript_language: str | None = Field(
        default=None,
        sa_column=Column(String(16), nullable=True),
        description="BCP-47 language code.",
    )
    duration_seconds: int | None = Field(
        default=None,
        description="Meeting duration from transcript.",
    )

    # Full transcript text (concatenated for easy access)
    full_transcript: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Full transcript text for summarization.",
    )
    compressed_transcript: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Cached compressed transcript for future agent context optimization.",
    )
    compressed_transcript_generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When compressed_transcript was last generated.",
    )
    compressed_transcript_source_hash: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Hash of the full_transcript used to generate the cached compression.",
    )

    # Deal-linked summaries remain on CalendarEventDealAssociation. These fields
    # store generated content for meetings that have a transcript but no deal link.
    meeting_summary_status: GenerationStatus = Field(
        default=GenerationStatus.NOT_STARTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
        description="Generation status for meeting-level post-meeting summary content.",
    )
    meeting_summary_data: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Generated post-meeting summary content for meetings without a deal link.",
    )
    meeting_summary_generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When meeting-level summary content was last generated.",
    )

    # Source-specific metadata
    source_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )

    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
    )

    # Transcript fetch tracking for scheduled task retry logic
    transcript_fetch_attempts: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
        description="Number of transcript fetch attempts. Used by scheduled task to limit retries.",
    )

    # Relationships - Note: lazy="selectin" prevents N+1 queries
    # Use quotes for forward reference since TranscriptSegment is defined after Meeting
    transcript_segments: list["TranscriptSegment"] = Relationship(
        back_populates="meeting",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "lazy": "selectin",
        },
    )


class TranscriptSegment(TimestampMixin, SQLModel, table=True):
    """
    Transcript text segment with speaker attribution.

    Each segment represents one utterance from the transcript.
    Example from Teams API metadataContent:
    {
        "entries": [
            {
                "text": "Hello everyone, let's get started",
                "speaker": {"id": "abc123", "displayName": "John Smith"},
                "language": "en-US"
            },
            ...
        ]
    }

    We store each entry as a TranscriptSegment row.
    The full_transcript in Meeting table is the concatenated text for LLM summarization.
    """

    __tablename__ = "transcript_segments"
    __table_args__ = (Index("ix_transcript_segment_meeting", "meeting_id", "segment_index"),)

    segment_id: UUID = Field(default_factory=uuid4, primary_key=True)
    meeting_id: UUID = Field(
        foreign_key="meetings.meeting_id",
        index=True,
    )

    # Speaker identity - try to match to CalendarAttendee
    attendee_id: UUID | None = Field(
        default=None,
        foreign_key="calendar_event_attendees.attendee_id",
        index=True,
        description="Matched calendar attendee (if found by email).",
    )
    # External speaker ID from source platform (e.g., Teams user ID)
    speaker_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Speaker ID from source platform.",
    )
    # Display name from transcript (always available)
    speaker_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Speaker display name from transcript.",
    )

    # Ordering and timing
    segment_index: int = Field(default=0)
    start_time_seconds: float | None = Field(default=None)
    end_time_seconds: float | None = Field(default=None)

    # Content
    content: str = Field(sa_column=Column(Text, nullable=False))

    # Optional metadata
    confidence: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 4), nullable=True),
    )

    # Relationships - Use quotes for back-reference
    meeting: Meeting = Relationship(back_populates="transcript_segments")


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "MeetingSource",
    "MeetingStatus",
    "Meeting",
    "TranscriptSegment",
]
