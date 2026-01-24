"""Allow NULL deal_id for org-level deal jobs.

Revision ID: 3c7f79637796
Revises: 3c7f79637773
Create Date: 2026-01-23

This migration makes deal_jobs.deal_id nullable so we can store
organisation-level jobs such as bulk deal imports that are not tied
to a single deal.
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637796"
down_revision: str | None = "3c7f79637773"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "deal_jobs",
        "deal_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    # Remove org-level jobs before restoring NOT NULL constraint
    op.execute("DELETE FROM deal_jobs WHERE deal_id IS NULL")
    op.alter_column(
        "deal_jobs",
        "deal_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
