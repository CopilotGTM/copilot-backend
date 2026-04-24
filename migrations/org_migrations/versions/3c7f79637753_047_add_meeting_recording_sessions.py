"""047 add meeting recording sessions

Adds the provider-agnostic session table used by Recall today. The table stores
one row per org/event/provider and is shaped so future meeting-bot providers can
reuse the same lifecycle columns.

Revision ID: 3c7f79637753
Revises: 3c7f79637754
Create Date: 2026-04-23 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637753"
down_revision: Union[str, None] = "3c7f79637754"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create meeting_recording_sessions table."""
    op.create_table(
        "meeting_recording_sessions",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_event_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), server_default=sa.text("'recall_ai'"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("meeting_url", sa.Text(), nullable=False),
        sa.Column("join_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'PENDING_SEND'"), nullable=False),
        sa.Column(
            "transcript_status",
            sa.String(length=32),
            server_default=sa.text("'NOT_REQUESTED'"),
            nullable=False,
        ),
        sa.Column("provider_session_id", sa.String(length=255), nullable=True),
        sa.Column("provider_status", sa.String(length=128), nullable=True),
        sa.Column("sent_to_provider_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transcript_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "provider_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["calendar_event_id"], ["calendar_events.event_id"]),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint(
            "organisation_id",
            "calendar_event_id",
            "provider",
            name="uq_meeting_recording_org_event_provider",
        ),
        sa.UniqueConstraint("provider_session_id", name="uq_meeting_recording_provider_session"),
        sa.UniqueConstraint("dedupe_key", name="uq_meeting_recording_dedupe_key"),
    )
    op.create_index(
        "ix_meeting_recording_org_status",
        "meeting_recording_sessions",
        ["organisation_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_recording_org_join_at",
        "meeting_recording_sessions",
        ["organisation_id", "join_at"],
        unique=False,
    )
    # The hourly fallback scan filters by org + unsent state + join time.
    op.create_index(
        "ix_meeting_recording_unsent_lookup",
        "meeting_recording_sessions",
        ["organisation_id", "sent_to_provider_at", "join_at"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_recording_sessions_calendar_event_id",
        "meeting_recording_sessions",
        ["calendar_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_recording_sessions_created_at",
        "meeting_recording_sessions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_recording_sessions_organisation_id",
        "meeting_recording_sessions",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_recording_sessions_updated_at",
        "meeting_recording_sessions",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop meeting_recording_sessions table."""
    op.drop_index("ix_meeting_recording_sessions_updated_at", table_name="meeting_recording_sessions")
    op.drop_index("ix_meeting_recording_sessions_organisation_id", table_name="meeting_recording_sessions")
    op.drop_index("ix_meeting_recording_sessions_created_at", table_name="meeting_recording_sessions")
    op.drop_index("ix_meeting_recording_sessions_calendar_event_id", table_name="meeting_recording_sessions")
    op.drop_index("ix_meeting_recording_unsent_lookup", table_name="meeting_recording_sessions")
    op.drop_index("ix_meeting_recording_org_join_at", table_name="meeting_recording_sessions")
    op.drop_index("ix_meeting_recording_org_status", table_name="meeting_recording_sessions")
    op.drop_table("meeting_recording_sessions")
