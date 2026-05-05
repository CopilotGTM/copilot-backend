"""Add public ingress keys.

Revision ID: f1a2b3c4d5eb
Revises: f1a2b3c4d5ea
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5eb"
down_revision: str | None = "f1a2b3c4d5ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB_EMPTY = sa.text("'{}'::jsonb")
JSONB_ARRAY_EMPTY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    """Create the public ingress key registry used by private/public API entrypoints."""
    op.create_table(
        "public_ingress_keys",
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("key_type", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("public_key", sa.String(length=128), nullable=True),
        sa.Column("secret_hash", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "allowed_origins",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_ARRAY_EMPTY,
        ),
        sa.Column(
            "allowed_ips",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_ARRAY_EMPTY,
        ),
        sa.Column(
            "rate_limit_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("key_id"),
        sa.ForeignKeyConstraint(["organisation_id"], ["public.organisations.organisation_id"]),
        schema="public",
    )
    op.create_index(
        "uq_public_ingress_public_key",
        "public_ingress_keys",
        ["public_key"],
        unique=True,
        postgresql_where=sa.text("public_key IS NOT NULL"),
        schema="public",
    )
    op.create_index(
        "ix_public_ingress_keys_organisation_id",
        "public_ingress_keys",
        ["organisation_id"],
        schema="public",
    )
    op.create_index(
        "ix_public_ingress_keys_resource_id",
        "public_ingress_keys",
        ["resource_id"],
        schema="public",
    )
    op.create_index(
        "ix_public_ingress_org_status",
        "public_ingress_keys",
        ["organisation_id", "status"],
        schema="public",
    )
    op.create_index(
        "ix_public_ingress_resource",
        "public_ingress_keys",
        ["resource_type", "resource_id"],
        schema="public",
    )
    op.create_index(
        "ix_public_ingress_key_type",
        "public_ingress_keys",
        ["key_type", "status"],
        schema="public",
    )


def downgrade() -> None:
    """Drop the public ingress key registry and all supporting indexes."""
    op.drop_index(
        "ix_public_ingress_key_type",
        table_name="public_ingress_keys",
        schema="public",
    )
    op.drop_index(
        "ix_public_ingress_resource",
        table_name="public_ingress_keys",
        schema="public",
    )
    op.drop_index(
        "ix_public_ingress_org_status",
        table_name="public_ingress_keys",
        schema="public",
    )
    op.drop_index(
        "ix_public_ingress_keys_resource_id",
        table_name="public_ingress_keys",
        schema="public",
    )
    op.drop_index(
        "ix_public_ingress_keys_organisation_id",
        table_name="public_ingress_keys",
        schema="public",
    )
    op.drop_index(
        "uq_public_ingress_public_key",
        table_name="public_ingress_keys",
        schema="public",
    )
    op.drop_table("public_ingress_keys", schema="public")
