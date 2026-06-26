"""Add trigram indexes for CRM text search.

Revision ID: 3c7f79637735
Revises: 3c7f79637736
Create Date: 2026-06-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3c7f79637735"
down_revision: str | None = "3c7f79637736"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create expression indexes used by CRM list and picker search."""
    op.execute("CREATE SCHEMA IF NOT EXISTS extensions")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_contact_search_email_trgm
        ON crm_contacts USING gin (email gin_trgm_ops)
        WHERE email IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_contact_search_full_name_trgm
        ON crm_contacts USING gin (full_name gin_trgm_ops)
        WHERE full_name IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_contact_search_company_name_trgm
        ON crm_contacts USING gin (company_name gin_trgm_ops)
        WHERE company_name IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_contact_search_additional_emails_trgm
        ON crm_contacts USING gin ((additional_emails::text) gin_trgm_ops)
        WHERE additional_emails <> '[]'::jsonb AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_company_search_name_trgm
        ON crm_companies USING gin (name gin_trgm_ops)
        WHERE name IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_company_search_domain_trgm
        ON crm_companies USING gin (domain gin_trgm_ops)
        WHERE domain IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_company_search_website_trgm
        ON crm_companies USING gin (website gin_trgm_ops)
        WHERE website IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_deal_search_name_trgm
        ON crm_deals USING gin (name gin_trgm_ops)
        WHERE name IS NOT NULL AND is_deleted = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_deal_search_company_name_trgm
        ON crm_deals USING gin (company_name gin_trgm_ops)
        WHERE company_name IS NOT NULL AND is_deleted = false
        """
    )


def downgrade() -> None:
    """Drop CRM text search indexes."""
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_search_company_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_search_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_company_search_website_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_company_search_domain_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_company_search_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_contact_search_additional_emails_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_contact_search_company_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_contact_search_full_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_crm_contact_search_email_trgm")
