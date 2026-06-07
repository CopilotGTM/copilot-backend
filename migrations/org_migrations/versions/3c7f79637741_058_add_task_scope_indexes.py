"""Add scoped task listing indexes.

Revision ID: 3c7f79637741
Revises: 3c7f79637742
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637741"
down_revision: str | None = "3c7f79637742"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_TASKS = "is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO')"


def upgrade() -> None:
    op.create_index(
        "ix_crm_activity_task_owner_due",
        "crm_activities",
        ["organisation_id", "owner_id", "due_date"],
        postgresql_where=sa.text(
            f"{_ACTIVE_TASKS} "
            "AND external_assignee_email IS NULL AND external_assignee_name IS NULL"
        ),
    )
    op.create_index(
        "ix_crm_activity_task_created_by_due",
        "crm_activities",
        ["organisation_id", "created_by_user_id", "due_date"],
        postgresql_where=sa.text(_ACTIVE_TASKS),
    )
    op.create_index(
        "ix_crm_activity_task_external_email_due",
        "crm_activities",
        ["organisation_id", "external_assignee_email", "due_date"],
        postgresql_where=sa.text(f"{_ACTIVE_TASKS} AND external_assignee_email IS NOT NULL"),
    )
    op.create_index(
        "ix_crm_activity_task_public_assignee_due",
        "crm_activities",
        [
            "organisation_id",
            sa.text("(properties ->> 'assigned_to_public_user_id')"),
            "due_date",
        ],
        postgresql_where=sa.text(
            f"{_ACTIVE_TASKS} "
            "AND external_assignee_email IS NULL AND external_assignee_name IS NULL "
            "AND properties ? 'assigned_to_public_user_id'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_crm_activity_task_public_assignee_due", table_name="crm_activities")
    op.drop_index("ix_crm_activity_task_external_email_due", table_name="crm_activities")
    op.drop_index("ix_crm_activity_task_created_by_due", table_name="crm_activities")
    op.drop_index("ix_crm_activity_task_owner_due", table_name="crm_activities")
