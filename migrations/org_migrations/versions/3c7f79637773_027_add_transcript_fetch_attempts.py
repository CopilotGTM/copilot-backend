"""Add transcript_fetch_attempts column to meetings table.

Revision ID: 3c7f79637773
Revises: 3c7f79637774
Create Date: 2026-01-17 12:00:00.000000

This migration adds the transcript_fetch_attempts column to the meetings table.
This column tracks the number of times we've attempted to fetch a transcript
for a meeting, enabling retry logic in the scheduled transcript fetch task.

After 2 failed attempts, meetings are marked as NOT_AVAILABLE to prevent
infinite retries on meetings that weren't recorded.
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637773"
down_revision: str | None = "3c7f79637774"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add transcript_fetch_attempts column to meetings table
    op.add_column(
        "meetings",
        sa.Column(
            "transcript_fetch_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    # Remove transcript_fetch_attempts column
    op.drop_column("meetings", "transcript_fetch_attempts")
