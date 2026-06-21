"""Drop legacy task and note payload columns from crm_activities.

Revision ID: 3c7f79637733
Revises: 3c7f79637734
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637733"
down_revision: str | None = "3c7f79637734"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove columns whose canonical data now lives in crm_tasks/crm_notes."""
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_org_status")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_org_priority")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_due_date")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_org_due")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_status_due")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_source")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_created_by")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_task_owner_due")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_task_created_by_due")
    op.execute("DROP INDEX IF EXISTS ix_crm_activity_task_external_email_due")

    op.drop_column("crm_activities", "metrics")
    op.drop_column("crm_activities", "properties")
    op.drop_column("crm_activities", "is_all_day")
    op.drop_column("crm_activities", "last_seen_at")
    op.drop_column("crm_activities", "synced_at")
    op.drop_column("crm_activities", "source_modified_at")
    op.drop_column("crm_activities", "source_created_at")
    op.drop_column("crm_activities", "call_outcome")
    op.drop_column("crm_activities", "direction")
    op.drop_column("crm_activities", "attendees")
    op.drop_column("crm_activities", "duration_minutes")
    op.drop_column("crm_activities", "reminder_date")
    op.drop_column("crm_activities", "completed_date")
    op.drop_column("crm_activities", "end_date")
    op.drop_column("crm_activities", "due_date")
    op.drop_column("crm_activities", "created_by_user_id")
    op.drop_column("crm_activities", "source")
    op.drop_column("crm_activities", "external_assignee_name")
    op.drop_column("crm_activities", "external_assignee_email")
    op.drop_column("crm_activities", "owner_id")
    op.drop_column("crm_activities", "owner_email")
    op.drop_column("crm_activities", "owner_name")
    op.drop_column("crm_activities", "activity_subtype")
    op.drop_column("crm_activities", "priority")
    op.drop_column("crm_activities", "status")
    op.drop_column("crm_activities", "body")


def downgrade() -> None:
    """Restore legacy columns without restoring migrated payload values."""
    op.add_column("crm_activities", sa.Column("body", sa.Text(), nullable=True))
    op.add_column("crm_activities", sa.Column("status", sa.String(length=32), nullable=True))
    op.add_column("crm_activities", sa.Column("priority", sa.String(length=32), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column("activity_subtype", sa.String(length=100), nullable=True),
    )
    op.add_column("crm_activities", sa.Column("owner_name", sa.String(length=255), nullable=True))
    op.add_column("crm_activities", sa.Column("owner_email", sa.String(length=255), nullable=True))
    op.add_column("crm_activities", sa.Column("owner_id", sa.UUID(), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column("external_assignee_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("external_assignee_name", sa.String(length=255), nullable=True),
    )
    op.add_column("crm_activities", sa.Column("source", sa.String(length=50), nullable=True))
    op.add_column("crm_activities", sa.Column("created_by_user_id", sa.UUID(), nullable=True))
    op.add_column("crm_activities", sa.Column("due_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("crm_activities", sa.Column("end_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column("completed_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("reminder_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("crm_activities", sa.Column("duration_minutes", sa.Integer(), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column(
            "attendees",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("crm_activities", sa.Column("direction", sa.String(length=32), nullable=True))
    op.add_column("crm_activities", sa.Column("call_outcome", sa.String(length=100), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("crm_activities", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column("is_all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "crm_activities",
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "crm_activities",
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_index("ix_crm_activity_org_status", "crm_activities", ["organisation_id", "status"])
    op.create_index("ix_crm_activity_org_priority", "crm_activities", ["organisation_id", "priority"])
    op.create_index("ix_crm_activity_due_date", "crm_activities", ["due_date"])
    op.create_index("ix_crm_activity_org_due", "crm_activities", ["organisation_id", "due_date"])
    op.create_index("ix_crm_activity_status_due", "crm_activities", ["status", "due_date"])
    op.create_index("ix_crm_activity_source", "crm_activities", ["organisation_id", "source"])
    op.create_index(
        "ix_crm_activity_created_by",
        "crm_activities",
        ["organisation_id", "created_by_user_id"],
    )
    op.create_index(
        "ix_crm_activity_task_owner_due",
        "crm_activities",
        ["organisation_id", "owner_id", "due_date"],
        postgresql_where=sa.text(
            "is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO') "
            "AND external_assignee_email IS NULL AND external_assignee_name IS NULL"
        ),
    )
    op.create_index(
        "ix_crm_activity_task_created_by_due",
        "crm_activities",
        ["organisation_id", "created_by_user_id", "due_date"],
        postgresql_where=sa.text("is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO')"),
    )
    op.create_index(
        "ix_crm_activity_task_external_email_due",
        "crm_activities",
        ["organisation_id", "external_assignee_email", "due_date"],
        postgresql_where=sa.text(
            "is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO') "
            "AND external_assignee_email IS NOT NULL"
        ),
    )
