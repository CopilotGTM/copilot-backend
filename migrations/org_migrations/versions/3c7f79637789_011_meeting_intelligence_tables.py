"""011 meeting intelligence tables

Revision ID: 3c7f79637789
Revises: 3c7f79637790
Create Date: 2025-11-30 12:00:00.000000

Creates meeting intelligence tables for storing transcripts from any provider:
- meetings: Transcript record linked to calendar_events
- transcript_segments: Individual utterances with speaker attribution

These tables are source-agnostic and work with Teams, Zoom, Google Meet,
Gong, native bot recording, or manual uploads.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637789"
down_revision: Union[str, None] = "3c7f79637790"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Constants
NOW_DEFAULT = sa.text("now()")
EMPTY_JSONB_DEFAULT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    """Create meeting intelligence tables."""

    # ==========================================================================
    # meetings table - Transcript record linked to CalendarEvent
    # ==========================================================================
    op.create_table(
        "meetings",
        # Primary key
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Organisation
        sa.Column(
            "organisation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
        ),
        # Link to calendar event (required)
        sa.Column(
            "calendar_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calendar_events.event_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Source and status
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="teams_transcript",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("status_message", sa.Text, nullable=True),
        # Transcript metadata
        sa.Column(
            "transcript_fetched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("transcript_language", sa.String(16), nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        # Full transcript text (concatenated for LLM)
        sa.Column("full_transcript", sa.Text, nullable=True),
        # Source-specific metadata
        sa.Column(
            "source_data",
            postgresql.JSONB,
            nullable=False,
            server_default=EMPTY_JSONB_DEFAULT,
        ),
        # Soft delete
        sa.Column(
            "is_deleted",
            sa.Boolean,
            nullable=False,
            server_default="false",
        ),
        # Timestamps (from TimestampMixin)
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=NOW_DEFAULT,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=NOW_DEFAULT,
        ),
    )

    # Unique constraint: one meeting record per calendar event
    op.create_unique_constraint(
        "uq_meeting_calendar_event",
        "meetings",
        ["calendar_event_id"],
    )

    # Index for status filtering
    op.create_index(
        "ix_meeting_org_status",
        "meetings",
        ["organisation_id", "status"],
    )

    # ==========================================================================
    # transcript_segments table - Individual utterances with speaker attribution
    # ==========================================================================
    op.create_table(
        "transcript_segments",
        # Primary key
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Link to meeting
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Speaker identity
        sa.Column(
            "attendee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calendar_event_attendees.attendee_id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("speaker_external_id", sa.String(255), nullable=True),
        sa.Column("speaker_name", sa.String(255), nullable=True),
        # Ordering and timing
        sa.Column("segment_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("start_time_seconds", sa.Float, nullable=True),
        sa.Column("end_time_seconds", sa.Float, nullable=True),
        # Content
        sa.Column("content", sa.Text, nullable=False),
        # Optional metadata
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        # Timestamps (from TimestampMixin)
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=NOW_DEFAULT,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=NOW_DEFAULT,
        ),
    )

    # Index for segment ordering within a meeting
    op.create_index(
        "ix_transcript_segment_meeting",
        "transcript_segments",
        ["meeting_id", "segment_index"],
    )


def downgrade() -> None:
    """Drop meeting intelligence tables."""
    op.drop_table("transcript_segments")
    op.drop_table("meetings")
