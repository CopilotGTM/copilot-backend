"""Add the durable activation outbox for event-triggered agents.

Revision ID: 3c7f79637719
Revises: 3c7f79637720
Create Date: 2026-07-22

Source-domain rows such as CRM mutations remain immutable audit history. This
child table owns retry and terminal activation state so one source occurrence
can be matched asynchronously without putting worker lifecycle on that history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637719"
down_revision: str | None = "3c7f79637720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create replay-safe event dispatch rows and their pending recovery path."""

    op.create_table(
        "automation_event_dispatches",
        sa.Column(
            "dispatch_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("source_domain", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_automation_event_dispatch_attempts",
        ),
        sa.CheckConstraint(
            "NOT (processed_at IS NOT NULL AND failed_at IS NOT NULL)",
            name="ck_automation_event_dispatch_terminal_state",
        ),
        sa.PrimaryKeyConstraint(
            "dispatch_id",
            name="pk_automation_event_dispatches",
        ),
    )
    op.create_index(
        "uq_automation_event_dispatch_source",
        "automation_event_dispatches",
        ["organisation_id", "source_domain", "source_event_id", "event_type"],
        unique=True,
    )
    op.create_index(
        "ix_automation_event_dispatch_pending",
        "automation_event_dispatches",
        ["created_at", "dispatch_id"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL AND failed_at IS NULL"),
    )


def downgrade() -> None:
    """Remove activation delivery state without changing source event history."""

    op.drop_index(
        "ix_automation_event_dispatch_pending",
        table_name="automation_event_dispatches",
    )
    op.drop_index(
        "uq_automation_event_dispatch_source",
        table_name="automation_event_dispatches",
    )
    op.drop_table("automation_event_dispatches")
