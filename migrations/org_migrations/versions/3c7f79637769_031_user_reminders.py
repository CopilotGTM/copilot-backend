"""031 add user_reminders table for scheduled reminders and agentic alerts.

Revision ID: 3c7f79637769
Revises: 3c7f79637770
Create Date: 2026-02-22 12:00:00.000000

This migration adds:
1. user_reminders table for time-triggered notifications (static) and AI tasks (agentic)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637769"
down_revision: str | None = "3c7f79637770"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create user_reminders table."""
    op.create_table(
        "user_reminders",
        # Identity
        sa.Column(
            "reminder_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False, index=True),
        sa.Column("user_id", sa.UUID(), nullable=False, index=True),
        # Type & content
        sa.Column("reminder_type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message_content", sa.Text(), nullable=True),
        sa.Column(
            "agent_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # Schedule
        sa.Column("next_trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_timezone", sa.String(50), nullable=False, server_default="UTC"),
        sa.Column("recurrence", sa.String(20), nullable=False, server_default="none"),
        sa.Column("recurrence_end_at", sa.DateTime(timezone=True), nullable=True),
        # Delivery
        sa.Column("delivery_channel", sa.String(20), nullable=False, server_default="teams"),
        sa.Column(
            "delivery_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Status & tracking
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        # Timestamps (from TimestampMixin)
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Composite indexes for common query patterns
    op.create_index(
        "ix_user_reminders_org_status",
        "user_reminders",
        ["organisation_id", "status"],
    )
    op.create_index(
        "ix_user_reminders_next_trigger",
        "user_reminders",
        ["next_trigger_at", "status"],
    )
    op.create_index(
        "ix_user_reminders_user_status",
        "user_reminders",
        ["user_id", "status"],
    )


def downgrade() -> None:
    """Drop user_reminders table and its indexes."""
    op.drop_index("ix_user_reminders_user_status", table_name="user_reminders")
    op.drop_index("ix_user_reminders_next_trigger", table_name="user_reminders")
    op.drop_index("ix_user_reminders_org_status", table_name="user_reminders")
    op.drop_table("user_reminders")
