"""Widen calendar user-state external IDs.

Revision ID: 3c7f79637745
Revises: 3c7f79637746
Create Date: 2026-05-27 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637745"
down_revision: str | None = "3c7f79637746"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "calendar_event_user_states",
        "external_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=1024),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "calendar_event_user_states",
        "external_id",
        existing_type=sa.String(length=1024),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
