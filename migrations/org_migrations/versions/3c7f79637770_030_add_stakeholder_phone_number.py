"""030 add phone_number to deal_stakeholders

Revision ID: 3c7f79637770
Revises: 3c7f79637797
Create Date: 2026-02-17 12:00:00.000000

Adds phone_number column to deal_stakeholders table.
Stored as-is from CRM or user input — no formatting or validation applied.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637770"
down_revision: str | None = "3c7f79637797"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deal_stakeholders",
        sa.Column("phone_number", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deal_stakeholders", "phone_number")
