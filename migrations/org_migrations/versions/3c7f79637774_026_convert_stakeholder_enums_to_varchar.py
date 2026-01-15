"""Convert PostgreSQL ENUM columns to VARCHAR for flexible schema evolution.

Revision ID: 3c7f79637774
Revises: 3c7f79637775
Create Date: 2026-01-15 12:00:00.000000

This migration converts PostgreSQL ENUM columns to VARCHAR(64) to allow
more flexible schema evolution without requiring migrations for each
enum value change.

Tables and columns converted:

1. deal_stakeholders:
   - stance (stakeholderstance -> VARCHAR)
   - decision_power (decisionpower -> VARCHAR)
   - temperature (stakeholdertemperature -> VARCHAR)
   - source (stakeholdersource -> VARCHAR)
   - approval_status (approvalstatus -> VARCHAR)

2. calendar_event_deal_associations:
   - status (associationstatus -> VARCHAR)
   - meeting_prep_status (generationstatus -> VARCHAR)
   - meeting_summary_status (generationstatus -> VARCHAR)
   - meeting_followup_status (generationstatus -> VARCHAR)

Application-level validation is still enforced via Python Enum classes
in the SQLModel/Pydantic layer.
"""

from typing import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637774"
down_revision: str | None = "3c7f79637775"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert ENUM columns to VARCHAR for flexibility.

    Strategy:
    1. Drop any server defaults that reference enum types
    2. Convert column types from ENUM to VARCHAR
    3. Set new string-based server defaults
    4. Drop the now-unused ENUM types
    """

    # =========================================================================
    # DEAL_STAKEHOLDERS TABLE
    # Step 1: Drop all enum-typed defaults first
    # =========================================================================
    op.execute("ALTER TABLE deal_stakeholders ALTER COLUMN approval_status DROP DEFAULT")

    # =========================================================================
    # Step 2: Convert all enum columns to VARCHAR
    # =========================================================================
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN stance TYPE VARCHAR(64) USING stance::text,
        ALTER COLUMN decision_power TYPE VARCHAR(64) USING decision_power::text,
        ALTER COLUMN temperature TYPE VARCHAR(64) USING temperature::text,
        ALTER COLUMN source TYPE VARCHAR(64) USING source::text,
        ALTER COLUMN approval_status TYPE VARCHAR(64) USING approval_status::text
        """
    )

    # =========================================================================
    # Step 3: Set new string-based defaults
    # =========================================================================
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN stance SET DEFAULT 'UNKNOWN',
        ALTER COLUMN decision_power SET DEFAULT 'UNKNOWN',
        ALTER COLUMN temperature SET DEFAULT 'UNKNOWN',
        ALTER COLUMN source SET DEFAULT 'CALENDAR_DISCOVERY',
        ALTER COLUMN approval_status SET DEFAULT 'APPROVED'
        """
    )

    # =========================================================================
    # CALENDAR_EVENT_DEAL_ASSOCIATIONS TABLE
    # Step 1: Drop any enum-typed defaults (if they exist)
    # =========================================================================
    # These columns may not have server defaults, but DROP DEFAULT IF EXISTS
    # is not supported, so we use a DO block to handle gracefully
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE calendar_event_deal_associations ALTER COLUMN status DROP DEFAULT;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE calendar_event_deal_associations ALTER COLUMN meeting_prep_status DROP DEFAULT;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE calendar_event_deal_associations ALTER COLUMN meeting_summary_status DROP DEFAULT;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE calendar_event_deal_associations ALTER COLUMN meeting_followup_status DROP DEFAULT;
        EXCEPTION WHEN OTHERS THEN NULL;
        END $$;
        """
    )

    # =========================================================================
    # Step 2: Convert all enum columns to VARCHAR
    # =========================================================================
    op.execute(
        """
        ALTER TABLE calendar_event_deal_associations
        ALTER COLUMN status TYPE VARCHAR(32) USING status::text,
        ALTER COLUMN meeting_prep_status TYPE VARCHAR(32) USING meeting_prep_status::text,
        ALTER COLUMN meeting_summary_status TYPE VARCHAR(32) USING meeting_summary_status::text,
        ALTER COLUMN meeting_followup_status TYPE VARCHAR(32) USING meeting_followup_status::text
        """
    )

    # =========================================================================
    # Step 3: Set new string-based defaults
    # =========================================================================
    op.execute(
        """
        ALTER TABLE calendar_event_deal_associations
        ALTER COLUMN status SET DEFAULT 'ACTIVE',
        ALTER COLUMN meeting_prep_status SET DEFAULT 'NOT_STARTED',
        ALTER COLUMN meeting_summary_status SET DEFAULT 'NOT_STARTED',
        ALTER COLUMN meeting_followup_status SET DEFAULT 'NOT_STARTED'
        """
    )

    # =========================================================================
    # Step 4: Drop the PostgreSQL ENUM types (no longer needed)
    # =========================================================================
    op.execute("DROP TYPE IF EXISTS stakeholderstance")
    op.execute("DROP TYPE IF EXISTS decisionpower")
    op.execute("DROP TYPE IF EXISTS stakeholdertemperature")
    op.execute("DROP TYPE IF EXISTS stakeholdersource")
    op.execute("DROP TYPE IF EXISTS approvalstatus")
    op.execute("DROP TYPE IF EXISTS associationstatus")
    op.execute("DROP TYPE IF EXISTS generationstatus")


