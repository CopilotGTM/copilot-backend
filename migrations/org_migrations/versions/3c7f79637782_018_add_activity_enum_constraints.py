"""Add check constraints for canonical activity enums on crm_activities.

Revision ID: 3c7f79637782
Revises: 3c7f79637783
Create Date: 2025-12-16

This migration:
1. Adds CHECK constraints for status, activity_type, and priority columns
   enforcing canonical enum values for cross-CRM analytics consistency.

The constraints allow NULL (since provider values may be missing) but enforce
that when present, values match the canonical CRMActivityStatus, CRMActivityType,
and CRMActivityPriority enums.

Note: Existing non-canonical values are normalized to canonical equivalents
before adding constraints.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "3c7f79637782"
down_revision = "3c7f79637783"
branch_labels = None
depends_on = None


# Canonical enum values
ACTIVITY_STATUSES = [
    "NOT_STARTED",
    "IN_PROGRESS",
    "COMPLETED",
    "WAITING",
    "DEFERRED",
    "CANCELLED",
]

ACTIVITY_TYPES = [
    "TASK",
    "TODO",
    "EMAIL",
    "CALL",
    "MEETING",
    "NOTE",
    "EVENT",
    "SMS",
    "LINKEDIN_MESSAGE",
    "CHAT",
    "OTHER",
]

ACTIVITY_PRIORITIES = [
    "NONE",
    "LOW",
    "MEDIUM",
    "HIGH",
    "URGENT",
]


def upgrade() -> None:
    """Upgrade: Normalize existing values and add enum CHECK constraints."""

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Normalize existing status values to canonical form
    # ─────────────────────────────────────────────────────────────────────────
    # Preserve unknown raw statuses for debugging/forensics (do not block migration).
    op.execute(
        """
        UPDATE crm_activities
        SET properties = properties || jsonb_build_object('_raw_activity_status', status)
        WHERE status IS NOT NULL
          AND UPPER(status) NOT IN (
            'NOT_STARTED',
            'NOT STARTED',
            'IN_PROGRESS',
            'IN PROGRESS',
            'COMPLETED',
            'WAITING',
            'WAITING ON SOMEONE ELSE',
            'WAITING FOR INPUT',
            'DEFERRED',
            'CANCELLED',
            'CANCELED'
          )
        """
    )

    # Map known values → canonical; unknown → NULL (so CHECK constraints will pass).
    op.execute(
        """
        UPDATE crm_activities
        SET status = CASE UPPER(status)
            WHEN 'NOT_STARTED' THEN 'NOT_STARTED'
            WHEN 'NOT STARTED' THEN 'NOT_STARTED'
            WHEN 'IN_PROGRESS' THEN 'IN_PROGRESS'
            WHEN 'IN PROGRESS' THEN 'IN_PROGRESS'
            WHEN 'COMPLETED' THEN 'COMPLETED'
            WHEN 'WAITING' THEN 'WAITING'
            WHEN 'WAITING ON SOMEONE ELSE' THEN 'WAITING'
            WHEN 'WAITING FOR INPUT' THEN 'WAITING'
            WHEN 'DEFERRED' THEN 'DEFERRED'
            WHEN 'CANCELLED' THEN 'CANCELLED'
            WHEN 'CANCELED' THEN 'CANCELLED'
            ELSE NULL
        END
        WHERE status IS NOT NULL
        """
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Normalize existing activity_type values to canonical form
    # ─────────────────────────────────────────────────────────────────────────
    # Preserve unknown raw types in activity_subtype for future analysis.
    op.execute(
        """
        UPDATE crm_activities
        SET activity_subtype = COALESCE(activity_subtype, activity_type)
        WHERE activity_type IS NOT NULL
          AND UPPER(activity_type) NOT IN (
            'TASK', 'TODO', 'EMAIL', 'CALL', 'MEETING', 'NOTE', 'EVENT',
            'SMS', 'LINKEDIN_MESSAGE', 'CHAT', 'OTHER',
            'DEADLINE', 'LUNCH'
          )
        """
    )

    op.execute(
        """
        UPDATE crm_activities
        SET activity_type = CASE UPPER(activity_type)
            WHEN 'TASK' THEN 'TASK'
            WHEN 'TODO' THEN 'TODO'
            WHEN 'EMAIL' THEN 'EMAIL'
            WHEN 'CALL' THEN 'CALL'
            WHEN 'MEETING' THEN 'MEETING'
            WHEN 'NOTE' THEN 'NOTE'
            WHEN 'EVENT' THEN 'EVENT'
            WHEN 'SMS' THEN 'SMS'
            WHEN 'LINKEDIN_MESSAGE' THEN 'LINKEDIN_MESSAGE'
            WHEN 'CHAT' THEN 'CHAT'
            WHEN 'DEADLINE' THEN 'TASK'
            WHEN 'LUNCH' THEN 'MEETING'
            ELSE 'OTHER'
        END
        WHERE activity_type IS NOT NULL
        """
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Normalize existing priority values to canonical form
    # ─────────────────────────────────────────────────────────────────────────
    # Preserve unknown raw priorities for debugging/forensics.
    op.execute(
        """
        UPDATE crm_activities
        SET properties = properties || jsonb_build_object('_raw_activity_priority', priority)
        WHERE priority IS NOT NULL
          AND UPPER(priority) NOT IN (
            'NONE', 'LOW', 'LOWEST', 'MEDIUM', 'NORMAL', 'HIGH', 'HIGHEST', 'URGENT'
          )
        """
    )

    op.execute(
        """
        UPDATE crm_activities
        SET priority = CASE UPPER(priority)
            WHEN 'NONE' THEN 'NONE'
            WHEN 'LOW' THEN 'LOW'
            WHEN 'LOWEST' THEN 'LOW'
            WHEN 'MEDIUM' THEN 'MEDIUM'
            WHEN 'NORMAL' THEN 'MEDIUM'
            WHEN 'HIGH' THEN 'HIGH'
            WHEN 'HIGHEST' THEN 'URGENT'
            WHEN 'URGENT' THEN 'URGENT'
            ELSE 'NONE'
        END
        WHERE priority IS NOT NULL
        """
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: Add CHECK constraints for canonical enum values
    # ─────────────────────────────────────────────────────────────────────────
    status_values = ", ".join(f"'{s}'" for s in ACTIVITY_STATUSES)
    op.execute(
        f"""
        ALTER TABLE crm_activities
        ADD CONSTRAINT ck_crm_activities_status
        CHECK (status IS NULL OR status IN ({status_values}))
        """
    )

    type_values = ", ".join(f"'{t}'" for t in ACTIVITY_TYPES)
    op.execute(
        f"""
        ALTER TABLE crm_activities
        ADD CONSTRAINT ck_crm_activities_activity_type
        CHECK (activity_type IS NULL OR activity_type IN ({type_values}))
        """
    )

    priority_values = ", ".join(f"'{p}'" for p in ACTIVITY_PRIORITIES)
    op.execute(
        f"""
        ALTER TABLE crm_activities
        ADD CONSTRAINT ck_crm_activities_priority
        CHECK (priority IS NULL OR priority IN ({priority_values}))
        """
    )


def downgrade() -> None:
    """Downgrade: Remove CHECK constraints (data normalization is not reversed)."""

    op.execute("ALTER TABLE crm_activities DROP CONSTRAINT IF EXISTS ck_crm_activities_status")
    op.execute(
        "ALTER TABLE crm_activities DROP CONSTRAINT IF EXISTS ck_crm_activities_activity_type"
    )
    op.execute("ALTER TABLE crm_activities DROP CONSTRAINT IF EXISTS ck_crm_activities_priority")
