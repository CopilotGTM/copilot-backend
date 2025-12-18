"""Standardize provider-deletion semantics across CRM tables.

Revision ID: 3c7f79637783
Revises: 3c7f79637784
Create Date: 2025-12-16

This migration:
1. Backfills SyncedRecordMixin.is_deleted from legacy is_deleted_remote columns
2. Drops redundant is_deleted_remote columns from crm_activities and crm_users

Rationale:
- is_deleted (from SyncedRecordMixin) is the single canonical flag for provider deletion.
- Archived/inactive semantics remain represented via separate fields like is_active.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3c7f79637783"
down_revision = "3c7f79637784"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade: Backfill canonical is_deleted and drop redundant columns."""

    # Backfill provider-deletion flags into canonical SyncedRecordMixin.is_deleted
    op.execute(
        """
        UPDATE crm_activities
        SET is_deleted = TRUE
        WHERE is_deleted_remote = TRUE
          AND (is_deleted IS DISTINCT FROM TRUE)
        """
    )
    op.execute(
        """
        UPDATE crm_users
        SET is_deleted = TRUE
        WHERE is_deleted_remote = TRUE
          AND (is_deleted IS DISTINCT FROM TRUE)
        """
    )

    # Drop legacy redundant columns (safe if already removed)
    op.execute("ALTER TABLE crm_activities DROP COLUMN IF EXISTS is_deleted_remote")
    op.execute("ALTER TABLE crm_users DROP COLUMN IF EXISTS is_deleted_remote")


def downgrade() -> None:
    """Downgrade: Re-add is_deleted_remote columns and mirror from is_deleted."""

    op.execute(
        "ALTER TABLE crm_users ADD COLUMN IF NOT EXISTS is_deleted_remote boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE crm_activities ADD COLUMN IF NOT EXISTS is_deleted_remote boolean NOT NULL DEFAULT false"
    )

    op.execute("UPDATE crm_users SET is_deleted_remote = COALESCE(is_deleted, FALSE)")
    op.execute("UPDATE crm_activities SET is_deleted_remote = COALESCE(is_deleted, FALSE)")
