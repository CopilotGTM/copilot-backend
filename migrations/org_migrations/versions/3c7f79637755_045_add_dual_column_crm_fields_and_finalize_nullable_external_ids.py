"""045 add dual-column CRM fields and finalize nullable external IDs.

Revision ID: 3c7f79637755
Revises: 3c7f79637756
Create Date: 2026-04-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637755"
down_revision: str | None = "3c7f79637756"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crm_contacts",
        sa.Column("company_external_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("crm_users.user_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_deals",
        sa.Column(
            "updated_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("crm_users.user_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_deals",
        sa.Column(
            "company_id",
            sa.Uuid(),
            sa.ForeignKey("crm_companies.company_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_deals",
        sa.Column(
            "primary_contact_id",
            sa.Uuid(),
            sa.ForeignKey("crm_contacts.contact_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_deal_stage_history",
        sa.Column(
            "from_stage_id",
            sa.Uuid(),
            sa.ForeignKey("crm_deal_stages.stage_id"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_crm_contact_company_external",
        "crm_contacts",
        ["organisation_id", "company_external_id"],
    )
    op.create_index(
        "ix_crm_deal_created_by",
        "crm_deals",
        ["organisation_id", "created_by_user_id"],
    )
    op.create_index(
        "ix_crm_deal_updated_by",
        "crm_deals",
        ["organisation_id", "updated_by_user_id"],
    )
    op.create_index(
        "ix_crm_deal_company_id",
        "crm_deals",
        ["organisation_id", "company_id"],
    )
    op.create_index(
        "ix_crm_deal_primary_contact_id",
        "crm_deals",
        ["organisation_id", "primary_contact_id"],
    )
    op.create_index(
        "ix_crm_deal_stage_history_from_stage_id",
        "crm_deal_stage_history",
        ["from_stage_id"],
    )

    statements = [
        """
        UPDATE crm_deals d
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE d.owner_id IS NULL
          AND d.owner_external_id IS NOT NULL
          AND d.organisation_id = u.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM u.integration_id
          AND d.owner_external_id = u.external_id
        """,
        """
        UPDATE crm_contacts c
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE c.owner_id IS NULL
          AND c.owner_external_id IS NOT NULL
          AND c.organisation_id = u.organisation_id
          AND c.integration_id IS NOT DISTINCT FROM u.integration_id
          AND c.owner_external_id = u.external_id
        """,
        """
        UPDATE crm_companies c
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE c.owner_id IS NULL
          AND c.owner_external_id IS NOT NULL
          AND c.organisation_id = u.organisation_id
          AND c.integration_id IS NOT DISTINCT FROM u.integration_id
          AND c.owner_external_id = u.external_id
        """,
        """
        UPDATE crm_activities a
        SET owner_id = u.user_id
        FROM crm_users u
        WHERE a.owner_id IS NULL
          AND a.owner_external_id IS NOT NULL
          AND a.organisation_id = u.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM u.integration_id)
          AND a.owner_external_id = u.external_id
        """,
        """
        UPDATE crm_deals d
        SET pipeline_id = p.pipeline_id
        FROM crm_pipelines p
        WHERE d.pipeline_id IS NULL
          AND d.pipeline_external_id IS NOT NULL
          AND d.organisation_id = p.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM p.integration_id
          AND d.pipeline_external_id = p.external_id
        """,
        """
        UPDATE crm_deals d
        SET stage_id = s.stage_id
        FROM crm_deal_stages s
        WHERE d.stage_id IS NULL
          AND d.stage_external_id IS NOT NULL
          AND d.organisation_id = s.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM s.integration_id
          AND d.stage_external_id = s.external_id
        """,
        """
        UPDATE crm_deals d
        SET created_by_user_id = u.user_id
        FROM crm_users u
        WHERE d.created_by_user_id IS NULL
          AND d.created_by_user_external_id IS NOT NULL
          AND d.organisation_id = u.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM u.integration_id
          AND d.created_by_user_external_id = u.external_id
        """,
        """
        UPDATE crm_deals d
        SET updated_by_user_id = u.user_id
        FROM crm_users u
        WHERE d.updated_by_user_id IS NULL
          AND d.updated_by_user_external_id IS NOT NULL
          AND d.organisation_id = u.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM u.integration_id
          AND d.updated_by_user_external_id = u.external_id
        """,
        """
        UPDATE crm_deals d
        SET company_id = c.company_id
        FROM crm_companies c
        WHERE d.company_id IS NULL
          AND d.company_external_id IS NOT NULL
          AND d.organisation_id = c.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM c.integration_id
          AND d.company_external_id = c.external_id
        """,
        """
        UPDATE crm_deals d
        SET primary_contact_id = c.contact_id
        FROM crm_contacts c
        WHERE d.primary_contact_id IS NULL
          AND d.primary_contact_external_id IS NOT NULL
          AND d.organisation_id = c.organisation_id
          AND d.integration_id IS NOT DISTINCT FROM c.integration_id
          AND d.primary_contact_external_id = c.external_id
        """,
        """
        UPDATE crm_contacts c
        SET company_external_id = co.external_id
        FROM crm_companies co
        WHERE c.company_external_id IS NULL
          AND c.company_id IS NOT NULL
          AND c.company_id = co.company_id
          AND c.organisation_id = co.organisation_id
          AND c.integration_id IS NOT DISTINCT FROM co.integration_id
        """,
        """
        UPDATE crm_deal_stage_history h
        SET stage_id = s.stage_id
        FROM crm_deal_stages s
        WHERE h.stage_id IS NULL
          AND h.stage_external_id IS NOT NULL
          AND h.organisation_id = s.organisation_id
          AND h.integration_id IS NOT DISTINCT FROM s.integration_id
          AND h.stage_external_id = s.external_id
        """,
        """
        UPDATE crm_deal_stage_history h
        SET from_stage_id = s.stage_id
        FROM crm_deal_stages s
        WHERE h.from_stage_id IS NULL
          AND h.from_stage_external_id IS NOT NULL
          AND h.organisation_id = s.organisation_id
          AND h.integration_id IS NOT DISTINCT FROM s.integration_id
          AND h.from_stage_external_id = s.external_id
        """,
        """
        UPDATE crm_deal_participants p
        SET related_local_id = c.contact_id
        FROM crm_contacts c
        WHERE p.related_local_id IS NULL
          AND p.related_external_id IS NOT NULL
          AND p.participant_type = 'contact'
          AND p.organisation_id = c.organisation_id
          AND p.integration_id IS NOT DISTINCT FROM c.integration_id
          AND p.related_external_id = c.external_id
        """,
        """
        UPDATE crm_deal_participants p
        SET related_local_id = c.company_id
        FROM crm_companies c
        WHERE p.related_local_id IS NULL
          AND p.related_external_id IS NOT NULL
          AND p.participant_type = 'company'
          AND p.organisation_id = c.organisation_id
          AND p.integration_id IS NOT DISTINCT FROM c.integration_id
          AND p.related_external_id = c.external_id
        """,
        """
        UPDATE crm_deal_participants p
        SET related_local_id = u.user_id
        FROM crm_users u
        WHERE p.related_local_id IS NULL
          AND p.related_external_id IS NOT NULL
          AND p.participant_type = 'user'
          AND p.organisation_id = u.organisation_id
          AND p.integration_id IS NOT DISTINCT FROM u.integration_id
          AND p.related_external_id = u.external_id
        """,
        """
        UPDATE crm_activities a
        SET deal_id = d.deal_id
        FROM crm_deals d
        WHERE a.deal_id IS NULL
          AND a.deal_external_id IS NOT NULL
          AND a.organisation_id = d.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM d.integration_id)
          AND a.deal_external_id = d.external_id
        """,
        """
        UPDATE crm_activities a
        SET contact_id = c.contact_id
        FROM crm_contacts c
        WHERE a.contact_id IS NULL
          AND a.contact_external_id IS NOT NULL
          AND a.organisation_id = c.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM c.integration_id)
          AND a.contact_external_id = c.external_id
        """,
        """
        UPDATE crm_activities a
        SET company_id = c.company_id
        FROM crm_companies c
        WHERE a.company_id IS NULL
          AND a.company_external_id IS NOT NULL
          AND a.organisation_id = c.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM c.integration_id)
          AND a.company_external_id = c.external_id
        """,
        """
        UPDATE crm_activities a
        SET related_local_id = d.deal_id
        FROM crm_deals d
        WHERE a.related_local_id IS NULL
          AND a.related_external_id IS NOT NULL
          AND a.related_object_type IN ('DEAL', 'OPPORTUNITY')
          AND a.organisation_id = d.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM d.integration_id)
          AND a.related_external_id = d.external_id
        """,
        """
        UPDATE crm_activities a
        SET related_local_id = c.contact_id
        FROM crm_contacts c
        WHERE a.related_local_id IS NULL
          AND a.related_external_id IS NOT NULL
          AND a.related_object_type = 'CONTACT'
          AND a.organisation_id = c.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM c.integration_id)
          AND a.related_external_id = c.external_id
        """,
        """
        UPDATE crm_activities a
        SET related_local_id = c.company_id
        FROM crm_companies c
        WHERE a.related_local_id IS NULL
          AND a.related_external_id IS NOT NULL
          AND a.related_object_type IN ('COMPANY', 'ACCOUNT')
          AND a.organisation_id = c.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM c.integration_id)
          AND a.related_external_id = c.external_id
        """,
        """
        UPDATE crm_activities a
        SET related_local_id = u.user_id
        FROM crm_users u
        WHERE a.related_local_id IS NULL
          AND a.related_external_id IS NOT NULL
          AND a.related_object_type = 'USER'
          AND a.organisation_id = u.organisation_id
          AND (a.integration_id IS NULL OR a.integration_id IS NOT DISTINCT FROM u.integration_id)
          AND a.related_external_id = u.external_id
        """,
    ]

    for statement in statements:
        op.execute(sa.text(statement))

    op.drop_constraint("uq_crm_user_external", "crm_users", type_="unique")

    op.alter_column("crm_users", "external_id", existing_type=sa.String(length=255), nullable=True)
    op.alter_column(
        "crm_companies",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_contacts",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_pipelines",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_deal_stages",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column("crm_deals", "external_id", existing_type=sa.String(length=255), nullable=True)
    op.alter_column(
        "crm_deal_participants",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_deal_participants",
        "related_external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_deal_stage_history",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_activities",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_object_associations",
        "from_external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "crm_object_associations",
        "to_external_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )

    op.create_index(
        "uq_crm_user_external",
        "crm_users",
        ["integration_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_crm_user_external", table_name="crm_users")

    op.alter_column(
        "crm_object_associations",
        "to_external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_object_associations",
        "from_external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_activities",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_deal_stage_history",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_deal_participants",
        "related_external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_deal_participants",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column("crm_deals", "external_id", existing_type=sa.String(length=255), nullable=False)
    op.alter_column(
        "crm_deal_stages",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_pipelines",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_contacts",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "crm_companies",
        "external_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column("crm_users", "external_id", existing_type=sa.String(length=255), nullable=False)

    op.create_unique_constraint(
        "uq_crm_user_external",
        "crm_users",
        ["integration_id", "external_id"],
    )

    op.drop_index("ix_crm_deal_stage_history_from_stage_id", table_name="crm_deal_stage_history")
    op.drop_index("ix_crm_deal_primary_contact_id", table_name="crm_deals")
    op.drop_index("ix_crm_deal_company_id", table_name="crm_deals")
    op.drop_index("ix_crm_deal_updated_by", table_name="crm_deals")
    op.drop_index("ix_crm_deal_created_by", table_name="crm_deals")
    op.drop_index("ix_crm_contact_company_external", table_name="crm_contacts")

    op.drop_column("crm_deal_stage_history", "from_stage_id")
    op.drop_column("crm_deals", "primary_contact_id")
    op.drop_column("crm_deals", "company_id")
    op.drop_column("crm_deals", "updated_by_user_id")
    op.drop_column("crm_deals", "created_by_user_id")
    op.drop_column("crm_contacts", "company_external_id")
