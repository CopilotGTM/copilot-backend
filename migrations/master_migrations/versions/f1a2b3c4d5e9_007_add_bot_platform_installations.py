"""Add bot_platform_installations table for multi-tenant bot tracking.

Revision ID: f1a2b3c4d5e9
Revises: f1a2b3c4d5e8
Create Date: 2026-02-26

This migration creates the bot_platform_installations table in the master (public) schema
to track per-workspace/per-tenant bot installations across platforms (Slack, Teams, Google Chat).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "f1a2b3c4d5e9"
down_revision = "f1a2b3c4d5e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_platform_installations",
        sa.Column("installation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_workspace_id", sa.String(512), nullable=False),
        sa.Column("external_workspace_name", sa.String(255), nullable=True),
        sa.Column("bot_token", sa.Text, nullable=True),
        sa.Column("bot_user_id", sa.String(255), nullable=True),
        sa.Column("extra_data", JSONB, nullable=True),
        sa.Column(
            "organisation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("public.organisations.organisation_id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("installed_by_external_user_id", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
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

    # Unique constraint: one installation per provider + workspace
    op.create_unique_constraint(
        "uq_bot_installation_provider_workspace",
        "bot_platform_installations",
        ["provider", "external_workspace_id"],
        schema="public",
    )

    op.create_index(
        "ix_bot_installation_provider",
        "bot_platform_installations",
        ["provider"],
        schema="public",
    )

    op.create_index(
        "ix_bot_installation_org",
        "bot_platform_installations",
        ["organisation_id"],
        schema="public",
    )

    op.create_index(
        "ix_bot_installation_workspace",
        "bot_platform_installations",
        ["external_workspace_id"],
        schema="public",
    )

    op.create_index(
        "ix_bot_installation_active",
        "bot_platform_installations",
        ["is_active"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bot_installation_active",
        table_name="bot_platform_installations",
        schema="public",
    )
    op.drop_index(
        "ix_bot_installation_workspace",
        table_name="bot_platform_installations",
        schema="public",
    )
    op.drop_index(
        "ix_bot_installation_org",
        table_name="bot_platform_installations",
        schema="public",
    )
    op.drop_index(
        "ix_bot_installation_provider",
        table_name="bot_platform_installations",
        schema="public",
    )
    op.drop_constraint(
        "uq_bot_installation_provider_workspace",
        "bot_platform_installations",
        schema="public",
    )
    op.drop_table("bot_platform_installations", schema="public")
