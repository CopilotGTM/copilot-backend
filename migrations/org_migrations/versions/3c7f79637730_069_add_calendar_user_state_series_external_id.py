"""Add calendar user-state recurring series identity.

Revision ID: 3c7f79637730
Revises: 3c7f79637731
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637730"
down_revision: str | None = "3c7f79637731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_event_user_states",
        sa.Column("series_external_id", sa.String(length=1024), nullable=True),
    )
    op.create_index(
        "ix_calendar_user_state_integration_series_external",
        "calendar_event_user_states",
        ["integration_id", "series_external_id"],
    )
    op.execute(
        """
        UPDATE calendar_event_user_states AS state
        SET series_external_id = COALESCE(
            event.data_payload ->> 'recurringEventId',
            event.data_payload ->> 'seriesMasterId'
        )
        FROM calendar_events AS event
        WHERE state.event_id = event.event_id
          AND state.series_external_id IS NULL
          AND event.data_payload ->> 'id' = state.external_id
          AND COALESCE(
              event.data_payload ->> 'recurringEventId',
              event.data_payload ->> 'seriesMasterId'
          ) IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_user_state_integration_series_external",
        table_name="calendar_event_user_states",
    )
    op.drop_column("calendar_event_user_states", "series_external_id")
