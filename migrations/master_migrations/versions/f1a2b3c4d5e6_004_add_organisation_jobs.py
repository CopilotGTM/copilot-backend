"""Add organisation_jobs table for async job tracking.

Revision ID: f1a2b3c4d5e6
Revises: e0c08621b04a
Create Date: 2025-12-23

This migration creates the organisation_jobs table in the master (public) schema
to support async job-based organisation creation with SSE streaming.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "f1a2b3c4d5e6"
down_revision = "e0c08621b04a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organisation_jobs",
        sa.Column("job_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), nullable=False),
        sa.Column("organisation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("organisation_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("progress", sa.Integer, server_default="0", nullable=False),
        sa.Column("status_message", sa.String(500), nullable=True),
        sa.Column("result_data", JSONB, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("attempt_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("max_retries", sa.Integer, server_default="3", nullable=False),
        sa.Column("request_payload", JSONB, server_default="{}", nullable=False),
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

    # Index for admin's recent jobs
    op.create_index(
        "ix_organisation_jobs_admin_created",
        "organisation_jobs",
        ["admin_id", "created_at"],
        schema="public",
    )

    # Index for admin lookup
    op.create_index(
        "ix_organisation_jobs_admin_id",
        "organisation_jobs",
        ["admin_id"],
        schema="public",
    )

    # Index for organisation lookup
    op.create_index(
        "ix_organisation_jobs_organisation_id",
        "organisation_jobs",
        ["organisation_id"],
        schema="public",
    )

    # Index for pending/in_progress jobs (for monitoring)
    op.create_index(
        "ix_organisation_jobs_status",
        "organisation_jobs",
        ["status"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organisation_jobs_status", table_name="organisation_jobs", schema="public"
    )
    op.drop_index(
        "ix_organisation_jobs_organisation_id",
        table_name="organisation_jobs",
        schema="public",
    )
    op.drop_index(
        "ix_organisation_jobs_admin_id",
        table_name="organisation_jobs",
        schema="public",
    )
    op.drop_index(
        "ix_organisation_jobs_admin_created",
        table_name="organisation_jobs",
        schema="public",
    )
    op.drop_table("organisation_jobs", schema="public")
