"""037 add calendar occurrence identity columns and constraints.

Revision ID: 3c7f79637763
Revises: 3c7f79637764
Create Date: 2026-03-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637763"
down_revision: str | None = "3c7f79637764"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column("provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column(
            "occurrence_anchor",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "calendar_events",
        sa.Column("original_start_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_calendar_event_occurrence_anchor",
        "calendar_events",
        ["occurrence_anchor"],
        unique=False,
    )
    op.create_index(
        "ix_calendar_user_state_integration_external",
        "calendar_event_user_states",
        ["integration_id", "external_id"],
        unique=False,
    )

    op.drop_constraint("uq_calendar_event_external", "calendar_events", type_="unique")
    op.create_unique_constraint(
        "uq_calendar_event_external_occurrence",
        "calendar_events",
        ["external_id", "occurrence_anchor"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_calendar_event_external_occurrence",
        "calendar_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_calendar_event_external",
        "calendar_events",
        ["organisation_id", "external_id"],
    )

    op.drop_index(
        "ix_calendar_user_state_integration_external",
        table_name="calendar_event_user_states",
    )
    op.drop_index("ix_calendar_event_occurrence_anchor", table_name="calendar_events")

    op.drop_column("calendar_events", "original_start_at")
    op.drop_column("calendar_events", "occurrence_anchor")
    op.drop_column("calendar_events", "provider")
