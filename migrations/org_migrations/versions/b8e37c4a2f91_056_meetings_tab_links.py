"""056 add Meetings tab link and bot preference tables.

Revision ID: b8e37c4a2f91
Revises: 3c7f79637744
Create Date: 2026-06-04 19:45:00.000000

The new tables are additive. They power the top-level Meetings tab while deal
meeting jobs continue to read `calendar_event_deal_associations`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e37c4a2f91"
down_revision: str | None = "3c7f79637744"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create additive Meetings V1 link and bot preference tables."""
    op.create_table(
        "meeting_record_links",
        sa.Column(
            "link_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("calendar_event_id", sa.UUID(), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("linked_by_user_id", sa.UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["calendar_event_id"], ["calendar_events.event_id"]),
        sa.CheckConstraint(
            "record_type IN ('DEAL', 'LEAD', 'CONTACT')",
            name="ck_meeting_record_links_type_v1",
        ),
    )
    op.create_index(
        "ix_meeting_record_links_org_event",
        "meeting_record_links",
        ["organisation_id", "calendar_event_id"],
    )
    op.create_index(
        "ix_meeting_record_links_org_record",
        "meeting_record_links",
        ["organisation_id", "record_type", "record_id"],
    )
    # One active Meetings-tab link is allowed per event. Inactive rows remain
    # available for audit/history and do not affect current drawer behavior.
    op.create_index(
        "uq_meeting_record_links_active_event",
        "meeting_record_links",
        ["organisation_id", "calendar_event_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "meeting_bot_preferences",
        sa.Column(
            "preference_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("calendar_event_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("preference", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["calendar_event_id"], ["calendar_events.event_id"]),
        sa.CheckConstraint(
            "preference IN ('AUTO_INVITE', 'ASK_BEFORE', 'DO_NOT_INVITE')",
            name="ck_meeting_bot_preferences_value",
        ),
    )
    op.create_index(
        "ix_meeting_bot_preferences_org_event",
        "meeting_bot_preferences",
        ["organisation_id", "calendar_event_id"],
    )
    op.create_index(
        "uq_meeting_bot_preferences_org_event_user",
        "meeting_bot_preferences",
        ["organisation_id", "calendar_event_id", "user_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop Meetings V1 additive tables."""
    op.drop_index(
        "uq_meeting_bot_preferences_org_event_user",
        table_name="meeting_bot_preferences",
    )
    op.drop_index("ix_meeting_bot_preferences_org_event", table_name="meeting_bot_preferences")
    op.drop_table("meeting_bot_preferences")

    op.drop_index("uq_meeting_record_links_active_event", table_name="meeting_record_links")
    op.drop_index("ix_meeting_record_links_org_record", table_name="meeting_record_links")
    op.drop_index("ix_meeting_record_links_org_event", table_name="meeting_record_links")
    op.drop_table("meeting_record_links")