def downgrade() -> None:
    """Convert VARCHAR columns back to PostgreSQL ENUM types.

    Note: This assumes the VARCHAR values match the original enum values.
    Invalid values will cause this migration to fail.
    """

    # =========================================================================
    # Recreate the PostgreSQL ENUM types
    # =========================================================================
    op.execute(
        """
        CREATE TYPE stakeholderstance AS ENUM (
            'UNKNOWN', 'CHAMPION', 'SUPPORTER', 'NEUTRAL', 'DETRACTOR', 'BLOCKER'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE decisionpower AS ENUM (
            'UNKNOWN', 'DECISION_MAKER', 'INFLUENCER', 'EVALUATOR', 'GATEKEEPER', 'END_USER'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE stakeholdertemperature AS ENUM (
            'UNKNOWN', 'HOT', 'WARM', 'COLD'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE stakeholdersource AS ENUM (
            'CRM', 'CALENDAR_DISCOVERY', 'EMAIL_DISCOVERY', 'MANUAL'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE approvalstatus AS ENUM (
            'APPROVED', 'PENDING', 'REJECTED', 'REMOVED'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE associationstatus AS ENUM (
            'SUGGESTED', 'CONFIRMED', 'REJECTED', 'ACTIVE', 'REMOVED', 'CANCELLED'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE generationstatus AS ENUM (
            'NOT_STARTED', 'GENERATING', 'COMPLETED', 'FAILED'
        )
        """
    )

    # =========================================================================
    # Convert deal_stakeholders columns back to ENUM types
    # =========================================================================
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN stance TYPE stakeholderstance
        USING stance::stakeholderstance
        """
    )
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN decision_power TYPE decisionpower
        USING decision_power::decisionpower
        """
    )
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN temperature TYPE stakeholdertemperature
        USING temperature::stakeholdertemperature
        """
    )
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN source TYPE stakeholdersource
        USING source::stakeholdersource
        """
    )
    op.execute(
        """
        ALTER TABLE deal_stakeholders
        ALTER COLUMN approval_status TYPE approvalstatus
        USING approval_status::approvalstatus
        """
    )

    # =========================================================================
    # Convert calendar_event_deal_associations columns back to ENUM types
    # =========================================================================
    op.execute(
        """
        ALTER TABLE calendar_event_deal_associations
        ALTER COLUMN status TYPE associationstatus
        USING status::associationstatus
        """
    )
    op.execute(
        """
        ALTER TABLE calendar_event_deal_associations
        ALTER COLUMN meeting_prep_status TYPE generationstatus
        USING meeting_prep_status::generationstatus
        """
    )
    op.execute(
        """
        ALTER TABLE calendar_event_deal_associations
        ALTER COLUMN meeting_summary_status TYPE generationstatus
        USING meeting_summary_status::generationstatus
        """
    )
    op.execute(
        """
        ALTER TABLE calendar_event_deal_associations
        ALTER COLUMN meeting_followup_status TYPE generationstatus
        USING meeting_followup_status::generationstatus
        """
    )
