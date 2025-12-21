"""Add deal_risks_blockers table for AI-identified and manual risk/blocker tracking.

Revision ID: 3c7f79637779
Revises: 3c7f79637780
Create Date: 2025-12-21 12:00:00.000000

"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637779"
down_revision: str | None = "3c7f79637780"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create deal_risks_blockers table
    op.create_table(
        "deal_risks_blockers",
        # Identity
        sa.Column(
            "risk_blocker_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Context
        sa.Column("organisation_id", sa.UUID(), nullable=False, index=True),
        sa.Column("deal_id", sa.UUID(), nullable=False, index=True),
        # Classification
        sa.Column("item_type", sa.String(32), nullable=False),  # RISK, BLOCKER
        sa.Column(
            "category",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'OTHER'"),
        ),
        sa.Column(
            "severity",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'MEDIUM'"),
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'AI_SUGGESTED'"),
        ),
        # Content
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        # Source Attribution
        sa.Column(
            "source_type",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'MANUAL'"),
        ),
        sa.Column("source_record_id", sa.String(512), nullable=True),
        sa.Column("source_context", sa.String(255), nullable=True),
        # AI Metadata
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("ai_reasoning", sa.Text(), nullable=True),
        sa.Column("extraction_run_id", sa.UUID(), nullable=True),
        # Assignment
        sa.Column("owner_name", sa.String(255), nullable=True),
        sa.Column("owner_email", sa.String(255), nullable=True),
        sa.Column("assigned_team", sa.String(128), nullable=True),
        # Resolution
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        # Provenance & Audit
        sa.Column(
            "is_ai_generated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_name", sa.String(255), nullable=True),
        sa.Column("last_modified_by_user_id", sa.UUID(), nullable=True),
        sa.Column("last_modified_by_user_name", sa.String(255), nullable=True),
        sa.Column("user_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        # Timestamps
        sa.Column(
            "identified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
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
        # Extended Data (audit trail, etc.)
        sa.Column(
            "risk_blocker_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Foreign key
        sa.ForeignKeyConstraint(
            ["deal_id"],
            ["crm_deals.deal_id"],
            name="fk_risk_blocker_deal",
            ondelete="CASCADE",
        ),
    )

    # Create indexes for common query patterns
    # Primary query: All items for a deal by status
    op.create_index(
        "ix_risk_blocker_deal_status",
        "deal_risks_blockers",
        ["deal_id", "status"],
    )

    # Filter by type within a deal
    op.create_index(
        "ix_risk_blocker_deal_type",
        "deal_risks_blockers",
        ["deal_id", "item_type"],
    )

    # Org-wide queries (dashboards, reports)
    op.create_index(
        "ix_risk_blocker_org_status",
        "deal_risks_blockers",
        ["organisation_id", "status"],
    )

    op.create_index(
        "ix_risk_blocker_org_severity",
        "deal_risks_blockers",
        ["organisation_id", "severity"],
    )

    # Category analysis
    op.create_index(
        "ix_risk_blocker_category",
        "deal_risks_blockers",
        ["organisation_id", "category"],
    )

    # Source tracking
    op.create_index(
        "ix_risk_blocker_source_ref",
        "deal_risks_blockers",
        ["source_type", "source_record_id"],
    )

    # AI-generated items pending review
    op.create_index(
        "ix_risk_blocker_ai_suggested",
        "deal_risks_blockers",
        ["organisation_id", "is_ai_generated", "status"],
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_risk_blocker_ai_suggested", table_name="deal_risks_blockers")
    op.drop_index("ix_risk_blocker_source_ref", table_name="deal_risks_blockers")
    op.drop_index("ix_risk_blocker_category", table_name="deal_risks_blockers")
    op.drop_index("ix_risk_blocker_org_severity", table_name="deal_risks_blockers")
    op.drop_index("ix_risk_blocker_org_status", table_name="deal_risks_blockers")
    op.drop_index("ix_risk_blocker_deal_type", table_name="deal_risks_blockers")
    op.drop_index("ix_risk_blocker_deal_status", table_name="deal_risks_blockers")

    # Drop table
    op.drop_table("deal_risks_blockers")
