"""Drop retired deal_stakeholders table.

Revision ID: 3c7f79637738
Revises: 3c7f79637739
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637738"
down_revision: str | None = "3c7f79637739"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove the legacy stakeholder table after participant rows become canonical."""
    op.execute("ALTER TABLE deal_jobs DROP CONSTRAINT IF EXISTS deal_jobs_stakeholder_id_fkey")
    op.execute(
        "ALTER TABLE deal_stakeholder_insights "
        "DROP CONSTRAINT IF EXISTS deal_stakeholder_insights_stakeholder_id_fkey"
    )
    op.execute(
        "ALTER TABLE deal_stakeholder_insights "
        "DROP CONSTRAINT IF EXISTS fk_stakeholder_insight_stakeholder"
    )
    op.drop_table("deal_stakeholders")


def downgrade() -> None:
    """Recreate an empty legacy table shape for schema rollback only."""
    op.create_table(
        "deal_stakeholders",
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("stakeholder_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("job_title", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("phone_number", sa.String(length=50), nullable=True),
        sa.Column("external_links", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stance", sa.String(length=64), nullable=False, server_default=sa.text("'UNKNOWN'")),
        sa.Column(
            "decision_power",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
        sa.Column("champion_potential", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("motivation", sa.String(), nullable=True),
        sa.Column("pain_points", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engagement_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "temperature",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'CALENDAR_DISCOVERY'"),
        ),
        sa.Column("source_reference_id", sa.UUID(), nullable=True),
        sa.Column(
            "approval_status",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'APPROVED'"),
        ),
        sa.Column(
            "stakeholder_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("NULL"),
        ),
        sa.Column(
            "stakeholder_engagement_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("NULL"),
        ),
        sa.Column("stakeholder_research", sa.Text(), nullable=True, server_default=sa.text("NULL")),
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["crm_contacts.contact_id"]),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.PrimaryKeyConstraint("stakeholder_id"),
        sa.UniqueConstraint("deal_id", "email", name="uq_deal_stakeholder_email"),
    )
    op.create_index("ix_deal_stakeholders_organisation_id", "deal_stakeholders", ["organisation_id"])
    op.create_index("ix_deal_stakeholders_integration_id", "deal_stakeholders", ["integration_id"])
    op.create_index("ix_deal_stakeholders_deal_id", "deal_stakeholders", ["deal_id"])
    op.create_index("ix_deal_stakeholders_contact_id", "deal_stakeholders", ["contact_id"])
    op.create_index("ix_deal_stakeholders_created_at", "deal_stakeholders", ["created_at"])
    op.create_index("ix_deal_stakeholders_updated_at", "deal_stakeholders", ["updated_at"])
    op.create_index("ix_deal_stakeholder_email", "deal_stakeholders", ["email"])
    op.create_index(
        "ix_deal_stakeholder_org_deal",
        "deal_stakeholders",
        ["organisation_id", "deal_id"],
    )
    op.create_index(
        "ix_deal_stakeholder_created_by",
        "deal_stakeholders",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_deal_stakeholder_source_ref",
        "deal_stakeholders",
        ["source_reference_id"],
    )
    op.create_index(
        "ix_deal_stakeholder_approval_status",
        "deal_stakeholders",
        ["approval_status"],
    )
    op.create_index(
        "ix_deal_stakeholder_org_deal_email_active",
        "deal_stakeholders",
        ["organisation_id", "deal_id", "email", "is_deleted"],
    )
    op.create_index(
        "ix_deal_stakeholder_org_deal_approval_active",
        "deal_stakeholders",
        ["organisation_id", "deal_id", "approval_status", "is_deleted"],
    )
