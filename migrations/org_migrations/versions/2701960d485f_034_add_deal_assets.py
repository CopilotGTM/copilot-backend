"""034 add deal_assets table.

Revision ID: 2701960d485f
Revises: 3c7f79637767
Create Date: 2026-03-08 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2701960d485f"
down_revision: str | None = "3c7f79637767"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_assets",
        sa.Column(
            "asset_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False, index=True),
        sa.Column("deal_id", sa.UUID(), nullable=False, index=True),
        sa.Column("added_by_user_id", sa.UUID(), nullable=False, index=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column(
            "direction",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("storage_path", sa.String(length=1024), nullable=True, unique=True),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("mimetype", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("thumbnail_small_path", sa.String(length=1024), nullable=True),
        sa.Column("thumbnail_medium_path", sa.String(length=1024), nullable=True),
        sa.Column("external_url", sa.String(length=2048), nullable=True),
        sa.Column("external_source", sa.String(length=100), nullable=True),
        sa.Column("link_status", sa.String(length=50), nullable=True),
        sa.Column("link_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kb_source_id", sa.UUID(), nullable=True),
        sa.Column(
            "source_type",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text("'manual_upload'"),
        ),
        sa.Column("source_record_id", sa.String(length=512), nullable=True),
        sa.Column("source_context", sa.String(length=500), nullable=True),
        sa.Column("deal_stage_when_added", sa.String(length=255), nullable=True),
        sa.Column(
            "asset_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
        sa.ForeignKeyConstraint(
            ["deal_id"], ["crm_deals.deal_id"], name="fk_deal_asset_deal"
        ),
        sa.ForeignKeyConstraint(
            ["kb_source_id"],
            ["organisation_sources.source_id"],
            name="fk_deal_asset_kb_source",
        ),
        sa.UniqueConstraint(
            "deal_id",
            "source_type",
            "source_record_id",
            "filename",
            name="uq_deal_asset_source_dedup",
        ),
    )
    op.create_index(
        "ix_deal_asset_org_deal", "deal_assets", ["organisation_id", "deal_id"]
    )
    op.create_index(
        "ix_deal_asset_deal_status", "deal_assets", ["deal_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_deal_asset_deal_status", table_name="deal_assets")
    op.drop_index("ix_deal_asset_org_deal", table_name="deal_assets")
    op.drop_table("deal_assets")
