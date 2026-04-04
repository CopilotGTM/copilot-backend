"""043 org-scoped messaging entities.

Restructures messaging tables from per-integration to org-scoped,
following the CalendarEvent + CalendarEventUserState pattern.

DESTRUCTIVE: Deletes all existing messaging data (channels, members,
messages, deal links, summaries) before restructuring. Safe because
no production messaging data exists yet.

MessagingChannel:
- integration_id nullable (FlexibleSyncedRecordMixin, always None)
- New 'provider' column (teams/slack/etc.)
- Unique: (organisation_id, provider, external_channel_id)
- Sync cursors stay on channel (any integration can continue)
- Drops: creator_participant_id, synced_at, last_seen_at

MessagingChannelMember:
- integration_id nullable (always None)
- Stores member data directly: external_user_id, email, display_name
- Drops: participant_id FK, added_by_participant_id FK
- Unique: (channel_id, external_user_id)

MessagingMessage:
- integration_id nullable (always None)
- Stores author data directly: author_email, author_display_name
- Unique: (channel_id, external_message_id)

IntegrationParticipant:
- integration_id nullable (FlexibleSyncedRecordMixin)
- Tables kept but no longer used by messaging sync

CalendarEvent:
- Drops integration_id (org-scoped via OrgScopedRecordMixin)
- Integration tracking stays in calendar_event_user_states

New table: messaging_channel_integration_states
- Per-integration visibility (like CalendarEventUserState)
- Tracks which integrations can see each channel
- No sync cursors (those stay on the channel)

Revision ID: 3c7f79637757
Revises: 3c7f79637758
Create Date: 2026-04-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637757"
down_revision: str | None = "3c7f79637758"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Clean slate: delete all messaging data before restructuring ──
    # Old schema allowed duplicates (same channel per-integration). New schema
    # requires org-scoped uniqueness. Rather than complex dedup logic, clear
    # the tables — no production messaging data exists yet.
    op.execute("DELETE FROM messaging_deal_links")
    op.execute("DELETE FROM messaging_thread_summaries")
    op.execute("DELETE FROM messaging_messages")
    op.execute("DELETE FROM messaging_channel_members")
    op.execute("DELETE FROM messaging_channels")

    # ── messaging_channels: drop integration_id (org-scoped, no integration ownership) ──
    op.execute("ALTER TABLE messaging_channels DROP COLUMN IF EXISTS integration_id")

    # Add provider column FIRST (needed by new unique constraint)
    op.execute(
        "ALTER TABLE messaging_channels "
        "ADD COLUMN IF NOT EXISTS provider VARCHAR(32) NOT NULL DEFAULT 'teams'"
    )

    # Drop old unique constraint (integration_id, external_channel_id) — may already be gone
    op.execute(
        "ALTER TABLE messaging_channels "
        "DROP CONSTRAINT IF EXISTS uq_messaging_channel_external"
    )

    # Add new unique constraint (organisation_id, provider, external_channel_id)
    op.create_unique_constraint(
        "uq_messaging_channel_org_provider_external",
        "messaging_channels",
        ["organisation_id", "provider", "external_channel_id"],
    )

    # Drop redundant timestamp columns (last_synced_at covers both)
    op.execute("ALTER TABLE messaging_channels DROP COLUMN IF EXISTS synced_at")
    op.execute("ALTER TABLE messaging_channels DROP COLUMN IF EXISTS last_seen_at")

    # Drop creator_participant_id FK (decoupling from IntegrationParticipant)
    op.execute("ALTER TABLE messaging_channels DROP COLUMN IF EXISTS creator_participant_id")

    # ── messaging_channel_members: redesign to store member data directly ──
    op.execute("ALTER TABLE messaging_channel_members DROP COLUMN IF EXISTS integration_id")

    # Make joined_at nullable (not always available from provider)
    op.alter_column(
        "messaging_channel_members",
        "joined_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )

    # Drop old unique constraint and participant FK columns
    op.execute("ALTER TABLE messaging_channel_members DROP CONSTRAINT IF EXISTS uq_channel_member")
    op.execute("DROP INDEX IF EXISTS ix_channel_member_participant")

    # Drop participant FK columns (no longer used)
    op.execute("ALTER TABLE messaging_channel_members DROP COLUMN IF EXISTS participant_id")
    op.execute("ALTER TABLE messaging_channel_members DROP COLUMN IF EXISTS added_by_participant_id")

    # Add direct member identity columns (table is empty after DELETE above)
    op.execute(
        "ALTER TABLE messaging_channel_members "
        "ADD COLUMN IF NOT EXISTS external_user_id VARCHAR(255) NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE messaging_channel_members "
        "ADD COLUMN IF NOT EXISTS email VARCHAR(320)"
    )
    op.execute(
        "ALTER TABLE messaging_channel_members "
        "ADD COLUMN IF NOT EXISTS email_normalized VARCHAR(320)"
    )
    op.execute(
        "ALTER TABLE messaging_channel_members "
        "ADD COLUMN IF NOT EXISTS display_name VARCHAR(255)"
    )

    # New unique constraint: one member per external user per channel
    op.create_unique_constraint(
        "uq_channel_member",
        "messaging_channel_members",
        ["channel_id", "external_user_id"],
    )
    op.create_index(
        "ix_channel_member_email",
        "messaging_channel_members",
        ["email_normalized"],
    )

    # ── messaging_messages: org-scoped, direct author fields ──
    op.execute("ALTER TABLE messaging_messages DROP COLUMN IF EXISTS integration_id")

    # Drop old unique constraint and participant FK
    op.execute("ALTER TABLE messaging_messages DROP CONSTRAINT IF EXISTS uq_messaging_message_external")
    op.execute("DROP INDEX IF EXISTS ix_messaging_message_participant")
    op.execute("ALTER TABLE messaging_messages DROP COLUMN IF EXISTS participant_id")

    # Add direct author columns
    op.execute("ALTER TABLE messaging_messages ADD COLUMN IF NOT EXISTS author_email VARCHAR(320)")
    op.execute(
        "ALTER TABLE messaging_messages "
        "ADD COLUMN IF NOT EXISTS author_email_normalized VARCHAR(320)"
    )
    op.execute(
        "ALTER TABLE messaging_messages "
        "ADD COLUMN IF NOT EXISTS author_display_name VARCHAR(255)"
    )

    # New unique constraint: one message per channel
    op.create_unique_constraint(
        "uq_messaging_message_external",
        "messaging_messages",
        ["channel_id", "external_message_id"],
    )
    op.create_index(
        "ix_messaging_message_author_email",
        "messaging_messages",
        ["author_email_normalized"],
    )

    # ── integration_participants: make integration_id nullable ──
    op.alter_column(
        "integration_participants",
        "integration_id",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=True,
    )

    # ── calendar_events: drop integration_id (org-scoped via OrgScopedRecordMixin) ──
    # Integration tracking lives in calendar_event_user_states, not on the event itself.
    op.execute("ALTER TABLE calendar_events DROP COLUMN IF EXISTS integration_id")

    # ── Create messaging_channel_integration_states table ──
    op.create_table(
        "messaging_channel_integration_states",
        sa.Column(
            "state_id",
            sa.dialects.postgresql.UUID(),
            primary_key=True,
        ),
        sa.Column(
            "channel_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("messaging_channels.channel_id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "integration_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("knowledge_integrations.integration_id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "external_channel_id",
            sa.String(255),
            nullable=False,
        ),
        sa.Column(
            "organisation_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
            index=True,
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
        sa.UniqueConstraint(
            "channel_id",
            "integration_id",
            name="uq_messaging_channel_integration_state",
        ),
    )

    op.create_index(
        "ix_msg_channel_state_integration",
        "messaging_channel_integration_states",
        ["integration_id"],
    )
    op.create_index(
        "ix_msg_channel_state_integration_external",
        "messaging_channel_integration_states",
        ["integration_id", "external_channel_id"],
    )


def downgrade() -> None:
    # Drop new table
    op.drop_index(
        "ix_msg_channel_state_integration_external",
        table_name="messaging_channel_integration_states",
    )
    op.drop_index(
        "ix_msg_channel_state_integration",
        table_name="messaging_channel_integration_states",
    )
    op.drop_table("messaging_channel_integration_states")

    # Restore messaging_messages to old schema
    op.drop_index("ix_messaging_message_author_email", table_name="messaging_messages")
    op.drop_constraint(
        "uq_messaging_message_external",
        "messaging_messages",
        type_="unique",
    )

    # Drop new author columns
    op.drop_column("messaging_messages", "author_display_name")
    op.drop_column("messaging_messages", "author_email_normalized")
    op.drop_column("messaging_messages", "author_email")

    # Restore participant_id FK
    op.add_column(
        "messaging_messages",
        sa.Column(
            "participant_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_messaging_message_participant",
        "messaging_messages",
        ["participant_id"],
    )

    # Restore old unique constraint
    op.create_unique_constraint(
        "uq_messaging_message_external",
        "messaging_messages",
        ["integration_id", "external_message_id"],
    )
    op.add_column(
        "messaging_messages",
        sa.Column(
            "integration_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
    )

    # Restore messaging_channel_members to old schema
    op.drop_index("ix_channel_member_email", table_name="messaging_channel_members")
    op.drop_constraint(
        "uq_channel_member",
        "messaging_channel_members",
        type_="unique",
    )

    # Drop new direct columns
    op.drop_column("messaging_channel_members", "display_name")
    op.drop_column("messaging_channel_members", "email_normalized")
    op.drop_column("messaging_channel_members", "email")
    op.drop_column("messaging_channel_members", "external_user_id")

    # Restore participant FK columns
    op.add_column(
        "messaging_channel_members",
        sa.Column(
            "participant_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
    )
    op.add_column(
        "messaging_channel_members",
        sa.Column(
            "added_by_participant_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_channel_member_participant",
        "messaging_channel_members",
        ["participant_id"],
    )

    # Make joined_at NOT NULL again
    op.alter_column(
        "messaging_channel_members",
        "joined_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )

    # Restore old unique constraint
    op.create_unique_constraint(
        "uq_channel_member",
        "messaging_channel_members",
        ["channel_id", "participant_id", "joined_at"],
    )
    op.add_column(
        "messaging_channel_members",
        sa.Column(
            "integration_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
    )

    # Restore integration_participants.integration_id as NOT NULL
    op.alter_column(
        "integration_participants",
        "integration_id",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=False,
    )

    # Restore calendar_events.integration_id (was dropped in upgrade)
    op.add_column(
        "calendar_events",
        sa.Column(
            "integration_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
    )

    # Drop new constraint FIRST (depends on provider column)
    op.drop_constraint(
        "uq_messaging_channel_org_provider_external",
        "messaging_channels",
        type_="unique",
    )

    # Now safe to drop provider and restore old columns
    op.drop_column("messaging_channels", "provider")
    op.add_column(
        "messaging_channels",
        sa.Column(
            "creator_participant_id",
            sa.dialects.postgresql.UUID(),
            nullable=True,
        ),
    )
    op.add_column(
        "messaging_channels",
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "messaging_channels",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # Restore integration_id column and old unique constraint
    op.add_column(
        "messaging_channels",
        sa.Column(
            "integration_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_messaging_channel_external",
        "messaging_channels",
        ["integration_id", "external_channel_id"],
    )
