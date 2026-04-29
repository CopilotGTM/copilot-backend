"""Risk/blocker active lifecycle: drop suggested flow, add closure tracking.

Removes the AI_SUGGESTED transit state. AI extraction now creates items
directly with status=OPEN (handled in application code). Renames the
resolution-specific columns into a unified closure schema so "who closed
it / when" is queryable in SQL instead of only via JSON parsing on
risk_blocker_data.

Schema changes (data-preserving where possible):
- resolved_at -> RENAMED to closed_at (single ALTER, no data movement).
  Same timestamp semantic; the column now also carries the close time of
  dismissed items going forward.
- resolved_by (free-text string, inconsistently populated) -> data copied
  into the new closed_by_user_name column before dropping. The legacy
  string sometimes held a name and sometimes a UUID-as-text; we keep
  whatever was there as the human-readable label.
- closed_by_user_id (new, nullable UUID): NULL for legacy rows; the
  service populates it for every closure going forward (the
  AUTOMATED_PIPELINE_USER_ID sentinel for AI-driven closures, the acting
  user's UUID for user-driven closures).
- user_confirmed_at: dropped (no longer meaningful with no confirm step).

Status backfill rules:
- AI_SUGGESTED rows -> OPEN (treated as active items going forward).
- AI_DISMISSED rows -> DISMISSED (consolidated under the user dismiss state).

Revision ID: 3c7f79637751
Revises: 3c7f79637752
Create Date: 2026-04-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637751"
down_revision: str | None = "3c7f79637752"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "deal_risks_blockers",
        "resolved_at",
        new_column_name="closed_at",
    )

    op.add_column(
        "deal_risks_blockers",
        sa.Column("closed_by_user_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "deal_risks_blockers",
        sa.Column("closed_by_user_name", sa.String(length=255), nullable=True),
    )

    op.execute(
        """
        UPDATE deal_risks_blockers
        SET closed_by_user_name = resolved_by
        WHERE resolved_by IS NOT NULL
        """
    )

    op.execute(
        "UPDATE deal_risks_blockers SET status = 'OPEN' WHERE status = 'AI_SUGGESTED'"
    )
    op.execute(
        "UPDATE deal_risks_blockers SET status = 'DISMISSED' WHERE status = 'AI_DISMISSED'"
    )

    op.alter_column(
        "deal_risks_blockers",
        "status",
        server_default=sa.text("'OPEN'"),
    )

    op.drop_index("ix_risk_blocker_ai_suggested", table_name="deal_risks_blockers")

    op.drop_column("deal_risks_blockers", "user_confirmed_at")
    op.drop_column("deal_risks_blockers", "resolved_by")


def downgrade() -> None:
    op.add_column(
        "deal_risks_blockers",
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "deal_risks_blockers",
        sa.Column("user_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        UPDATE deal_risks_blockers
        SET resolved_by = closed_by_user_name
        WHERE status = 'RESOLVED' AND closed_by_user_name IS NOT NULL
        """
    )

    op.create_index(
        "ix_risk_blocker_ai_suggested",
        "deal_risks_blockers",
        ["organisation_id", "is_ai_generated", "status"],
    )

    op.alter_column(
        "deal_risks_blockers",
        "status",
        server_default=sa.text("'AI_SUGGESTED'"),
    )

    op.drop_column("deal_risks_blockers", "closed_by_user_name")
    op.drop_column("deal_risks_blockers", "closed_by_user_id")

    op.alter_column(
        "deal_risks_blockers",
        "closed_at",
        new_column_name="resolved_at",
    )
