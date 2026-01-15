"""Add source_reference_id and approval_status to deal_stakeholders.

Revision ID: 3c7f79637775
Revises: 3c7f79637776
Create Date: 2026-01-13 12:00:00.000000

This migration adds:
1. source_reference_id UUID column to deal_stakeholders table
   - Links to CRMDealParticipant.participant_id when source=CRM
   - Used for tracking stakeholder origin and avoiding duplicates

2. approval_status enum column to deal_stakeholders table
   - Uses PostgreSQL enum type 'approvalstatus' (APPROVED, PENDING)
   - CRM stakeholders are always APPROVED (trusted source)
   - CALENDAR_DISCOVERY: First 5 per deal auto-APPROVED, rest PENDING
   - EMAIL_DISCOVERY: First 5 per deal auto-APPROVED, rest PENDING
   - User can REJECT any stakeholder to exclude from matching
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

# revision identifiers, used by Alembic.
revision: str = "3c7f79637775"
down_revision: str | None = "3c7f79637776"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Define the PostgreSQL enum type
approvalstatus_enum = ENUM(
    "APPROVED",
    "PENDING",
    "REJECTED",
    name="approvalstatus",
    create_type=False,  # We'll create it manually
)


def upgrade() -> None:
    conn = op.get_bind()

    # =========================================================================
    # ADD ALL MISSING ENUM VALUES AT ONCE
    # PostgreSQL enums must have all values defined before they can be used
    # =========================================================================

    # 1. associationstatus: ACTIVE, REMOVED, CANCELLED
    for value in ["ACTIVE", "REMOVED", "CANCELLED"]:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN "
                f"ALTER TYPE associationstatus ADD VALUE IF NOT EXISTS '{value}'; "
                f"EXCEPTION WHEN others THEN NULL; "
                f"END $$;"
            )
        )

    # 2. generationstatus: NOT_STARTED, GENERATING, COMPLETED, FAILED
    for value in ["NOT_STARTED", "GENERATING", "COMPLETED", "FAILED"]:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN "
                f"ALTER TYPE generationstatus ADD VALUE IF NOT EXISTS '{value}'; "
                f"EXCEPTION WHEN others THEN NULL; "
                f"END $$;"
            )
        )

    # 3. stakeholderstance: UNKNOWN, CHAMPION, SUPPORTER, NEUTRAL, DETRACTOR, BLOCKER
    for value in ["UNKNOWN", "CHAMPION", "SUPPORTER", "NEUTRAL", "DETRACTOR", "BLOCKER"]:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN "
                f"ALTER TYPE stakeholderstance ADD VALUE IF NOT EXISTS '{value}'; "
                f"EXCEPTION WHEN others THEN NULL; "
                f"END $$;"
            )
        )

    # 4. decisionpower: UNKNOWN, DECISION_MAKER, INFLUENCER, EVALUATOR, GATEKEEPER, END_USER
    for value in ["UNKNOWN", "DECISION_MAKER", "INFLUENCER", "EVALUATOR", "GATEKEEPER", "END_USER"]:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN "
                f"ALTER TYPE decisionpower ADD VALUE IF NOT EXISTS '{value}'; "
                f"EXCEPTION WHEN others THEN NULL; "
                f"END $$;"
            )
        )

    # 5. stakeholdertemperature: UNKNOWN, HOT, WARM, COLD
    for value in ["UNKNOWN", "HOT", "WARM", "COLD"]:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN "
                f"ALTER TYPE stakeholdertemperature ADD VALUE IF NOT EXISTS '{value}'; "
                f"EXCEPTION WHEN others THEN NULL; "
                f"END $$;"
            )
        )

    # 6. stakeholdersource: CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL
    for value in ["CRM", "CALENDAR_DISCOVERY", "EMAIL_DISCOVERY", "MANUAL"]:
        conn.execute(
            sa.text(
                f"DO $$ BEGIN "
                f"ALTER TYPE stakeholdersource ADD VALUE IF NOT EXISTS '{value}'; "
                f"EXCEPTION WHEN others THEN NULL; "
                f"END $$;"
            )
        )

    # Add source_reference_id column to deal_stakeholders
    op.add_column(
        "deal_stakeholders",
        sa.Column(
            "source_reference_id",
            sa.UUID(),
            nullable=True,
        ),
    )

    # Create index for efficient lookups by source reference
    op.create_index(
        "ix_deal_stakeholder_source_ref",
        "deal_stakeholders",
        ["source_reference_id"],
    )

    # Create the approvalstatus enum type if it doesn't exist
    conn.execute(
        sa.text(
            "DO $$ BEGIN "
            "CREATE TYPE approvalstatus AS ENUM ('APPROVED', 'PENDING', 'REJECTED'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$;"
        )
    )

    # Add REJECTED value if enum already exists (handles partial migrations)
    conn.execute(
        sa.text(
            "DO $$ BEGIN "
            "ALTER TYPE approvalstatus ADD VALUE IF NOT EXISTS 'REJECTED'; "
            "EXCEPTION WHEN others THEN NULL; "
            "END $$;"
        )
    )

    # Add approval_status column using the enum type
    # Default to 'APPROVED' for existing records (they were trusted before this feature)
    op.add_column(
        "deal_stakeholders",
        sa.Column(
            "approval_status",
            approvalstatus_enum,
            nullable=False,
            server_default="APPROVED",
        ),
    )

    # Create index for efficient filtering by approval status
    op.create_index(
        "ix_deal_stakeholder_approval_status",
        "deal_stakeholders",
        ["approval_status"],
    )


def downgrade() -> None:
    # Use raw SQL with IF EXISTS for safety (handles partial migration states)
    conn = op.get_bind()

    # Drop approval_status index if exists
    conn.execute(
        sa.text("DROP INDEX IF EXISTS ix_deal_stakeholder_approval_status")
    )

    # Drop approval_status column if exists
    conn.execute(
        sa.text(
            "ALTER TABLE deal_stakeholders DROP COLUMN IF EXISTS approval_status"
        )
    )

    # Drop approvalstatus enum type if exists (only if no other tables use it)
    conn.execute(sa.text("DROP TYPE IF EXISTS approvalstatus"))

    # Drop source_reference_id index if exists
    conn.execute(
        sa.text("DROP INDEX IF EXISTS ix_deal_stakeholder_source_ref")
    )

    # Drop source_reference_id column if exists
    conn.execute(
        sa.text(
            "ALTER TABLE deal_stakeholders DROP COLUMN IF EXISTS source_reference_id"
        )
    )
