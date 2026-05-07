"""Add preference JSONB columns to public organisations and users.

Revision ID: f1a2b3c4d5ec
Revises: f1a2b3c4d5eb
Create Date: 2026-05-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a2b3c4d5ec"
down_revision = "f1a2b3c4d5eb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable-safe JSONB preferences columns for org and user settings."""
    op.add_column(
        "organisations",
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="public",
    )
    op.add_column(
        "users",
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="public",
    )


def downgrade() -> None:
    """Drop preference JSONB columns and return to schema v1 preference model."""
    op.drop_column("users", "preferences", schema="public")
    op.drop_column("organisations", "preferences", schema="public")
