"""Add source_reference_id to deal_stakeholders.

Revision ID: 3c7f79637775
Revises: 3c7f79637776
Create Date: 2026-01-13 12:00:00.000000

This migration adds:
1. source_reference_id UUID column to deal_stakeholders table
   - Links to CRMDealParticipant.participant_id when source=CRM
   - Used for tracking stakeholder origin and avoiding duplicates
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637775"
down_revision: str | None = "3c7f79637776"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add source_reference_id column to deal_stakeholders
    op.add_column(
        "deal_stakeholders",
        sa.Column(
            "source_reference_id",
            sa.UUID(),
            nullable=True,
        ),
    )

    # Create index for efficient lookups by source reference
    op.create_index(
        "ix_deal_stakeholder_source_ref",
        "deal_stakeholders",
        ["source_reference_id"],
    )


def downgrade() -> None:
    # Drop index
    op.drop_index("ix_deal_stakeholder_source_ref", table_name="deal_stakeholders")

    # Drop column
    op.drop_column("deal_stakeholders", "source_reference_id")
