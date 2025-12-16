"""Rename crm_deal_tasks to crm_activities and add polymorphic columns.

Revision ID: 3c7f79637784
Revises: 3c7f79637785
Create Date: 2025-01-16

This migration:
1. Renames crm_deal_tasks → crm_activities
2. Renames task_id → activity_id (primary key)
3. Renames task_type → activity_type
4. Adds polymorphic relationship columns (related_object_type, related_external_id, etc.)
5. Adds contact/company link columns
6. Adds meeting/call specific columns (attendees, direction, duration, etc.)
7. Updates indexes and constraints to match new naming
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "3c7f79637784"
down_revision = "3c7f79637785"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade: Rename and extend deal_tasks to activities."""

    # Step 1: Rename the table
    op.rename_table("crm_deal_tasks", "crm_activities")

    # Step 2: Rename primary key column task_id → activity_id
    op.alter_column(
        "crm_activities",
        "task_id",
        new_column_name="activity_id",
    )

    # Step 3: Rename task_type → activity_type
    op.alter_column(
        "crm_activities",
        "task_type",
        new_column_name="activity_type",
    )

    # Step 4: Add new polymorphic relationship columns
    op.add_column(
        "crm_activities",
        sa.Column("related_object_type", sa.String(32), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("related_external_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("related_local_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("related_name", sa.String(500), nullable=True),
    )

    # Step 5: Add contact linkage columns
    op.add_column(
        "crm_activities",
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_contacts.contact_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_activities",
        sa.Column("contact_external_id", sa.String(255), nullable=True),
    )

    # Step 6: Add company linkage columns
    op.add_column(
        "crm_activities",
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_companies.company_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "crm_activities",
        sa.Column("company_external_id", sa.String(255), nullable=True),
    )

    # Step 7: Add activity_subtype for provider-specific subtypes
    op.add_column(
        "crm_activities",
        sa.Column("activity_subtype", sa.String(100), nullable=True),
    )

    # Step 8: Add meeting/event timing columns
    op.add_column(
        "crm_activities",
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("duration_minutes", sa.Integer, nullable=True),
    )

    # Step 9: Add attendees JSONB for meetings
    op.add_column(
        "crm_activities",
        sa.Column(
            "attendees",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Step 10: Add communication-specific columns
    op.add_column(
        "crm_activities",
        sa.Column("direction", sa.String(32), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("call_outcome", sa.String(100), nullable=True),
    )

    # Step 11: Add is_all_day flag for events
    op.add_column(
        "crm_activities",
        sa.Column(
            "is_all_day",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Step 12: Drop old indexes (they reference old table/column names)
    # Use IF EXISTS so this is safe on dev DBs that may have drifted.
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_deal")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_org_status")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_org_type")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_org_priority")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_owner")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_due_date")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_org_due")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_status_due")
    op.execute("DROP INDEX IF EXISTS ix_crm_deal_task_synced")

    # Step 13: Drop old unique constraint and create new one
    op.execute(
        "ALTER TABLE crm_activities DROP CONSTRAINT IF EXISTS uq_crm_deal_task_external"
    )
    op.create_unique_constraint(
        "uq_crm_activity_external",
        "crm_activities",
        ["integration_id", "external_id"],
    )

    # Step 14: Create new indexes with updated naming
    op.create_index(
        "ix_crm_activity_deal",
        "crm_activities",
        ["deal_id"],
    )
    op.create_index(
        "ix_crm_activity_org_status",
        "crm_activities",
        ["organisation_id", "status"],
    )
    op.create_index(
        "ix_crm_activity_org_type",
        "crm_activities",
        ["organisation_id", "activity_type"],
    )
    op.create_index(
        "ix_crm_activity_org_priority",
        "crm_activities",
        ["organisation_id", "priority"],
    )
    op.create_index(
        "ix_crm_activity_owner",
        "crm_activities",
        ["owner_external_id"],
    )
    op.create_index(
        "ix_crm_activity_due_date",
        "crm_activities",
        ["due_date"],
    )
    op.create_index(
        "ix_crm_activity_org_due",
        "crm_activities",
        ["organisation_id", "due_date"],
    )
    op.create_index(
        "ix_crm_activity_status_due",
        "crm_activities",
        ["status", "due_date"],
    )
    op.create_index(
        "ix_crm_activity_synced",
        "crm_activities",
        ["organisation_id", "synced_at"],
    )

    # Step 15: Create new polymorphic relationship indexes
    op.create_index(
        "ix_crm_activity_related",
        "crm_activities",
        ["organisation_id", "related_object_type", "related_external_id"],
    )
    op.create_index(
        "ix_crm_activity_related_local",
        "crm_activities",
        ["related_local_id"],
    )
    op.create_index(
        "ix_crm_activity_contact",
        "crm_activities",
        ["contact_id"],
    )
    op.create_index(
        "ix_crm_activity_company",
        "crm_activities",
        ["company_id"],
    )


def downgrade() -> None:
    """Downgrade: Revert activities back to deal_tasks."""

    # Drop new indexes
    op.drop_index("ix_crm_activity_company", table_name="crm_activities")
    op.drop_index("ix_crm_activity_contact", table_name="crm_activities")
    op.drop_index("ix_crm_activity_related_local", table_name="crm_activities")
    op.drop_index("ix_crm_activity_related", table_name="crm_activities")
    op.drop_index("ix_crm_activity_synced", table_name="crm_activities")
    op.drop_index("ix_crm_activity_status_due", table_name="crm_activities")
    op.drop_index("ix_crm_activity_org_due", table_name="crm_activities")
    op.drop_index("ix_crm_activity_due_date", table_name="crm_activities")
    op.drop_index("ix_crm_activity_owner", table_name="crm_activities")
    op.drop_index("ix_crm_activity_org_priority", table_name="crm_activities")
    op.drop_index("ix_crm_activity_org_type", table_name="crm_activities")
    op.drop_index("ix_crm_activity_org_status", table_name="crm_activities")
    op.drop_index("ix_crm_activity_deal", table_name="crm_activities")

    # Drop new constraint and recreate old one
    op.drop_constraint("uq_crm_activity_external", "crm_activities", type_="unique")
    op.create_unique_constraint(
        "uq_crm_deal_task_external",
        "crm_activities",
        ["integration_id", "external_id"],
    )

    # Recreate old indexes (table still named crm_activities at this point)
    op.create_index(
        "ix_crm_deal_task_deal",
        "crm_activities",
        ["deal_id"],
    )
    op.create_index(
        "ix_crm_deal_task_org_status",
        "crm_activities",
        ["organisation_id", "status"],
    )
    op.create_index(
        "ix_crm_deal_task_org_type",
        "crm_activities",
        ["organisation_id", "activity_type"],
    )
    op.create_index(
        "ix_crm_deal_task_org_priority",
        "crm_activities",
        ["organisation_id", "priority"],
    )
    op.create_index(
        "ix_crm_deal_task_owner",
        "crm_activities",
        ["owner_external_id"],
    )
    op.create_index(
        "ix_crm_deal_task_due_date",
        "crm_activities",
        ["due_date"],
    )
    op.create_index(
        "ix_crm_deal_task_org_due",
        "crm_activities",
        ["organisation_id", "due_date"],
    )
    op.create_index(
        "ix_crm_deal_task_status_due",
        "crm_activities",
        ["status", "due_date"],
    )
    op.create_index(
        "ix_crm_deal_task_synced",
        "crm_activities",
        ["organisation_id", "synced_at"],
    )

    # Drop new columns
    op.drop_column("crm_activities", "is_all_day")
    op.drop_column("crm_activities", "call_outcome")
    op.drop_column("crm_activities", "direction")
    op.drop_column("crm_activities", "attendees")
    op.drop_column("crm_activities", "duration_minutes")
    op.drop_column("crm_activities", "end_date")
    op.drop_column("crm_activities", "activity_subtype")
    op.drop_column("crm_activities", "company_external_id")
    op.drop_column("crm_activities", "company_id")
    op.drop_column("crm_activities", "contact_external_id")
    op.drop_column("crm_activities", "contact_id")
    op.drop_column("crm_activities", "related_name")
    op.drop_column("crm_activities", "related_local_id")
    op.drop_column("crm_activities", "related_external_id")
    op.drop_column("crm_activities", "related_object_type")

    # Rename columns back
    op.alter_column(
        "crm_activities",
        "activity_type",
        new_column_name="task_type",
    )
    op.alter_column(
        "crm_activities",
        "activity_id",
        new_column_name="task_id",
    )

    # Rename table back
    op.rename_table("crm_activities", "crm_deal_tasks")
