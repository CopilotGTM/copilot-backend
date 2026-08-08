"""Extend generic notification deliveries for agent output email.

Revision ID: 3c7f79637711
Revises: 3c7f79637712
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637711"
down_revision: str | None = "3c7f79637712"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add generic claim fencing without replacing existing notification data."""

    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM agent_runs WHERE status = 'delivery_failed')")
        )
        .scalar()
    ):
        raise RuntimeError("Refusing to remove delivery_failed while agent runs use that status")

    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'validation_failed', 'cancelled')",
    )
    op.drop_constraint(
        "ck_notification_delivery_status",
        "notification_deliveries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_notification_delivery_status",
        "notification_deliveries",
        "status IN ('PENDING','SENDING','SENT','FAILED','SKIPPED','NEEDS_ATTENTION')",
    )
    for column in (
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("provider_resource_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
    ):
        op.add_column(
            "notification_deliveries",
            column,
        )
    op.create_index(
        "ix_notification_delivery_recovery",
        "notification_deliveries",
        ["organisation_id", "status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_delivery_stale_claim",
        "notification_deliveries",
        ["organisation_id", "status", "claimed_at"],
    )


def downgrade() -> None:
    """Remove claim fencing only before agent output notification data exists."""

    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM notification_deliveries "
                "WHERE notification_type = 'agent_output_ready' OR status = 'NEEDS_ATTENTION')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Refusing to remove active agent output notification state")

    op.drop_index("ix_notification_delivery_stale_claim", table_name="notification_deliveries")
    op.drop_index("ix_notification_delivery_recovery", table_name="notification_deliveries")
    for column in (
        "error_code",
        "provider_resource_id",
        "claim_token",
        "claimed_by",
        "claimed_at",
        "next_attempt_at",
    ):
        op.drop_column("notification_deliveries", column)
    op.drop_constraint(
        "ck_notification_delivery_status",
        "notification_deliveries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_notification_delivery_status",
        "notification_deliveries",
        "status IN ('PENDING','SENDING','SENT','FAILED','SKIPPED')",
    )
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'delivery_failed', "
        "'validation_failed', 'cancelled')",
    )
