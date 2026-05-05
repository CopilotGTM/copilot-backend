"""Add CRM lead tables.

Revision ID: 3c7f79637750
Revises: 3c7f79637751
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637750"
down_revision: str | None = "3c7f79637751"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB_EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    """Create org CRM lead tables for lead lifecycle and history tracking."""
    op.create_table(
        "crm_leads",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("source_channel", sa.String(length=128), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("ingestion_key_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'new'")),
        sa.Column("priority", sa.String(length=32), nullable=True),
        sa.Column("score", sa.Numeric(8, 2), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("job_title", sa.String(length=255), nullable=True),
        sa.Column("linkedin_url", sa.String(length=500), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("company_domain", sa.String(length=255), nullable=True),
        sa.Column("company_website", sa.String(length=500), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("country", sa.String(length=128), nullable=True),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("interest", sa.String(length=255), nullable=True),
        sa.Column("budget_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("budget_currency", sa.String(length=3), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("owner_external_id", sa.String(length=255), nullable=True),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("owner_email", sa.String(length=255), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("converted_deal_id", sa.Uuid(), nullable=True),
        sa.Column("disqualified_reason", sa.String(length=255), nullable=True),
        sa.Column("archive_reason", sa.String(length=255), nullable=True),
        sa.Column("form_id", sa.Uuid(), nullable=True),
        sa.Column("form_version_id", sa.Uuid(), nullable=True),
        sa.Column("form_submission_id", sa.Uuid(), nullable=True),
        sa.Column("utm_source", sa.String(length=255), nullable=True),
        sa.Column("utm_medium", sa.String(length=255), nullable=True),
        sa.Column("utm_campaign", sa.String(length=255), nullable=True),
        sa.Column("utm_term", sa.String(length=255), nullable=True),
        sa.Column("utm_content", sa.String(length=255), nullable=True),
        sa.Column("attribution", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("consent", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("data_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("crm_url", sa.String(length=500), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disqualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("lead_id"),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["crm_users.user_id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["crm_contacts.contact_id"]),
        sa.ForeignKeyConstraint(["company_id"], ["crm_companies.company_id"]),
        sa.ForeignKeyConstraint(["converted_deal_id"], ["crm_deals.deal_id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["crm_users.user_id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["crm_users.user_id"]),
        sa.CheckConstraint(
            "status != 'converted' OR (converted_deal_id IS NOT NULL AND converted_at IS NOT NULL)",
            name="ck_crm_lead_converted_requires_deal",
        ),
        sa.CheckConstraint("budget_amount IS NULL OR budget_amount >= 0", name="ck_crm_lead_budget_non_negative"),
        sa.CheckConstraint("score IS NULL OR score >= 0", name="ck_crm_lead_score_non_negative"),
    )

    op.create_table(
        "crm_lead_participants",
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("participant_type", sa.String(length=32), nullable=False),
        sa.Column("relationship_role", sa.String(length=128), nullable=False, server_default=sa.text("'participant'")),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("related_external_id", sa.String(length=255), nullable=True),
        sa.Column("related_local_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("job_title", sa.String(length=255), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("company_domain", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("source_reference_id", sa.Uuid(), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("data_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("participant_id"),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["crm_leads.lead_id"]),
        sa.CheckConstraint(
            "related_external_id IS NOT NULL OR related_local_id IS NOT NULL OR email IS NOT NULL",
            name="ck_crm_lead_participant_has_identity",
        ),
    )

    op.create_table(
        "crm_lead_status_history",
        sa.Column("history_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("transition_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.PrimaryKeyConstraint("history_id"),
        sa.ForeignKeyConstraint(["lead_id"], ["crm_leads.lead_id"]),
    )

    _create_indexes()


def downgrade() -> None:
    """Drop org CRM lead tables in dependency-safe reverse order."""
    op.drop_table("crm_lead_status_history")
    op.drop_table("crm_lead_participants")
    op.drop_table("crm_leads")


def _create_indexes() -> None:
    """Create indexes used for tenant lookups, filtering, and conversion joins."""
    op.create_index("ix_crm_leads_organisation_id", "crm_leads", ["organisation_id"])
    op.create_index("ix_crm_leads_integration_id", "crm_leads", ["integration_id"])
    op.create_index("ix_crm_leads_owner_id", "crm_leads", ["owner_id"])
    op.create_index("ix_crm_leads_contact_id", "crm_leads", ["contact_id"])
    op.create_index("ix_crm_leads_company_id", "crm_leads", ["company_id"])
    op.create_index("ix_crm_leads_converted_deal_id", "crm_leads", ["converted_deal_id"])
    op.create_index("ix_crm_leads_form_id", "crm_leads", ["form_id"])
    op.create_index("ix_crm_leads_form_version_id", "crm_leads", ["form_version_id"])
    op.create_index("ix_crm_leads_form_submission_id", "crm_leads", ["form_submission_id"])
    op.create_index("ix_crm_leads_created_by_user_id", "crm_leads", ["created_by_user_id"])
    op.create_index("ix_crm_leads_updated_by_user_id", "crm_leads", ["updated_by_user_id"])
    op.create_index(
        "uq_crm_lead_provider_external",
        "crm_leads",
        ["integration_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("integration_id IS NOT NULL AND external_id IS NOT NULL"),
    )
    op.create_index("ix_crm_lead_org_status_captured", "crm_leads", ["organisation_id", "status", "captured_at"])
    op.create_index("ix_crm_lead_org_owner_status", "crm_leads", ["organisation_id", "owner_id", "status"])
    op.create_index(
        "ix_crm_lead_org_email_active",
        "crm_leads",
        ["organisation_id", "email"],
        postgresql_where=sa.text("email IS NOT NULL AND is_deleted = false"),
    )
    op.create_index(
        "ix_crm_lead_org_company_domain_active",
        "crm_leads",
        ["organisation_id", "company_domain"],
        postgresql_where=sa.text("company_domain IS NOT NULL AND is_deleted = false"),
    )
    op.create_index(
        "ix_crm_lead_org_form_captured",
        "crm_leads",
        ["organisation_id", "form_id", "captured_at"],
        postgresql_where=sa.text("form_id IS NOT NULL"),
    )
    op.create_index(
        "ix_crm_lead_org_converted_deal",
        "crm_leads",
        ["organisation_id", "converted_deal_id"],
        postgresql_where=sa.text("converted_deal_id IS NOT NULL"),
    )
    op.create_index(
        "uq_crm_lead_private_ingestion_idempotency",
        "crm_leads",
        ["organisation_id", "ingestion_key_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "source_type = 'api' AND ingestion_key_id IS NOT NULL "
            "AND idempotency_key IS NOT NULL AND form_submission_id IS NULL"
        ),
    )

    op.create_index("ix_crm_lead_participants_organisation_id", "crm_lead_participants", ["organisation_id"])
    op.create_index("ix_crm_lead_participants_integration_id", "crm_lead_participants", ["integration_id"])
    op.create_index("ix_crm_lead_participants_lead_id", "crm_lead_participants", ["lead_id"])
    op.create_index("ix_crm_lead_participants_relationship_role", "crm_lead_participants", ["relationship_role"])
    op.create_index("ix_crm_lead_participants_related_local_id", "crm_lead_participants", ["related_local_id"])
    op.create_index("ix_crm_lead_participants_source_reference_id", "crm_lead_participants", ["source_reference_id"])
    op.create_index(
        "uq_crm_lead_participant_external",
        "crm_lead_participants",
        ["integration_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("integration_id IS NOT NULL AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_crm_lead_participant_local_role",
        "crm_lead_participants",
        ["lead_id", "participant_type", "related_local_id", "relationship_role"],
        unique=True,
        postgresql_where=sa.text("related_local_id IS NOT NULL AND is_deleted = false"),
    )
    op.create_index(
        "uq_crm_lead_participant_external_role",
        "crm_lead_participants",
        ["lead_id", "participant_type", "related_external_id", "relationship_role"],
        unique=True,
        postgresql_where=sa.text("related_external_id IS NOT NULL AND is_deleted = false"),
    )
    op.create_index(
        "uq_crm_lead_participant_primary_role",
        "crm_lead_participants",
        ["lead_id", "participant_type", "relationship_role"],
        unique=True,
        postgresql_where=sa.text("is_primary = true AND is_deleted = false"),
    )
    op.create_index("ix_crm_lead_participant_org_lead", "crm_lead_participants", ["organisation_id", "lead_id"])
    op.create_index(
        "ix_crm_lead_participant_org_type",
        "crm_lead_participants",
        ["organisation_id", "participant_type", "is_deleted"],
    )
    op.create_index(
        "ix_crm_lead_participant_email",
        "crm_lead_participants",
        ["organisation_id", "email"],
        postgresql_where=sa.text("email IS NOT NULL AND is_deleted = false"),
    )
    op.create_index("ix_crm_lead_participant_related_local", "crm_lead_participants", ["related_local_id"])

    op.create_index("ix_crm_lead_status_history_organisation_id", "crm_lead_status_history", ["organisation_id"])
    op.create_index("ix_crm_lead_status_history_lead_id", "crm_lead_status_history", ["lead_id"])
    op.create_index("ix_crm_lead_status_history_actor_user_id", "crm_lead_status_history", ["actor_user_id"])
    op.create_index(
        "ix_crm_lead_status_history_lead",
        "crm_lead_status_history",
        ["organisation_id", "lead_id", "changed_at"],
    )
    op.create_index(
        "ix_crm_lead_status_history_status",
        "crm_lead_status_history",
        ["organisation_id", "to_status", "changed_at"],
    )
