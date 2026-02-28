"""032 add user_reminder_runs table for reminder execution history.

Revision ID: 3c7f79637768
Revises: 3c7f79637769
Create Date: 2026-02-28 18:45:00.000000

This migration adds:
1. user_reminder_runs table for per-attempt reminder execution history
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637768"
down_revision: str | None = "3c7f79637769"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create user_reminder_runs table."""
    op.create_table(
        "user_reminder_runs",
        sa.Column(
            "run_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("reminder_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("triggered_for_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="in_progress"),
        sa.Column("input_snapshot", sa.Text(), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["reminder_id"], ["user_reminders.reminder_id"]),
    )

    op.create_index(
        "ix_user_reminder_runs_org_started",
        "user_reminder_runs",
        ["organisation_id", "started_at"],
    )
    op.create_index(
        "ix_user_reminder_runs_reminder_started",
        "user_reminder_runs",
        ["reminder_id", "started_at"],
    )
    op.create_index(
        "ix_user_reminder_runs_user_started",
        "user_reminder_runs",
        ["user_id", "started_at"],
    )


def downgrade() -> None:
    """Drop user_reminder_runs table and its indexes."""
    op.drop_index("ix_user_reminder_runs_user_started", table_name="user_reminder_runs")
    op.drop_index("ix_user_reminder_runs_reminder_started", table_name="user_reminder_runs")
    op.drop_index("ix_user_reminder_runs_org_started", table_name="user_reminder_runs")
    op.drop_table("user_reminder_runs")
