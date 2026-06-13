"""Add contact participant stakeholder fields.

Revision ID: 3c7f79637739
Revises: 3c7f79637740
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637739"
down_revision: str | None = "3c7f79637740"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add participant behavior and profile snapshot fields without moving runtime reads."""
    op.add_column(
        "crm_deal_participants",
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("job_title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("department", sa.String(), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("phone_number", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("external_links", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column(
            "stance",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column(
            "decision_power",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("motivation", sa.String(), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("pain_points", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column(
            "temperature",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column(
            "participant_source",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'CRM'"),
        ),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("source_reference_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column(
            "approval_status",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'APPROVED'"),
        ),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("stakeholder_research", sa.Text(), nullable=True),
    )
    op.add_column(
        "crm_deal_participants",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("contact_research", sa.Text(), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("ALTER TABLE deal_jobs DROP CONSTRAINT IF EXISTS deal_jobs_stakeholder_id_fkey")
    op.execute(
        "ALTER TABLE deal_stakeholder_insights "
        "DROP CONSTRAINT IF EXISTS deal_stakeholder_insights_stakeholder_id_fkey"
    )
    op.execute(
        "ALTER TABLE deal_stakeholder_insights "
        "DROP CONSTRAINT IF EXISTS fk_stakeholder_insight_stakeholder"
    )
    op.execute(
        """
        WITH stakeholder_participant_matches AS (
            SELECT DISTINCT ON (stakeholders.organisation_id, stakeholders.stakeholder_id)
                stakeholders.organisation_id,
                stakeholders.stakeholder_id AS old_stakeholder_id,
                participants.participant_id
            FROM deal_stakeholders AS stakeholders
            JOIN crm_deal_participants AS participants
              ON participants.organisation_id = stakeholders.organisation_id
             AND participants.deal_id = stakeholders.deal_id
             AND participants.participant_type = 'contact'
             AND participants.is_deleted = false
             AND (
                 participants.participant_id = stakeholders.source_reference_id
                 OR participants.related_local_id = stakeholders.contact_id
                 OR (
                     stakeholders.email IS NOT NULL
                     AND participants.related_email IS NOT NULL
                     AND lower(btrim(participants.related_email)) =
                         lower(btrim(stakeholders.email))
                 )
             )
            ORDER BY
                stakeholders.organisation_id,
                stakeholders.stakeholder_id,
                CASE
                    WHEN participants.participant_id = stakeholders.source_reference_id THEN 0
                    WHEN participants.related_local_id = stakeholders.contact_id THEN 1
                    ELSE 2
                END,
                participants.participant_id
        )
        UPDATE deal_jobs AS jobs
           SET stakeholder_id = matches.participant_id,
               job_metadata = COALESCE(jobs.job_metadata, '{}'::jsonb)
                   || jsonb_build_object(
                       'legacy_deal_stakeholder_id', jobs.stakeholder_id::text,
                       'stakeholder_id', matches.participant_id::text,
                       'participant_id', matches.participant_id::text
                   )
          FROM stakeholder_participant_matches AS matches
         WHERE jobs.organisation_id = matches.organisation_id
           AND jobs.stakeholder_id = matches.old_stakeholder_id
        """
    )
    op.execute(
        """
        WITH stakeholder_participant_matches AS (
            SELECT DISTINCT ON (stakeholders.organisation_id, stakeholders.stakeholder_id)
                stakeholders.organisation_id,
                stakeholders.stakeholder_id AS old_stakeholder_id,
                participants.participant_id
            FROM deal_stakeholders AS stakeholders
            JOIN crm_deal_participants AS participants
              ON participants.organisation_id = stakeholders.organisation_id
             AND participants.deal_id = stakeholders.deal_id
             AND participants.participant_type = 'contact'
             AND participants.is_deleted = false
             AND (
                 participants.participant_id = stakeholders.source_reference_id
                 OR participants.related_local_id = stakeholders.contact_id
                 OR (
                     stakeholders.email IS NOT NULL
                     AND participants.related_email IS NOT NULL
                     AND lower(btrim(participants.related_email)) =
                         lower(btrim(stakeholders.email))
                 )
             )
            ORDER BY
                stakeholders.organisation_id,
                stakeholders.stakeholder_id,
                CASE
                    WHEN participants.participant_id = stakeholders.source_reference_id THEN 0
                    WHEN participants.related_local_id = stakeholders.contact_id THEN 1
                    ELSE 2
                END,
                participants.participant_id
        )
        UPDATE deal_stakeholder_insights AS insights
           SET stakeholder_id = matches.participant_id,
               insight_data = COALESCE(insights.insight_data, '{}'::jsonb)
                   || jsonb_build_object(
                       'legacy_deal_stakeholder_id', insights.stakeholder_id::text,
                       'stakeholder_id', matches.participant_id::text,
                       'participant_id', matches.participant_id::text
                   )
          FROM stakeholder_participant_matches AS matches
         WHERE insights.organisation_id = matches.organisation_id
           AND insights.stakeholder_id = matches.old_stakeholder_id
        """
    )
    op.create_index(
        "ix_crm_deal_participant_contact_lookup",
        "crm_deal_participants",
        [
            "organisation_id",
            "deal_id",
            "participant_type",
            "approval_status",
            "is_deleted",
        ],
    )
    op.create_index(
        "ux_crm_deal_participant_active_contact_email",
        "crm_deal_participants",
        [
            "organisation_id",
            "deal_id",
            sa.text("lower(btrim(related_email))"),
        ],
        unique=True,
        postgresql_where=sa.text(
            "participant_type = 'contact' "
            "AND related_email IS NOT NULL "
            "AND btrim(related_email) <> '' "
            "AND is_deleted = false"
        ),
    )
    op.create_index(
        "ux_crm_deal_participant_active_contact_local_id",
        "crm_deal_participants",
        ["organisation_id", "deal_id", "related_local_id"],
        unique=True,
        postgresql_where=sa.text(
            "participant_type = 'contact' "
            "AND related_local_id IS NOT NULL "
            "AND is_deleted = false"
        ),
    )


def downgrade() -> None:
    """Remove participant behavior and profile snapshot fields."""
    op.drop_index(
        "ux_crm_deal_participant_active_contact_local_id",
        table_name="crm_deal_participants",
    )
    op.drop_index(
        "ux_crm_deal_participant_active_contact_email",
        table_name="crm_deal_participants",
    )
    op.drop_index(
        "ix_crm_deal_participant_contact_lookup",
        table_name="crm_deal_participants",
    )
    op.drop_column("crm_contacts", "last_researched_at")
    op.drop_column("crm_contacts", "contact_research")

    for column_name in (
        "last_researched_at",
        "stakeholder_research",
        "approval_status",
        "source_reference_id",
        "participant_source",
        "temperature",
        "last_contact_at",
        "keywords",
        "pain_points",
        "motivation",
        "decision_power",
        "stance",
        "external_links",
        "phone_number",
        "department",
        "job_title",
        "created_by_user_id",
    ):
        op.drop_column("crm_deal_participants", column_name)
