"""Add calendar invite request audit table.

Revision ID: 3c7f79637747
Revises: 3c7f79637748
Create Date: 2026-05-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637747"
down_revision: str | None = "3c7f79637748"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the calendar invite request audit table and indexes.

    This table is written before outbound provider calls, so operators always have
    a persisted attempt row even when Graph returns an error mid-flight.
    """
    # Build the write audit schema before any provider call can depend on it.
    # Without this table, failed invites lose durable status and error context.
    op.create_table(
        "calendar_invite_requests",
        sa.Column(
            "request_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=True),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("provider_timezone", sa.String(length=128), nullable=False),
        sa.Column(
            "attendees",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("conference_link", sa.Text(), nullable=True),
        sa.Column("provider_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
        sa.CheckConstraint("end_at > start_at", name="ck_calendar_invite_end_after_start"),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED')",
            name="ck_calendar_invite_status",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "requested_by_user_id",
            "idempotency_key",
            name="uq_calendar_invite_request_idempotency",
        ),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"]),
        sa.ForeignKeyConstraint(["event_id"], ["calendar_events.event_id"]),
    )

    op.create_index(
        "ix_calendar_invite_requests_organisation_id",
        "calendar_invite_requests",
        ["organisation_id"],
    )
    op.create_index(
        "ix_calendar_invite_requests_requested_by_user_id",
        "calendar_invite_requests",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_calendar_invite_requests_integration_id",
        "calendar_invite_requests",
        ["integration_id"],
    )
    op.create_index(
        "ix_calendar_invite_requests_deal_id",
        "calendar_invite_requests",
        ["deal_id"],
    )
    op.create_index(
        "ix_calendar_invite_requests_event_id",
        "calendar_invite_requests",
        ["event_id"],
    )
    op.create_index(
        "ix_calendar_invite_requests_provider_event_id",
        "calendar_invite_requests",
        ["provider_event_id"],
    )
    op.create_index(
        "ix_calendar_invite_org_status",
        "calendar_invite_requests",
        ["organisation_id", "status"],
    )


def downgrade() -> None:
    """Drop the calendar invite request table and indexes in reverse order."""
    # Release supporting indexes first so PostgreSQL can drop table objects
    # cleanly without transient dependency errors.
    op.drop_index("ix_calendar_invite_org_status", table_name="calendar_invite_requests")
    op.drop_index(
        "ix_calendar_invite_requests_provider_event_id",
        table_name="calendar_invite_requests",
    )
    op.drop_index("ix_calendar_invite_requests_event_id", table_name="calendar_invite_requests")
    op.drop_index("ix_calendar_invite_requests_deal_id", table_name="calendar_invite_requests")
    op.drop_index(
        "ix_calendar_invite_requests_integration_id",
        table_name="calendar_invite_requests",
    )
    op.drop_index(
        "ix_calendar_invite_requests_requested_by_user_id",
        table_name="calendar_invite_requests",
    )
    op.drop_index(
        "ix_calendar_invite_requests_organisation_id",
        table_name="calendar_invite_requests",
    )
    op.drop_table("calendar_invite_requests")
