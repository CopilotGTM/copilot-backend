"""Add active stage-key uniqueness for CRM stages.

Revision ID: 3c7f79637727
Revises: 3c7f79637728
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637727"
down_revision: str | None = "3c7f79637728"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "ux_crm_deal_stages_internal_active_stage_key"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM crm_deal_stages
                WHERE is_deleted = false
                  AND is_active = true
                  AND stage_key IS NOT NULL
                GROUP BY organisation_id, integration_id, pipeline_id, stage_key
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Duplicate active crm_deal_stages.stage_key rows exist; resolve before applying migration 072';
            END IF;
        END $$;
        """
    )
    op.create_index(
        INDEX_NAME,
        "crm_deal_stages",
        ["organisation_id", "integration_id", "pipeline_id", "stage_key"],
        unique=True,
        postgresql_where=sa.text(
            "is_deleted = false AND is_active = true AND stage_key IS NOT NULL"
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="crm_deal_stages", if_exists=True)
