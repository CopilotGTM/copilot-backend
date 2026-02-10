"""Add crm_custom_field_definitions table.

Revision ID: 3c7f79637797
Revises: 3c7f79637796
Create Date: 2026-02-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637797"
down_revision: str | None = "3c7f79637796"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crm_custom_field_definitions",
        sa.Column(
            "field_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("data_type", sa.String(length=32), nullable=False),
        sa.Column(
            "required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_predefined",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "validation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "formula",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "key ~ '^[a-z][a-z0-9_]{1,62}$'",
            name="ck_crm_custom_field_key_format",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "entity_type",
            "key",
            "is_deleted",
            name="uq_crm_custom_field_org_entity_key_state",
        ),
    )

    op.create_index(
        "ix_crm_custom_field_definitions_organisation_id",
        "crm_custom_field_definitions",
        ["organisation_id"],
    )
    op.create_index(
        "ix_crm_custom_field_definitions_entity_type",
        "crm_custom_field_definitions",
        ["entity_type"],
    )
    op.create_index(
        "ix_crm_custom_field_org_entity_active",
        "crm_custom_field_definitions",
        ["organisation_id", "entity_type", "is_active", "is_deleted"],
    )
    op.create_index(
        "ix_crm_custom_field_definitions_created_at",
        "crm_custom_field_definitions",
        ["created_at"],
    )
    op.create_index(
        "ix_crm_custom_field_definitions_updated_at",
        "crm_custom_field_definitions",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crm_custom_field_definitions_updated_at",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "ix_crm_custom_field_definitions_created_at",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "ix_crm_custom_field_org_entity_active",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "ix_crm_custom_field_definitions_entity_type",
        table_name="crm_custom_field_definitions",
    )
    op.drop_index(
        "ix_crm_custom_field_definitions_organisation_id",
        table_name="crm_custom_field_definitions",
    )
    op.drop_table("crm_custom_field_definitions")
