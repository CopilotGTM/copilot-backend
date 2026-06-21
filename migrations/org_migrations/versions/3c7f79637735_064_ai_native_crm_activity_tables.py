"""Add AI-native CRM activity feed, task, note, and link tables.

Revision ID: 3c7f79637735
Revises: 3c7f79637736
Create Date: 2026-06-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637735"
down_revision: str | None = "3c7f79637736"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create source tables and the link index used by per-object activity feeds."""
    op.add_column("crm_activities", sa.Column("title", sa.String(length=500), nullable=True))
    op.add_column("crm_activities", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("crm_activities", sa.Column("preview", sa.String(length=1000), nullable=True))
    op.add_column("crm_activities", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("crm_activities", sa.Column("source_type", sa.String(length=64), nullable=True))
    op.add_column("crm_activities", sa.Column("source_record_id", sa.Text(), nullable=True))
    op.add_column("crm_activities", sa.Column("source_provider", sa.String(length=64), nullable=True))
    op.add_column("crm_activities", sa.Column("source_integration_id", sa.UUID(), nullable=True))
    op.add_column("crm_activities", sa.Column("source_url", sa.String(length=2048), nullable=True))
    op.add_column("crm_activities", sa.Column("actor_name", sa.String(length=255), nullable=True))
    op.add_column("crm_activities", sa.Column("actor_email", sa.String(length=255), nullable=True))
    op.add_column(
        "crm_activities",
        sa.Column(
            "display_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Small timeline display snapshot; validate known shapes with Pydantic "
                "before writing."
            ),
        ),
    )
    op.create_foreign_key(
        "fk_crm_activities_source_integration_id",
        "crm_activities",
        "knowledge_integrations",
        ["source_integration_id"],
        ["integration_id"],
    )
    op.create_unique_constraint(
        "uq_crm_activity_org_activity",
        "crm_activities",
        ["organisation_id", "activity_id"],
    )
    op.create_index(
        "ix_crm_activity_org_occurred",
        "crm_activities",
        ["organisation_id", "occurred_at"],
    )

    op.create_table(
        "crm_tasks",
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'NOT_STARTED'"),
        ),
        sa.Column(
            "priority",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'NONE'"),
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to_user_id", sa.UUID(), nullable=True),
        sa.Column("assigned_to_email", sa.String(length=255), nullable=True),
        sa.Column("assigned_to_name", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("owner_email", sa.String(length=255), nullable=True),
        sa.Column("completed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "task_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["assigned_to_user_id"], ["crm_users.user_id"]),
        sa.ForeignKeyConstraint(["completed_by_user_id"], ["crm_users.user_id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"]),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["crm_users.user_id"]),
        sa.CheckConstraint(
            "status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')",
            name="ck_crm_task_status",
        ),
        sa.CheckConstraint(
            "priority IN ('NONE', 'LOW', 'MEDIUM', 'HIGH', 'URGENT')",
            name="ck_crm_task_priority",
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_crm_tasks_organisation_id", "crm_tasks", ["organisation_id"])
    op.create_index("ix_crm_tasks_integration_id", "crm_tasks", ["integration_id"])
    op.create_index("ix_crm_tasks_created_at", "crm_tasks", ["created_at"])
    op.create_index("ix_crm_tasks_updated_at", "crm_tasks", ["updated_at"])
    op.create_index("ix_crm_task_org_due", "crm_tasks", ["organisation_id", "due_at", "task_id"])
    op.create_index(
        "ix_crm_task_org_due_date",
        "crm_tasks",
        ["organisation_id", "due_date", "task_id"],
    )
    op.create_index(
        "ix_crm_task_deal",
        "crm_tasks",
        ["organisation_id", "deal_id", "due_at", "task_id"],
        postgresql_where=sa.text("deal_id IS NOT NULL AND is_deleted IS NOT TRUE"),
    )
    op.create_index(
        "ix_crm_task_assignee_due",
        "crm_tasks",
        ["organisation_id", "assigned_to_user_id", "due_at"],
    )
    op.create_index(
        "ix_crm_task_status_due",
        "crm_tasks",
        ["organisation_id", "status", "due_at"],
    )

    op.create_table(
        "crm_notes",
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "body_format",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'plain_text'"),
        ),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("author_email", sa.String(length=255), nullable=True),
        sa.Column(
            "visibility",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'TEAM'"),
        ),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "note_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["crm_users.user_id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"]),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.CheckConstraint(
            "body_format IN ('plain_text', 'markdown', 'html')",
            name="ck_crm_note_body_format",
        ),
        sa.CheckConstraint(
            "visibility IN ('PRIVATE', 'TEAM', 'ORG')",
            name="ck_crm_note_visibility",
        ),
        sa.PrimaryKeyConstraint("note_id"),
    )
    op.create_index("ix_crm_notes_organisation_id", "crm_notes", ["organisation_id"])
    op.create_index("ix_crm_notes_integration_id", "crm_notes", ["integration_id"])
    op.create_index("ix_crm_notes_created_at", "crm_notes", ["created_at"])
    op.create_index("ix_crm_notes_updated_at", "crm_notes", ["updated_at"])
    op.create_index("ix_crm_note_author", "crm_notes", ["organisation_id", "author_user_id"])
    op.create_index(
        "ix_crm_note_deal",
        "crm_notes",
        ["organisation_id", "deal_id", "created_at"],
        postgresql_where=sa.text("deal_id IS NOT NULL AND is_deleted IS NOT TRUE"),
    )
    op.create_index("ix_crm_note_visibility", "crm_notes", ["organisation_id", "visibility"])

    op.create_table(
        "crm_activity_links",
        sa.Column("activity_link_id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("activity_id", sa.UUID(), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("object_id", sa.UUID(), nullable=False),
        sa.Column(
            "link_role",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'RELATED'"),
        ),
        sa.Column(
            "link_source",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'SYSTEM'"),
        ),
        sa.Column("confidence_score", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organisation_id", "activity_id"],
            ["crm_activities.organisation_id", "crm_activities.activity_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="ck_crm_activity_link_confidence_score_range",
        ),
        sa.CheckConstraint(
            "object_type IN ('DEAL', 'COMPANY', 'CONTACT', 'LEAD', 'USER', 'RISK', 'TASK', 'NOTE')",
            name="ck_crm_activity_link_object_type",
        ),
        sa.CheckConstraint(
            "link_role IN ("
            "'PRIMARY', 'SENDER', 'RECIPIENT', 'ATTENDEE', 'OWNER', 'MENTIONED', "
            "'RELATED', 'CREATED', 'RESOLVED', 'ASSIGNEE'"
            ")",
            name="ck_crm_activity_link_role",
        ),
        sa.CheckConstraint(
            "link_source IN ("
            "'MANUAL', 'PROVIDER_ASSOCIATION', 'RULE_MATCH', 'AI_MATCH', 'SYSTEM'"
            ")",
            name="ck_crm_activity_link_source",
        ),
        sa.PrimaryKeyConstraint("activity_link_id"),
        sa.UniqueConstraint(
            "organisation_id",
            "activity_id",
            "object_type",
            "object_id",
            "link_role",
            name="uq_crm_activity_link_target_role",
        ),
    )
    op.create_index("ix_crm_activity_links_organisation_id", "crm_activity_links", ["organisation_id"])
    op.create_index("ix_crm_activity_links_activity_id", "crm_activity_links", ["activity_id"])
    op.create_index("ix_crm_activity_links_object_id", "crm_activity_links", ["object_id"])
    op.create_index(
        "ix_crm_activity_links_activity",
        "crm_activity_links",
        ["organisation_id", "activity_id"],
    )
    op.create_index(
        "ix_crm_activity_links_object_time",
        "crm_activity_links",
        ["organisation_id", "object_type", "object_id", "occurred_at", "activity_id"],
    )


def downgrade() -> None:
    """Remove the activity feed source/link schema."""
    op.drop_index("ix_crm_activity_links_object_time", table_name="crm_activity_links")
    op.drop_index("ix_crm_activity_links_activity", table_name="crm_activity_links")
    op.drop_index("ix_crm_activity_links_object_id", table_name="crm_activity_links")
    op.drop_index("ix_crm_activity_links_activity_id", table_name="crm_activity_links")
    op.drop_index("ix_crm_activity_links_organisation_id", table_name="crm_activity_links")
    op.drop_table("crm_activity_links")

    op.drop_index("ix_crm_note_visibility", table_name="crm_notes")
    op.drop_index("ix_crm_note_deal", table_name="crm_notes")
    op.drop_index("ix_crm_note_author", table_name="crm_notes")
    op.drop_index("ix_crm_notes_updated_at", table_name="crm_notes")
    op.drop_index("ix_crm_notes_created_at", table_name="crm_notes")
    op.drop_index("ix_crm_notes_integration_id", table_name="crm_notes")
    op.drop_index("ix_crm_notes_organisation_id", table_name="crm_notes")
    op.drop_table("crm_notes")

    op.drop_index("ix_crm_task_status_due", table_name="crm_tasks")
    op.drop_index("ix_crm_task_assignee_due", table_name="crm_tasks")
    op.drop_index("ix_crm_task_deal", table_name="crm_tasks")
    op.drop_index("ix_crm_task_org_due", table_name="crm_tasks")
    op.drop_index("ix_crm_task_org_due_date", table_name="crm_tasks")
    op.drop_index("ix_crm_tasks_updated_at", table_name="crm_tasks")
    op.drop_index("ix_crm_tasks_created_at", table_name="crm_tasks")
    op.drop_index("ix_crm_tasks_integration_id", table_name="crm_tasks")
    op.drop_index("ix_crm_tasks_organisation_id", table_name="crm_tasks")
    op.drop_table("crm_tasks")

    op.drop_index("ix_crm_activity_org_occurred", table_name="crm_activities")
    op.drop_constraint(
        "uq_crm_activity_org_activity",
        "crm_activities",
        type_="unique",
    )
    op.drop_constraint(
        "fk_crm_activities_source_integration_id",
        "crm_activities",
        type_="foreignkey",
    )
    op.drop_column("crm_activities", "display_payload")
    op.drop_column("crm_activities", "actor_email")
    op.drop_column("crm_activities", "actor_name")
    op.drop_column("crm_activities", "source_url")
    op.drop_column("crm_activities", "source_integration_id")
    op.drop_column("crm_activities", "source_provider")
    op.drop_column("crm_activities", "source_record_id")
    op.drop_column("crm_activities", "source_type")
    op.drop_column("crm_activities", "occurred_at")
    op.drop_column("crm_activities", "preview")
    op.drop_column("crm_activities", "summary")
    op.drop_column("crm_activities", "title")
