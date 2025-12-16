"""Add owner_id FK columns and backfill from owner_external_id.

Revision ID: 3c7f79637781
Revises: 3c7f79637782
Create Date: 2025-12-16

This migration:
1. Adds nullable owner_id (UUID FK to crm_users.user_id) columns to:
   - crm_deals
   - crm_contacts
   - crm_companies
   - crm_activities
2. Creates indexes on the new columns for efficient owner-based queries.
3. Backfills owner_id by joining on (integration_id, owner_external_id) → crm_users.

The denormalized owner_external_id/owner_name/owner_email columns are retained
for display/fallback; owner_id enables proper FK-based filtering and analytics.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "3c7f79637781"
down_revision = "3c7f79637782"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade: Add owner_id FK columns and backfill."""

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Add owner_id columns (nullable, no FK constraint yet)
    # ─────────────────────────────────────────────────────────────────────────
    op.add_column(
        "crm_deals",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "crm_companies",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Create indexes for owner_id columns
    # ─────────────────────────────────────────────────────────────────────────
    op.create_index("ix_crm_deal_owner_id", "crm_deals", ["owner_id"])
    op.create_index("ix_crm_contact_owner_id", "crm_contacts", ["owner_id"])
    op.create_index("ix_crm_company_owner_id", "crm_companies", ["owner_id"])
    op.create_index("ix_crm_activity_owner_id", "crm_activities", ["owner_id"])

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Backfill owner_id from owner_external_id via crm_users
    # ─────────────────────────────────────────────────────────────────────────
    op.execute(
        """
        UPDATE crm_deals d
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE d.owner_external_id IS NOT NULL
          AND d.integration_id = u.integration_id
          AND d.owner_external_id = u.external_id
        """
    )

    op.execute(
        """
        UPDATE crm_contacts c
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE c.owner_external_id IS NOT NULL
          AND c.integration_id = u.integration_id
          AND c.owner_external_id = u.external_id
        """
    )

    op.execute(
        """
        UPDATE crm_companies co
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE co.owner_external_id IS NOT NULL
          AND co.integration_id = u.integration_id
          AND co.owner_external_id = u.external_id
        """
    )

    op.execute(
        """
        UPDATE crm_activities a
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE a.owner_external_id IS NOT NULL
          AND a.integration_id = u.integration_id
          AND a.owner_external_id = u.external_id
        """
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: Add FK constraints (deferred so backfill happens first)
    # ─────────────────────────────────────────────────────────────────────────
    op.create_foreign_key(
        "fk_crm_deal_owner_id",
        "crm_deals",
        "crm_users",
        ["owner_id"],
        ["user_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_crm_contact_owner_id",
        "crm_contacts",
        "crm_users",
        ["owner_id"],
        ["user_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_crm_company_owner_id",
        "crm_companies",
        "crm_users",
        ["owner_id"],
        ["user_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_crm_activity_owner_id",
        "crm_activities",
        "crm_users",
        ["owner_id"],
        ["user_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade: Remove owner_id FK columns."""

    # Drop FK constraints first
    op.drop_constraint("fk_crm_activity_owner_id", "crm_activities", type_="foreignkey")
    op.drop_constraint("fk_crm_company_owner_id", "crm_companies", type_="foreignkey")
    op.drop_constraint("fk_crm_contact_owner_id", "crm_contacts", type_="foreignkey")
    op.drop_constraint("fk_crm_deal_owner_id", "crm_deals", type_="foreignkey")

    # Drop indexes
    op.drop_index("ix_crm_activity_owner_id", "crm_activities")
    op.drop_index("ix_crm_company_owner_id", "crm_companies")
    op.drop_index("ix_crm_contact_owner_id", "crm_contacts")
    op.drop_index("ix_crm_deal_owner_id", "crm_deals")

    # Drop columns
    op.drop_column("crm_activities", "owner_id")
    op.drop_column("crm_companies", "owner_id")
    op.drop_column("crm_contacts", "owner_id")
    op.drop_column("crm_deals", "owner_id")
