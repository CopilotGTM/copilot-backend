"""Add research fields to crm_contacts and crm_companies.

Revision ID: 3c7f79637798
Revises: 3c7f79637797
Create Date: 2026-04-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637798"
down_revision: str | None = "3c7f79637797"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Elevate enrichment from deal-scoped (DealStakeholder.stakeholder_research) to
    # the contact/company level so research is done once and shared across all deals
    # the person or company participates in.
    op.add_column("crm_contacts", sa.Column("contact_research", sa.Text(), nullable=True))
    op.add_column(
        "crm_contacts",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("research_status", sa.String(length=20), nullable=True),
    )

    op.add_column("crm_companies", sa.Column("company_research", sa.Text(), nullable=True))
    op.add_column(
        "crm_companies",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_companies",
        sa.Column("research_status", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crm_companies", "research_status")
    op.drop_column("crm_companies", "last_researched_at")
    op.drop_column("crm_companies", "company_research")
    op.drop_column("crm_contacts", "research_status")
    op.drop_column("crm_contacts", "last_researched_at")
    op.drop_column("crm_contacts", "contact_research")
