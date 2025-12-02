"""013 update stakeholder enums

Revision ID: 3c7f79637787
Revises: 3c7f79637788
Create Date: 2025-12-02 10:00:00.000000

This migration updates the stakeholder enums to support richer analysis:
- stakeholderstance: Add CHAMPION, DETRACTOR values for better stance tracking
- decisionpower: Replace HIGH/MEDIUM/LOW with role-based values (DECISION_MAKER, INFLUENCER, etc.)

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3c7f79637787'
down_revision: Union[str, None] = '3c7f79637788'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - update stakeholder enums with new values."""
    
    # 1. Update stakeholderstance enum - add CHAMPION and DETRACTOR
    # PostgreSQL requires adding new values to existing enum
    op.execute("ALTER TYPE stakeholderstance ADD VALUE IF NOT EXISTS 'CHAMPION'")
    op.execute("ALTER TYPE stakeholderstance ADD VALUE IF NOT EXISTS 'DETRACTOR'")
    
    # 2. Update decisionpower enum - need to replace values entirely
    # This requires: create new enum, update column, drop old enum, rename new enum
    
    # Create new enum type with the desired values
    op.execute("""
        CREATE TYPE decisionpower_new AS ENUM (
            'UNKNOWN', 'DECISION_MAKER', 'INFLUENCER', 'EVALUATOR', 'GATEKEEPER', 'END_USER'
        )
    """)
    
    # Map old values to new values and update the column
    # HIGH -> DECISION_MAKER (high decision power = decision maker)
    # MEDIUM -> INFLUENCER (medium decision power = influencer)
    # LOW -> END_USER (low decision power = end user)
    # UNKNOWN -> UNKNOWN
    op.execute("""
        ALTER TABLE deal_stakeholders 
        ALTER COLUMN decision_power TYPE decisionpower_new 
        USING CASE decision_power::text
            WHEN 'HIGH' THEN 'DECISION_MAKER'::decisionpower_new
            WHEN 'MEDIUM' THEN 'INFLUENCER'::decisionpower_new
            WHEN 'LOW' THEN 'END_USER'::decisionpower_new
            WHEN 'UNKNOWN' THEN 'UNKNOWN'::decisionpower_new
            ELSE 'UNKNOWN'::decisionpower_new
        END
    """)
    
    # Drop old enum type
    op.execute("DROP TYPE decisionpower")
    
    # Rename new enum to original name
    op.execute("ALTER TYPE decisionpower_new RENAME TO decisionpower")


def downgrade() -> None:
    """Downgrade schema - revert stakeholder enums to original values."""
    
    # Note: Cannot remove enum values in PostgreSQL, so we need to recreate the enum
    
    # 1. Revert decisionpower enum
    op.execute("""
        CREATE TYPE decisionpower_old AS ENUM ('HIGH', 'MEDIUM', 'LOW', 'UNKNOWN')
    """)
    
    # Map new values back to old values
    op.execute("""
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
    """)
    
    op.execute("DROP TYPE decisionpower")
    op.execute("ALTER TYPE decisionpower_old RENAME TO decisionpower")
    
    # 2. Revert stakeholderstance enum (recreate without CHAMPION and DETRACTOR)
    # Map CHAMPION -> SUPPORTER, DETRACTOR -> BLOCKER
    op.execute("""
        CREATE TYPE stakeholderstance_old AS ENUM ('SUPPORTER', 'NEUTRAL', 'BLOCKER', 'UNKNOWN')
    """)
    
    op.execute("""
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
    """)
    
    op.execute("DROP TYPE stakeholderstance")
    op.execute("ALTER TYPE stakeholderstance_old RENAME TO stakeholderstance")
