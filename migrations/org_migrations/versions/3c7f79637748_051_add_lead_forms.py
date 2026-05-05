"""Add lead capture form tables.

Revision ID: 3c7f79637748
Revises: 3c7f79637750
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637748"
down_revision: str | None = "3c7f79637750"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB_EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    """Create org lead-form tables used for authenticated config and public submissions."""
    op.create_table(
        "lead_forms",
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("form_id"),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["crm_users.user_id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["crm_users.user_id"]),
    )

    op.create_table(
        "lead_form_fields",
        sa.Column("field_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("form_version", sa.Integer(), nullable=False),
        sa.Column("external_key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_key", sa.String(length=128), nullable=True),
        sa.Column("data_type", sa.String(length=32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("default_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.PrimaryKeyConstraint("field_id"),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["form_id"], ["lead_forms.form_id"]),
    )

    op.create_table(
        "lead_form_submissions",
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("form_version", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("mapped_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=JSONB_EMPTY),
        sa.Column("submitter_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=1000), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("submission_id"),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["form_id"], ["lead_forms.form_id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["crm_leads.lead_id"]),
    )

    _create_indexes()


def downgrade() -> None:
    """Drop lead form tables in reverse dependency order."""
    op.drop_table("lead_form_submissions")
    op.drop_table("lead_form_fields")
    op.drop_table("lead_forms")


def _create_indexes() -> None:
    """Create indexes supporting org-scoped form and submission lookups."""
    op.create_index("ix_lead_forms_organisation_id", "lead_forms", ["organisation_id"])
    op.create_index("ix_lead_forms_integration_id", "lead_forms", ["integration_id"])
    op.create_index(
        "ix_lead_form_org_status",
        "lead_forms",
        ["organisation_id", "status", "is_deleted"],
    )

    op.create_index("ix_lead_form_fields_organisation_id", "lead_form_fields", ["organisation_id"])
    op.create_index("ix_lead_form_fields_integration_id", "lead_form_fields", ["integration_id"])
    op.create_index("ix_lead_form_fields_form_id", "lead_form_fields", ["form_id"])
    op.create_index(
        "uq_lead_form_field_key_active",
        "lead_form_fields",
        ["form_id", "external_key", "is_deleted"],
        unique=True,
    )
    op.create_index(
        "ix_lead_form_field_form_order",
        "lead_form_fields",
        ["form_id", "display_order"],
    )

    op.create_index(
        "ix_lead_form_submissions_organisation_id",
        "lead_form_submissions",
        ["organisation_id"],
    )
    op.create_index("ix_lead_form_submissions_integration_id", "lead_form_submissions", ["integration_id"])
    op.create_index("ix_lead_form_submissions_form_id", "lead_form_submissions", ["form_id"])
    op.create_index("ix_lead_form_submissions_lead_id", "lead_form_submissions", ["lead_id"])
    op.create_index(
        "ix_lead_form_submission_form_received",
        "lead_form_submissions",
        ["organisation_id", "form_id", "received_at"],
    )
    op.create_index(
        "ix_lead_form_submission_lead",
        "lead_form_submissions",
        ["lead_id"],
    )
