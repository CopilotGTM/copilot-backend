"""044 add company_id FK to crm_contacts for explicit account linking.

Adds a nullable company_id column on crm_contacts referencing crm_companies,
with an index for efficient lookups from the account side. Backfills existing
contacts by matching email domains against company domains within the same org.

Revision ID: 3c7f79637756
Revises: 3c7f79637757
Create Date: 2026-04-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637756"
down_revision: str | None = "3c7f79637757"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crm_contacts",
        sa.Column(
            "company_id",
            sa.Uuid(),
            sa.ForeignKey("crm_companies.company_id"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_crm_contact_company_id",
        "crm_contacts",
        ["organisation_id", "company_id"],
    )

    # Backfill: link contacts to accounts by matching email domain
    op.execute(
        sa.text("""
            UPDATE crm_contacts c
            SET company_id = co.company_id,
                company_name = COALESCE(c.company_name, co.name)
            FROM crm_companies co
            WHERE c.organisation_id = co.organisation_id
              AND co.is_deleted = false
              AND c.is_deleted = false
              AND c.company_id IS NULL
              AND c.email IS NOT NULL
              AND co.domain IS NOT NULL
              AND split_part(lower(c.email), '@', 2) = lower(co.domain)
        """)
    )


def downgrade() -> None:
    op.drop_index("ix_crm_contact_company_id", table_name="crm_contacts")
    op.drop_column("crm_contacts", "company_id")
