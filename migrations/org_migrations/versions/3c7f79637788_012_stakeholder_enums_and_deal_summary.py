"""012 update stakeholder enums and add deal_summary_data

Revision ID: 3c7f79637788
Revises: 3c7f79637789
Create Date: 2025-12-03 10:00:00.000000

This migration:
1. Updates stakeholder enums to support richer analysis:
   - stakeholderstance: Add CHAMPION, DETRACTOR values for better stance tracking
   - decisionpower: Replace HIGH/MEDIUM/LOW with role-based values (DECISION_MAKER, INFLUENCER, etc.)
2. Adds deal_summary_data JSONB column to crm_deals for AI-generated deal summaries

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "3c7f79637788"
down_revision: Union[str, None] = "3c7f79637789"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - update stakeholder enums and add deal_summary_data."""

    # ========================================
    # 1. Update stakeholder enums
    # ========================================

    # 1a. Update stakeholderstance enum - add CHAMPION and DETRACTOR
    op.execute("ALTER TYPE stakeholderstance ADD VALUE IF NOT EXISTS 'CHAMPION'")
    op.execute("ALTER TYPE stakeholderstance ADD VALUE IF NOT EXISTS 'DETRACTOR'")

    # 1b. Update decisionpower enum - need to replace values entirely
    # Create new enum type with the desired values
    op.execute(
        """
        CREATE TYPE decisionpower_new AS ENUM (
            'UNKNOWN', 'DECISION_MAKER', 'INFLUENCER', 'EVALUATOR', 'GATEKEEPER', 'END_USER'
        )
    """
    )

    # Map old values to new values and update the column
    # HIGH -> DECISION_MAKER, MEDIUM -> INFLUENCER, LOW -> END_USER, UNKNOWN -> UNKNOWN
    op.execute(
        """
        ALTER TABLE deal_stakeholders 
        ALTER COLUMN decision_power TYPE decisionpower_new 
        USING CASE decision_power::text
            WHEN 'HIGH' THEN 'DECISION_MAKER'::decisionpower_new
            WHEN 'MEDIUM' THEN 'INFLUENCER'::decisionpower_new
            WHEN 'LOW' THEN 'END_USER'::decisionpower_new
            WHEN 'UNKNOWN' THEN 'UNKNOWN'::decisionpower_new
            ELSE 'UNKNOWN'::decisionpower_new
        END
    """
    )

    # Drop old enum type and rename new enum
    op.execute("DROP TYPE decisionpower")
    op.execute("ALTER TYPE decisionpower_new RENAME TO decisionpower")

    # ========================================
    # 2. Add deal_summary_data column
    # ========================================
    op.add_column(
        "crm_deals",
        sa.Column(
            "deal_summary_data",
            JSONB,
            nullable=True,
            comment="AI-generated deal summary data with schema_version, generated_at, and summary content.",
        ),
    )


def downgrade() -> None:
    """Downgrade schema - revert stakeholder enums and remove deal_summary_data."""

    # ========================================
    # 1. Remove deal_summary_data column
    # ========================================
    op.drop_column("crm_deals", "deal_summary_data")

    # ========================================
    # 2. Revert stakeholder enums
    # ========================================

    # 2a. Revert decisionpower enum
    op.execute(
        """
        CREATE TYPE decisionpower_old AS ENUM ('HIGH', 'MEDIUM', 'LOW', 'UNKNOWN')
    """
    )

    op.execute(
        """
        ALTER TABLE deal_stakeholders 
        ALTER COLUMN decision_power TYPE decisionpower_old 
        USING CASE decision_power::text
            WHEN 'DECISION_MAKER' THEN 'HIGH'::decisionpower_old
            WHEN 'INFLUENCER' THEN 'MEDIUM'::decisionpower_old
            WHEN 'EVALUATOR' THEN 'MEDIUM'::decisionpower_old
            WHEN 'GATEKEEPER' THEN 'MEDIUM'::decisionpower_old
            WHEN 'END_USER' THEN 'LOW'::decisionpower_old
            WHEN 'UNKNOWN' THEN 'UNKNOWN'::decisionpower_old
            ELSE 'UNKNOWN'::decisionpower_old
        END
    """
    )

    op.execute("DROP TYPE decisionpower")
    op.execute("ALTER TYPE decisionpower_old RENAME TO decisionpower")

    # 2b. Revert stakeholderstance enum (recreate without CHAMPION and DETRACTOR)
    op.execute(
        """
        CREATE TYPE stakeholderstance_old AS ENUM ('SUPPORTER', 'NEUTRAL', 'BLOCKER', 'UNKNOWN')
    """
    )

    op.execute(
        """
        ALTER TABLE deal_stakeholders 
        ALTER COLUMN stance TYPE stakeholderstance_old 
        USING CASE stance::text
            WHEN 'CHAMPION' THEN 'SUPPORTER'::stakeholderstance_old
            WHEN 'SUPPORTER' THEN 'SUPPORTER'::stakeholderstance_old
            WHEN 'NEUTRAL' THEN 'NEUTRAL'::stakeholderstance_old
            WHEN 'DETRACTOR' THEN 'BLOCKER'::stakeholderstance_old
            WHEN 'BLOCKER' THEN 'BLOCKER'::stakeholderstance_old
            WHEN 'UNKNOWN' THEN 'UNKNOWN'::stakeholderstance_old
            ELSE 'UNKNOWN'::stakeholderstance_old
        END
    """
    )

    op.execute("DROP TYPE stakeholderstance")
    op.execute("ALTER TYPE stakeholderstance_old RENAME TO stakeholderstance")
