"""053 add deal-scale indexes for large CRM organisations.

Revision ID: 3c7f79637746
Revises: 3c7f79637747
Create Date: 2026-05-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637746"
down_revision: str | None = "3c7f79637747"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    """Add indexes used by large deal-list and deal-matcher workloads."""
    if not _has_column("crm_deal_stages", "is_active"):
        op.add_column(
            "crm_deal_stages",
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )

    op.create_index(
        "ix_crm_deal_stage_pipeline_active",
        "crm_deal_stages",
        ["pipeline_id", "is_active"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_crm_deal_org_active_created",
        "crm_deals",
        ["organisation_id", "is_deleted", "created_at", "deal_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_crm_deal_org_active_status_created",
        "crm_deals",
        ["organisation_id", "is_deleted", "deal_status", "created_at", "deal_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_crm_deal_org_active_owner_created",
        "crm_deals",
        ["organisation_id", "is_deleted", "owner_id", "created_at", "deal_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_crm_deal_participant_org_deal_type_active",
        "crm_deal_participants",
        ["organisation_id", "deal_id", "participant_type", "is_deleted"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_deal_stakeholder_org_deal_email_active",
        "deal_stakeholders",
        ["organisation_id", "deal_id", "email", "is_deleted"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_deal_stakeholder_org_deal_approval_active",
        "deal_stakeholders",
        ["organisation_id", "deal_id", "approval_status", "is_deleted"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop deal-scale indexes."""
    op.drop_index(
        "ix_deal_stakeholder_org_deal_approval_active",
        table_name="deal_stakeholders",
        if_exists=True,
    )
    op.drop_index(
        "ix_deal_stakeholder_org_deal_email_active",
        table_name="deal_stakeholders",
        if_exists=True,
    )
    op.drop_index(
        "ix_crm_deal_participant_org_deal_type_active",
        table_name="crm_deal_participants",
        if_exists=True,
    )
    op.drop_index(
        "ix_crm_deal_org_active_owner_created",
        table_name="crm_deals",
        if_exists=True,
    )
    op.drop_index(
        "ix_crm_deal_org_active_status_created",
        table_name="crm_deals",
        if_exists=True,
    )
    op.drop_index(
        "ix_crm_deal_org_active_created",
        table_name="crm_deals",
        if_exists=True,
    )
