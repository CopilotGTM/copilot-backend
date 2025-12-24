"""Allow NULL integration_id for user-created activities.

Revision ID: 3c7f79637778
Revises: 3c7f79637779
Create Date: 2025-12-24

This migration enables user-created notes (and potentially other activities)
by making integration_id nullable in the crm_activities table.

Changes:
1. Makes integration_id column nullable
2. Drops the existing unique constraint on (integration_id, external_id)
3. Creates a partial unique index for CRM-synced activities (integration_id IS NOT NULL)
4. Creates a partial unique index for user-created activities (integration_id IS NULL)
   ensuring external_id uniqueness within an organisation

User-created notes have:
- integration_id = NULL
- external_id = activity_id (UUID string)
- properties = {"is_user_created": true, "created_via": "api"}
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3c7f79637778"
down_revision: str | None = "3c7f79637779"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow NULL integration_id for user-created activities."""
    
    # Step 1: Drop the existing unique constraint
    # This constraint requires integration_id to be NOT NULL
    op.drop_constraint(
        "uq_crm_activity_external",
        "crm_activities",
        type_="unique",
    )
    
    # Step 2: Make integration_id nullable
    op.alter_column(
        "crm_activities",
        "integration_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    
    # Step 3: Create partial unique index for CRM-synced activities
    # This handles the case where integration_id IS NOT NULL
    op.create_index(
        "uq_crm_activity_external",
        "crm_activities",
        ["integration_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("integration_id IS NOT NULL"),
    )
    
    # Step 4: Create partial unique index for user-created activities
    # This handles the case where integration_id IS NULL
    # Ensures external_id is unique within an organisation for user-created items
    op.create_index(
        "uq_crm_activity_external_user",
        "crm_activities",
        ["organisation_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("integration_id IS NULL"),
    )


def downgrade() -> None:
    """Revert: Make integration_id NOT NULL again."""
    
    # Step 1: Drop the partial indexes
    op.drop_index(
        "uq_crm_activity_external_user",
        table_name="crm_activities",
    )
    op.drop_index(
        "uq_crm_activity_external",
        table_name="crm_activities",
    )
    
    # Step 2: Delete any user-created activities (integration_id IS NULL)
    # This is required before making the column NOT NULL
    op.execute(
        "DELETE FROM crm_activities WHERE integration_id IS NULL"
    )
    
    # Step 3: Make integration_id NOT NULL again
    op.alter_column(
        "crm_activities",
        "integration_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    
    # Step 4: Recreate the original unique constraint
    op.create_unique_constraint(
        "uq_crm_activity_external",
        "crm_activities",
        ["integration_id", "external_id"],
    )
