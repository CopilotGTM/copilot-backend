"""035 add activity source, created_by, and reminder activity linkage.

Revision ID: 3c7f79637765
Revises: 2701960d485f
Create Date: 2026-03-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637765"
down_revision: str | None = "2701960d485f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- crm_activities: source tracking
    op.add_column("crm_activities", sa.Column("source", sa.String(50), nullable=True))
    op.execute("UPDATE crm_activities SET source = 'CRM_SYNC' WHERE integration_id IS NOT NULL")
    op.execute("UPDATE crm_activities SET source = 'MANUAL' WHERE integration_id IS NULL")
    op.create_index(
        "ix_crm_activity_source", "crm_activities", ["organisation_id", "source"]
    )

    # -- crm_activities: creator tracking
    op.add_column(
        "crm_activities", sa.Column("created_by_user_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        "ix_crm_activity_created_by",
        "crm_activities",
        ["organisation_id", "created_by_user_id"],
    )

    # -- user_reminders: activity linkage
    op.add_column("user_reminders", sa.Column("activity_id", sa.UUID(), nullable=True))
    op.create_index("ix_user_reminders_activity", "user_reminders", ["activity_id"])


def downgrade() -> None:
    op.drop_index("ix_user_reminders_activity", table_name="user_reminders")
    op.drop_column("user_reminders", "activity_id")
    op.drop_index("ix_crm_activity_created_by", table_name="crm_activities")
    op.drop_column("crm_activities", "created_by_user_id")
    op.drop_index("ix_crm_activity_source", table_name="crm_activities")
    op.drop_column("crm_activities", "source")
