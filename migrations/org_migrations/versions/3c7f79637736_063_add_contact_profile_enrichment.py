"""Add contact profile enrichment columns.

Revision ID: 3c7f79637736
Revises: 3c7f79637737
Create Date: 2026-06-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637736"
down_revision: str | None = "3c7f79637737"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add canonical contact profile enrichment storage."""
    op.add_column(
        "crm_contacts",
        sa.Column("linkedin_url", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column(
            "profile_enrichment",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("profile_enrichment_source", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("profile_enriched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("linkedin_url", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_crm_contact_org_linkedin_url",
        "crm_contacts",
        ["organisation_id", "linkedin_url"],
        postgresql_where=sa.text("linkedin_url IS NOT NULL"),
    )
    op.execute(
        """
        UPDATE crm_contacts
        SET linkedin_url = properties ->> 'linkedin_url'
        WHERE linkedin_url IS NULL
          AND properties ? 'linkedin_url'
          AND NULLIF(TRIM(properties ->> 'linkedin_url'), '') IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE crm_deal_participants AS participant
        SET linkedin_url = contact.linkedin_url
        FROM crm_contacts AS contact
        WHERE participant.linkedin_url IS NULL
          AND contact.linkedin_url IS NOT NULL
          AND participant.organisation_id = contact.organisation_id
          AND participant.related_local_id = contact.contact_id
          AND participant.participant_type = 'CONTACT'
        """
    )


def downgrade() -> None:
    """Remove contact profile enrichment storage."""
    op.drop_index("ix_crm_contact_org_linkedin_url", table_name="crm_contacts")
    op.drop_column("crm_deal_participants", "linkedin_url")
    op.drop_column("crm_contacts", "profile_enriched_at")
    op.drop_column("crm_contacts", "profile_enrichment_source")
    op.drop_column("crm_contacts", "profile_enrichment")
    op.drop_column("crm_contacts", "linkedin_url")
