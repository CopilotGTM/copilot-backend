"""Add deal timeline events table.

Revision ID: 3c7f79637780
Revises: 3c7f79637781
Create Date: 2025-01-09 12:00:00.000000

"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637780"
down_revision: str | None = "3c7f79637781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create deal_timeline_events table
    op.create_table(
        "deal_timeline_events",
        sa.Column(
            "timeline_event_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("event_source", sa.String(32), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_record_id", sa.String(512), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("subtitle", sa.String(500), nullable=True),
        sa.Column("direction", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "participant_names",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("participant_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "event_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.ForeignKeyConstraint(
            ["deal_id"],
            ["crm_deals.deal_id"],
            name="fk_timeline_deal",
            ondelete="CASCADE",
        ),
    )

    # Create indexes
    op.create_index(
        "ix_timeline_deal_time",
        "deal_timeline_events",
        ["deal_id", "event_timestamp"],
    )
    op.create_index(
        "ix_timeline_deal_type_time",
        "deal_timeline_events",
        ["deal_id", "event_type", "event_timestamp"],
    )
    op.create_index(
        "ix_timeline_org_time",
        "deal_timeline_events",
        ["organisation_id", "event_timestamp"],
    )
    op.create_index(
        "ix_timeline_org_id",
        "deal_timeline_events",
        ["organisation_id"],
    )

    # Unique constraint to prevent duplicate source records per deal/type
    op.create_unique_constraint(
        "uq_timeline_event_source",
        "deal_timeline_events",
        ["deal_id", "event_type", "source_record_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_timeline_event_source", "deal_timeline_events", type_="unique")
    op.drop_index("ix_timeline_org_id", table_name="deal_timeline_events")
    op.drop_index("ix_timeline_org_time", table_name="deal_timeline_events")
    op.drop_index("ix_timeline_deal_type_time", table_name="deal_timeline_events")
    op.drop_index("ix_timeline_deal_time", table_name="deal_timeline_events")
    op.drop_table("deal_timeline_events")
