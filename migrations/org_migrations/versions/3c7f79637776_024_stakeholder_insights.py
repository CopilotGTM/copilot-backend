"""Add deal_stakeholder_insights table and stakeholder columns.

Revision ID: 3c7f79637776
Revises: 3c7f79637777
Create Date: 2025-01-06 12:00:00.000000

This migration adds:
1. deal_stakeholder_insights table for AI-generated stakeholder recommendations
2. stakeholder_data JSONB column to deal_stakeholders for general extended data
3. stakeholder_engagement_history JSONB column for time-series engagement tracking
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637776"
down_revision: str | None = "3c7f79637777"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add stakeholder_data column to deal_stakeholders for general extended data
    op.add_column(
        "deal_stakeholders",
        sa.Column(
            "stakeholder_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # Add stakeholder_engagement_history column for time-series tracking
    op.add_column(
        "deal_stakeholders",
        sa.Column(
            "stakeholder_engagement_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # Create deal_stakeholder_insights table
    op.create_table(
        "deal_stakeholder_insights",
        # Identity
        sa.Column(
            "insight_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Context
        sa.Column("organisation_id", sa.UUID(), nullable=False, index=True),
        sa.Column("deal_id", sa.UUID(), nullable=False, index=True),
        sa.Column("stakeholder_id", sa.UUID(), nullable=True, index=True),
        # Classification
        sa.Column("insight_type", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
        ),
        sa.Column(
            "priority",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'MEDIUM'"),
        ),
        # Content
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # AI Metadata
        sa.Column("ai_reasoning", sa.Text(), nullable=False),
        sa.Column(
            "ai_confidence",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.8"),
        ),
        # Actions (JSONB array)
        sa.Column(
            "actions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Source Attribution
        sa.Column("source_type", sa.String(64), nullable=True),
        sa.Column("source_record_id", sa.String(512), nullable=True),
        sa.Column("related_risk_blocker_id", sa.UUID(), nullable=True),
        # Stakeholder Context (denormalized)
        sa.Column("stakeholder_name", sa.String(255), nullable=True),
        sa.Column("stakeholder_title", sa.String(255), nullable=True),
        sa.Column("stakeholder_email", sa.String(255), nullable=True),
        # Metrics
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("metric_unit", sa.String(32), nullable=True),
        sa.Column("threshold_value", sa.Float(), nullable=True),
        sa.Column("trend_direction", sa.String(16), nullable=True),
        # Timestamps
        sa.Column(
            "identified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        # Extended Data
        sa.Column(
            "insight_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Foreign keys
        sa.ForeignKeyConstraint(
            ["deal_id"],
            ["crm_deals.deal_id"],
            name="fk_stakeholder_insight_deal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stakeholder_id"],
            ["deal_stakeholders.stakeholder_id"],
            name="fk_stakeholder_insight_stakeholder",
            ondelete="SET NULL",
        ),
    )

    # Create indexes for common query patterns
    # Primary query: All insights for a deal by status
    op.create_index(
        "ix_stakeholder_insight_deal_status",
        "deal_stakeholder_insights",
        ["deal_id", "status"],
    )

    # Filter by stakeholder
    op.create_index(
        "ix_stakeholder_insight_stakeholder",
        "deal_stakeholder_insights",
        ["stakeholder_id", "status"],
    )

    # Org-wide queries (dashboards)
    op.create_index(
        "ix_stakeholder_insight_org_status",
        "deal_stakeholder_insights",
        ["organisation_id", "status"],
    )

    # Type-based filtering
    op.create_index(
        "ix_stakeholder_insight_type",
        "deal_stakeholder_insights",
        ["organisation_id", "insight_type"],
    )

    # Priority-based filtering
    op.create_index(
        "ix_stakeholder_insight_priority",
        "deal_stakeholder_insights",
        ["organisation_id", "priority"],
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_stakeholder_insight_priority", table_name="deal_stakeholder_insights")
    op.drop_index("ix_stakeholder_insight_type", table_name="deal_stakeholder_insights")
    op.drop_index("ix_stakeholder_insight_org_status", table_name="deal_stakeholder_insights")
    op.drop_index("ix_stakeholder_insight_stakeholder", table_name="deal_stakeholder_insights")
    op.drop_index("ix_stakeholder_insight_deal_status", table_name="deal_stakeholder_insights")

    # Drop table
    op.drop_table("deal_stakeholder_insights")

    # Drop stakeholder columns
    op.drop_column("deal_stakeholders", "stakeholder_engagement_history")
    op.drop_column("deal_stakeholders", "stakeholder_data")
