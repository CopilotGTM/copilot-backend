"""015 add crm_object_associations table

Revision ID: 3c7f79637785
Revises: 3c7f79637786
Create Date: 2025-12-16 00:00:00.000000

This migration:
- Creates the crm_object_associations table for generic CRM object relationships
- Supports associations like: deal↔contact roles, contact↔company, activity↔entities
- Uses FlexibleSyncedRecordMixin pattern (integration_id nullable) for internal associations
- Designed to be provider-agnostic (HubSpot, Salesforce, Pipedrive, Zoho)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637785"
down_revision: Union[str, None] = "3c7f79637786"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create crm_object_associations table."""

    op.create_table(
        "crm_object_associations",
        # Primary key
        sa.Column("association_id", sa.Uuid(), nullable=False),
        # FlexibleSyncedRecordMixin columns
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Identity
        sa.Column("external_id", sa.String(255), nullable=True),
        # From Object
        sa.Column("from_object_type", sa.String(32), nullable=False),
        sa.Column("from_external_id", sa.String(255), nullable=False),
        sa.Column("from_local_id", sa.Uuid(), nullable=True),
        # To Object
        sa.Column("to_object_type", sa.String(32), nullable=False),
        sa.Column("to_external_id", sa.String(255), nullable=False),
        sa.Column("to_local_id", sa.Uuid(), nullable=True),
        # Association Metadata
        sa.Column(
            "association_category",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'other'"),
        ),
        sa.Column("association_type_id", sa.String(128), nullable=True),
        sa.Column("association_label", sa.String(255), nullable=True),
        # Role Information
        sa.Column("role_label", sa.String(128), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Flexible Properties
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Sync Timestamps
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        # Raw Payload
        sa.Column("data_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Constraints
        sa.PrimaryKeyConstraint("association_id"),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["knowledge_integrations.integration_id"],
            name="crm_object_associations_integration_id_fkey",
        ),
        # Unique constraints
        sa.UniqueConstraint(
            "integration_id",
            "external_id",
            name="uq_crm_object_association_external",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "integration_id",
            "from_object_type",
            "from_external_id",
            "to_object_type",
            "to_external_id",
            "association_type_id",
            name="uq_crm_object_association_logical",
        ),
    )

    # Create indexes for common query patterns
    op.create_index(
        "ix_crm_assoc_from_object",
        "crm_object_associations",
        ["organisation_id", "from_object_type", "from_external_id"],
    )
    op.create_index(
        "ix_crm_assoc_to_object",
        "crm_object_associations",
        ["organisation_id", "to_object_type", "to_external_id"],
    )
    op.create_index(
        "ix_crm_assoc_org_types",
        "crm_object_associations",
        ["organisation_id", "from_object_type", "to_object_type"],
    )
    op.create_index(
        "ix_crm_assoc_from_local",
        "crm_object_associations",
        ["from_local_id"],
    )
    op.create_index(
        "ix_crm_assoc_to_local",
        "crm_object_associations",
        ["to_local_id"],
    )
    op.create_index(
        "ix_crm_assoc_role",
        "crm_object_associations",
        ["organisation_id", "role_label"],
    )
    op.create_index(
        "ix_crm_assoc_type_id",
        "crm_object_associations",
        ["association_type_id"],
    )
    # Standard mixin indexes
    op.create_index(
        "ix_crm_object_associations_organisation_id",
        "crm_object_associations",
        ["organisation_id"],
    )
    op.create_index(
        "ix_crm_object_associations_integration_id",
        "crm_object_associations",
        ["integration_id"],
    )
    op.create_index(
        "ix_crm_object_associations_created_at",
        "crm_object_associations",
        ["created_at"],
    )
    op.create_index(
        "ix_crm_object_associations_updated_at",
        "crm_object_associations",
        ["updated_at"],
    )


def downgrade() -> None:
    """Drop crm_object_associations table."""

    # Drop indexes
    op.drop_index("ix_crm_object_associations_updated_at", table_name="crm_object_associations")
    op.drop_index("ix_crm_object_associations_created_at", table_name="crm_object_associations")
    op.drop_index("ix_crm_object_associations_integration_id", table_name="crm_object_associations")
    op.drop_index("ix_crm_object_associations_organisation_id", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_type_id", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_role", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_to_local", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_from_local", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_org_types", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_to_object", table_name="crm_object_associations")
    op.drop_index("ix_crm_assoc_from_object", table_name="crm_object_associations")

    # Drop table
    op.drop_table("crm_object_associations")
