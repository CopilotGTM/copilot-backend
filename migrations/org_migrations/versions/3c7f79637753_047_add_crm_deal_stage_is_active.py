"""047 add is_active to crm_deal_stages.

Adds an is_active flag to CRMDealStage mirroring the one already on CRMPipeline
so that the HubSpot pipelines record sync can deactivate stages that disappear
from the provider snapshot without hard-deleting them.

Also adds a composite index on (pipeline_id, is_active) to support the writer's
active-stage lookups during the two-pass upsert.

Revision ID: 3c7f79637753
Revises: 3c7f79637754
Create Date: 2026-04-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637753"
down_revision: str | None = "3c7f79637754"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    )


def downgrade() -> None:
    op.drop_index("ix_crm_deal_stage_pipeline_active", table_name="crm_deal_stages")
    op.drop_column("crm_deal_stages", "is_active")
