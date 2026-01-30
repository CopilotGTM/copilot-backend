"""Add external_identity_mappings table for Teams/SSO identity linking.

Revision ID: f1a2b3c4d5e8
Revises: f1a2b3c4d5e7
Create Date: 2026-01-28

This migration creates the external_identity_mappings table in the master (public) schema
to support linking external identities (Teams, Slack, SSO providers) to CopilotGTM users.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "f1a2b3c4d5e8"
down_revision = "f1a2b3c4d5e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_identity_mappings",
        sa.Column("mapping_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.users.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "organisation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.organisations.organisation_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("external_user_id", sa.String(512), nullable=False),
        sa.Column("external_tenant_id", sa.String(512), nullable=True),
        sa.Column("external_user_email", sa.String(255), nullable=True),
        sa.Column("external_user_name", sa.String(255), nullable=True),
        sa.Column("conversation_context", JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        schema="public",
    )

    # Unique constraint for provider + external_user_id + external_tenant_id
    op.create_unique_constraint(
        "uq_external_identity_provider_user_tenant",
        "external_identity_mappings",
        ["provider", "external_user_id", "external_tenant_id"],
        schema="public",
    )

    # Index for user + provider lookup (e.g., find all user's linked identities)
    op.create_index(
        "ix_external_identity_user_provider",
        "external_identity_mappings",
        ["user_id", "provider"],
        schema="public",
    )

    # Index for org + provider lookup (e.g., find all org's Teams users)
    op.create_index(
        "ix_external_identity_org_provider",
        "external_identity_mappings",
        ["organisation_id", "provider"],
        schema="public",
    )

    # Index for external_user_id lookup (primary lookup path from bot)
    op.create_index(
        "ix_external_identity_external_user_id",
        "external_identity_mappings",
        ["external_user_id"],
        schema="public",
    )

    # Index for is_active status filtering
    op.create_index(
        "ix_external_identity_is_active",
        "external_identity_mappings",
        ["is_active"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_identity_is_active",
        table_name="external_identity_mappings",
        schema="public",
    )
    op.drop_index(
        "ix_external_identity_external_user_id",
        table_name="external_identity_mappings",
        schema="public",
    )
    op.drop_index(
        "ix_external_identity_org_provider",
        table_name="external_identity_mappings",
        schema="public",
    )
    op.drop_index(
        "ix_external_identity_user_provider",
        table_name="external_identity_mappings",
        schema="public",
    )
    op.drop_constraint(
        "uq_external_identity_provider_user_tenant",
        "external_identity_mappings",
        schema="public",
    )
    op.drop_table("external_identity_mappings", schema="public")
