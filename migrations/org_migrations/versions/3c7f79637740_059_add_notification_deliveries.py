"""Add notification deliveries and meeting-level summary fields.

This migration introduces the durable notification ledger used by meeting
summary notifications and the meeting-level summary columns used when a Recall
transcript is not linked to a deal. Both changes roll out together because the
worker code can enqueue non-deal summaries and deal-linked notifications in the
same deploy.

Revision ID: 3c7f79637740
Revises: 3c7f79637741
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637740"
down_revision: str | None = "3c7f79637741"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add summary/cache meeting fields and the notification delivery ledger."""
    # Meeting summaries used to be stored only on deal associations. These fields
    # give non-deal meetings their own durable generated-content slot.
    op.add_column(
        "meetings",
        sa.Column(
            "meeting_summary_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'NOT_STARTED'"),
        ),
    )
    op.add_column(
        "meetings",
        sa.Column(
            "meeting_summary_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "meetings",
        sa.Column("meeting_summary_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "meetings",
        sa.Column("compressed_transcript", sa.Text(), nullable=True),
    )
    op.add_column(
        "meetings",
        sa.Column("compressed_transcript_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "meetings",
        sa.Column("compressed_transcript_source_hash", sa.String(length=64), nullable=True),
    )

    # One row per notification target lets workers claim delivery idempotently
    # across retries and across future channels without creating per-feature tables.
    op.create_table(
        "notification_deliveries",
        sa.Column(
            "delivery_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("target_key", sa.String(length=255), nullable=False),
        sa.Column("target_label", sa.String(length=320), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "delivery_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "channel IN ('email','slack','teams','whatsapp','google_chat')",
            name="ck_notification_delivery_channel",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED','SKIPPED')",
            name="ck_notification_delivery_status",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "notification_type",
            "source_type",
            "source_id",
            "channel",
            "target_key",
            name="uq_notification_delivery_target",
        ),
    )
    op.create_index(
        "ix_notification_delivery_org_status",
        "notification_deliveries",
        ["organisation_id", "status"],
    )
    op.create_index(
        "ix_notification_delivery_source",
        "notification_deliveries",
        ["source_type", "source_id"],
    )
    op.create_index(
        "ix_notification_delivery_type_channel",
        "notification_deliveries",
        ["notification_type", "channel"],
    )


def downgrade() -> None:
    """Remove notification delivery and meeting summary/cache fields."""
    # Drop the delivery ledger before the meeting columns so rollback removes the
    # dependent notification feature before deleting summary storage.
    op.drop_index("ix_notification_delivery_type_channel", table_name="notification_deliveries")
    op.drop_index("ix_notification_delivery_source", table_name="notification_deliveries")
    op.drop_index("ix_notification_delivery_org_status", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_column("meetings", "compressed_transcript_source_hash")
    op.drop_column("meetings", "compressed_transcript_generated_at")
    op.drop_column("meetings", "compressed_transcript")
    op.drop_column("meetings", "meeting_summary_generated_at")
    op.drop_column("meetings", "meeting_summary_data")
    op.drop_column("meetings", "meeting_summary_status")
