"""046 extend crm_custom_field_definitions into a source-aware field registry.

Revision ID: 3c7f79637754
Revises: 3c7f79637755
Create Date: 2026-04-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637754"
down_revision: str | None = "3c7f79637755"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column(
            "field_source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column("integration_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column("provider_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column("provider_field_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column("is_provider_custom", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column(
            "data_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_custom_field_definitions",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_crm_custom_field_definitions_integration_id",
        "crm_custom_field_definitions",
        "knowledge_integrations",
        ["integration_id"],
        ["integration_id"],
    )

    op.create_index(
        "ix_crm_custom_field_definitions_field_source",
        "crm_custom_field_definitions",
        ["field_source"],
    )
    op.create_index(
        "ix_crm_custom_field_definitions_integration_id",
        "crm_custom_field_definitions",
        ["integration_id"],
    )
    op.drop_index(
        "ix_crm_custom_field_org_entity_active",
        table_name="crm_custom_field_definitions",
    )
    op.create_index(
        "ix_crm_custom_field_org_entity_active",
        "crm_custom_field_definitions",
        ["organisation_id", "entity_type", "field_source", "is_active", "is_deleted"],
    )
    op.create_index(
        "ix_crm_custom_field_integration_entity_active",
        "crm_custom_field_definitions",
        ["integration_id", "entity_type", "is_active", "is_deleted"],
    )

    op.drop_constraint(
        "uq_crm_custom_field_org_entity_key_state",
        "crm_custom_field_definitions",
        type_="unique",
    )
    op.create_index(
        "uq_crm_custom_field_internal_key_state",
        "crm_custom_field_definitions",
        ["organisation_id", "entity_type", "key", "is_deleted"],
        unique=True,
        postgresql_where=sa.text("field_source = 'internal'"),
    )
    op.create_index(
        "uq_crm_custom_field_provider_key_state",
        "crm_custom_field_definitions",
        ["integration_id", "entity_type", "provider_field_key", "is_deleted"],
        unique=True,
        postgresql_where=sa.text("field_source = 'provider'"),
    )

    op.create_check_constraint(
        "ck_crm_custom_field_source_provenance",
        "crm_custom_field_definitions",
        "("
        "field_source = 'internal' "
        "AND integration_id IS NULL "
        "AND provider_name IS NULL "
        "AND provider_field_key IS NULL "
        "AND is_provider_custom IS NULL"
        ") OR ("
        "field_source = 'provider' "
        "AND integration_id IS NOT NULL "
        "AND provider_name IS NOT NULL "
        "AND provider_field_key IS NOT NULL"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_crm_custom_field_source_provenance",
        "crm_custom_field_definitions",
        type_="check",
    )
    op.drop_index(
        "uq_crm_custom_field_provider_key_state",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "uq_crm_custom_field_internal_key_state",
        table_name="crm_custom_field_definitions",
    )
    op.create_unique_constraint(
        "uq_crm_custom_field_org_entity_key_state",
        "crm_custom_field_definitions",
        ["organisation_id", "entity_type", "key", "is_deleted"],
    )

    op.drop_index(
        "ix_crm_custom_field_integration_entity_active",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "ix_crm_custom_field_org_entity_active",
        table_name="crm_custom_field_definitions",
    )
    op.create_index(
        "ix_crm_custom_field_org_entity_active",
        "crm_custom_field_definitions",
        ["organisation_id", "entity_type", "is_active", "is_deleted"],
    )
    op.drop_index(
        "ix_crm_custom_field_definitions_integration_id",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "ix_crm_custom_field_definitions_field_source",
        table_name="crm_custom_field_definitions",
    )

    op.drop_constraint(
        "fk_crm_custom_field_definitions_integration_id",
        "crm_custom_field_definitions",
        type_="foreignkey",
    )

    op.drop_column("crm_custom_field_definitions", "last_seen_at")
    op.drop_column("crm_custom_field_definitions", "source_modified_at")
    op.drop_column("crm_custom_field_definitions", "data_payload")
    op.drop_column("crm_custom_field_definitions", "is_provider_custom")
    op.drop_column("crm_custom_field_definitions", "provider_field_key")
    op.drop_column("crm_custom_field_definitions", "provider_name")
    op.drop_column("crm_custom_field_definitions", "integration_id")
    op.drop_column("crm_custom_field_definitions", "field_source")
