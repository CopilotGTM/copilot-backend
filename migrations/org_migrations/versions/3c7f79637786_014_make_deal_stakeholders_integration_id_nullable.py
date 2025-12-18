"""014 make deal_stakeholders integration_id nullable

Revision ID: 3c7f79637786
Revises: 3c7f79637787
Create Date: 2025-12-14 00:00:00.000000

This migration:
- Makes deal_stakeholders.integration_id nullable so stakeholders can be created from
  non-CRM sources (calendar/email discovery, manual) without being owned by a single CRM integration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3c7f79637786"
down_revision: Union[str, None] = "3c7f79637787"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - make deal_stakeholders.integration_id nullable."""

    # Drop the existing foreign key constraint first (safe if local DB differs)
    op.execute(
        "ALTER TABLE deal_stakeholders DROP CONSTRAINT IF EXISTS deal_stakeholders_integration_id_fkey"
    )

    # Alter column to be nullable
    op.alter_column(
        "deal_stakeholders",
        "integration_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )

    # Re-add foreign key constraint (allowing NULL)
    op.create_foreign_key(
        "deal_stakeholders_integration_id_fkey",
        "deal_stakeholders",
        "knowledge_integrations",
        ["integration_id"],
        ["integration_id"],
    )


def downgrade() -> None:
    """Downgrade schema - make deal_stakeholders.integration_id NOT NULL again.

    Note: This may fail if any deal_stakeholders rows have NULL integration_id.
    """

    op.execute(
        "ALTER TABLE deal_stakeholders DROP CONSTRAINT IF EXISTS deal_stakeholders_integration_id_fkey"
    )

    op.alter_column(
        "deal_stakeholders",
        "integration_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )

    op.create_foreign_key(
        "deal_stakeholders_integration_id_fkey",
        "deal_stakeholders",
        "knowledge_integrations",
        ["integration_id"],
        ["integration_id"],
    )
