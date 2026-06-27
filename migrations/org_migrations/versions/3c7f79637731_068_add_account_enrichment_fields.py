"""Add account enrichment metadata fields.

Revision ID: 3c7f79637731
Revises: 3c7f79637732
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637731"
down_revision: str | None = "3c7f79637732"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add account enrichment state columns used by async provider research."""
    op.add_column(
        "crm_companies",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_companies",
        sa.Column(
            "company_enrichment",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "crm_companies",
        sa.Column("company_enrichment_source", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "crm_companies",
        sa.Column("company_enriched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_companies",
        sa.Column("account_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove account enrichment state columns."""
    op.drop_column("crm_companies", "account_summary")
    op.drop_column("crm_companies", "company_enriched_at")
    op.drop_column("crm_companies", "company_enrichment_source")
    op.drop_column("crm_companies", "company_enrichment")
    op.drop_column("crm_companies", "last_researched_at")
