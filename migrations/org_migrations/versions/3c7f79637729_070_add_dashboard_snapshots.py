"""Add tenant dashboard snapshot storage.

Revision ID: 3c7f79637729
Revises: 3c7f79637730
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637729"
down_revision: str | None = "3c7f79637730"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dashboards",
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
        sa.Column("dashboard_id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("dashboard_key", sa.String(length=128), nullable=False),
        sa.Column("dashboard_name", sa.String(length=255), nullable=False),
        sa.Column("dashboard_type", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("latest_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("latest_data_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "latest_generation_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("latest_generation_error", sa.String(), nullable=True),
        sa.Column(
            "refresh_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("86400"),
        ),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("dashboard_id"),
    )
    op.create_index("ix_dashboards_organisation_id", "dashboards", ["organisation_id"])
    op.create_index("ix_dashboards_created_at", "dashboards", ["created_at"])
    op.create_index("ix_dashboards_updated_at", "dashboards", ["updated_at"])
    op.create_index("ix_dashboards_created_by_user_id", "dashboards", ["created_by_user_id"])
    op.create_index(
        "ux_dashboards_org_key_active",
        "dashboards",
        ["organisation_id", "dashboard_key"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index(
        "ix_dashboards_refresh_lookup",
        "dashboards",
        ["organisation_id", "latest_generation_status", "latest_data_generated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dashboards_refresh_lookup", table_name="dashboards")
    op.drop_index("ux_dashboards_org_key_active", table_name="dashboards")
    op.drop_index("ix_dashboards_created_by_user_id", table_name="dashboards")
    op.drop_index("ix_dashboards_updated_at", table_name="dashboards")
    op.drop_index("ix_dashboards_created_at", table_name="dashboards")
    op.drop_index("ix_dashboards_organisation_id", table_name="dashboards")
    op.drop_table("dashboards")
