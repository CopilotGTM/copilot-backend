"""038 add external assignee columns to crm_activities.

Revision ID: 3c7f79637762
Revises: 3c7f79637763
Create Date: 2026-03-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637762"
down_revision: str | None = "3c7f79637763"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crm_activities",
        sa.Column("external_assignee_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("external_assignee_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crm_activities", "external_assignee_name")
    op.drop_column("crm_activities", "external_assignee_email")
