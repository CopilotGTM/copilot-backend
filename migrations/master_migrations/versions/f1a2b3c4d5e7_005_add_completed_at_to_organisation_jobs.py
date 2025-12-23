"""Add completed_at column to organisation_jobs table.

Revision ID: f1a2b3c4d5e7
Revises: f1a2b3c4d5e6
Create Date: 2025-12-23

This migration adds the completed_at timestamp column to track when jobs
reach terminal state (completed, failed, cancelled).
"""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organisation_jobs",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("organisation_jobs", "completed_at", schema="public")
