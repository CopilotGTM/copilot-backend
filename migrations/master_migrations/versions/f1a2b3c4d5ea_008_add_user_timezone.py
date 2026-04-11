"""Add timezone column to public.users.

Revision ID: f1a2b3c4d5ea
Revises: f1a2b3c4d5e9
Create Date: 2026-04-11
"""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5ea"
down_revision = "f1a2b3c4d5e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("timezone", sa.String(length=64), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("users", "timezone", schema="public")
