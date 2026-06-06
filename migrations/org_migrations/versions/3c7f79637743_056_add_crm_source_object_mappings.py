"""056 add crm_source_object_mappings.

Revision ID: 3c7f79637743
Revises: 3c7f79637744
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637743"
down_revision: str | None = "3c7f79637744"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRM_SOURCE_OBJECT_TYPES_SQL = (
    "'deal', 'contact', 'company', 'user', 'pipeline', 'stage', 'activity', "
    "'note', 'task', 'association', 'lead', 'lead_participant', "
    "'deal_participant', 'stage_history', 'note_link'"
)


def upgrade() -> None:
    op.create_table(
        "crm_source_object_mappings",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("mapping_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("source_run_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_object_type", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("local_object_type", sa.String(length=64), nullable=False),
        sa.Column("local_object_id", sa.Uuid(), nullable=False),
        sa.Column("data_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "mapping_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('integration', 'migration')",
            name="ck_crm_source_mapping_source_kind",
        ),
        sa.CheckConstraint(
            "("
            "source_kind = 'integration' "
            "AND integration_id IS NOT NULL "
            "AND source_run_id IS NULL"
            ") OR ("
            "source_kind = 'migration' "
            "AND source_run_id IS NOT NULL "
            "AND integration_id IS NULL"
            ")",
            name="ck_crm_source_mapping_source_ref",
        ),
        sa.CheckConstraint(
            f"external_object_type IN ({CRM_SOURCE_OBJECT_TYPES_SQL})",
            name="ck_crm_source_mapping_external_type",
        ),
        sa.CheckConstraint(
            f"local_object_type IN ({CRM_SOURCE_OBJECT_TYPES_SQL})",
            name="ck_crm_source_mapping_local_type",
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(external_id)) > 0",
            name="ck_crm_source_mapping_non_empty_identity",
        ),
        sa.CheckConstraint(
            "provider = lower(btrim(provider))",
            name="ck_crm_source_mapping_provider_normalized",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(mapping_metadata) = 'object'",
            name="ck_crm_source_mapping_metadata_object",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["knowledge_integrations.integration_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mapping_id"),
    )
    op.create_index(
        op.f("ix_crm_source_object_mappings_created_at"),
        "crm_source_object_mappings",
        ["created_at"],
    )
    op.create_index(
        op.f("ix_crm_source_object_mappings_updated_at"),
        "crm_source_object_mappings",
        ["updated_at"],
    )
    op.create_index(
        op.f("ix_crm_source_object_mappings_organisation_id"),
        "crm_source_object_mappings",
        ["organisation_id"],
    )
    op.create_index(
        op.f("ix_crm_source_object_mappings_integration_id"),
        "crm_source_object_mappings",
        ["integration_id"],
    )
    op.create_index(
        op.f("ix_crm_source_object_mappings_source_run_id"),
        "crm_source_object_mappings",
        ["source_run_id"],
    )
    op.create_index(
        op.f("ix_crm_source_object_mappings_local_object_id"),
        "crm_source_object_mappings",
        ["local_object_id"],
    )
    op.create_index(
        "uq_crm_source_mapping_integration_identity",
        "crm_source_object_mappings",
        ["organisation_id", "integration_id", "provider", "external_object_type", "external_id"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'integration'"),
    )
    op.create_index(
        "uq_crm_source_mapping_migration_identity",
        "crm_source_object_mappings",
        ["organisation_id", "source_run_id", "provider", "external_object_type", "external_id"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'migration'"),
    )
    op.create_index(
        "ix_crm_source_mapping_source_lookup",
        "crm_source_object_mappings",
        ["organisation_id", "provider", "external_object_type", "external_id"],
    )
    op.create_index(
        "ix_crm_source_mapping_local_lookup",
        "crm_source_object_mappings",
        ["organisation_id", "local_object_type", "local_object_id"],
    )
    op.create_index(
        "ix_crm_source_mapping_integration",
        "crm_source_object_mappings",
        ["organisation_id", "integration_id"],
    )
    op.create_index(
        "ix_crm_source_mapping_active",
        "crm_source_object_mappings",
        ["organisation_id", "is_deleted"],
    )


def downgrade() -> None:
    op.drop_index("ix_crm_source_mapping_active", table_name="crm_source_object_mappings")
    op.drop_index("ix_crm_source_mapping_integration", table_name="crm_source_object_mappings")
    op.drop_index("ix_crm_source_mapping_local_lookup", table_name="crm_source_object_mappings")
    op.drop_index("ix_crm_source_mapping_source_lookup", table_name="crm_source_object_mappings")
    op.drop_index(
        "uq_crm_source_mapping_migration_identity",
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        "uq_crm_source_mapping_integration_identity",
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        op.f("ix_crm_source_object_mappings_local_object_id"),
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        op.f("ix_crm_source_object_mappings_source_run_id"),
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        op.f("ix_crm_source_object_mappings_integration_id"),
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        op.f("ix_crm_source_object_mappings_organisation_id"),
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        op.f("ix_crm_source_object_mappings_updated_at"),
        table_name="crm_source_object_mappings",
    )
    op.drop_index(
        op.f("ix_crm_source_object_mappings_created_at"),
        table_name="crm_source_object_mappings",
    )
    op.drop_table("crm_source_object_mappings")
