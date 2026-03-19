"""036 add token tracking columns to job tables.

Add token_usage (JSONB), model_name (varchar), cost (numeric), and
latency_ms (integer) to deal_jobs and chat_message_jobs for per-agent
cost tracking and latency monitoring. Add user_id and chat_type to
chat_message_jobs. Make message_id nullable (group/channel bot chats
have no persisted message).
Drop token_usage from chat_messages (never populated, wrong entity).

Revision ID: 3c7f79637764
Revises: 3c7f79637765
Create Date: 2026-03-19 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637764"
down_revision: str | None = "3c7f79637765"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- deal_jobs: add token tracking columns
    op.add_column(
        "deal_jobs",
        sa.Column("token_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "deal_jobs",
        sa.Column("model_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "deal_jobs",
        sa.Column("cost", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "deal_jobs",
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )

    # -- chat_message_jobs: make message_id nullable (group/channel bot chats)
    op.alter_column(
        "chat_message_jobs",
        "message_id",
        existing_type=sa.UUID(),
        nullable=True,
    )

    # -- chat_message_jobs: add chat_type, user_id, token tracking columns
    op.add_column(
        "chat_message_jobs",
        sa.Column(
            "chat_type",
            sa.String(50),
            server_default=sa.text("'GENERAL_CHAT'"),
            nullable=False,
        ),
    )
    op.create_index("ix_chat_message_jobs_chat_type", "chat_message_jobs", ["chat_type"])
    op.add_column(
        "chat_message_jobs",
        sa.Column("user_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "chat_message_jobs",
        sa.Column("token_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "chat_message_jobs",
        sa.Column("model_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "chat_message_jobs",
        sa.Column("cost", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "chat_message_jobs",
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_chat_message_jobs_user_id", "chat_message_jobs", ["user_id"]
    )

    # -- chat_messages: drop token_usage (never populated, metrics belong on jobs)
    op.drop_column("chat_messages", "token_usage")


def downgrade() -> None:
    # -- chat_messages: restore token_usage
    op.add_column(
        "chat_messages",
        sa.Column("token_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # -- chat_message_jobs: remove added columns and revert message_id to NOT NULL
    # Use raw SQL with IF EXISTS for columns that may not exist yet
    # (handles the case where the old version of this migration was applied)
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_chat_message_jobs_user_id"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_chat_message_jobs_chat_type"))
    conn.execute(sa.text("ALTER TABLE chat_message_jobs DROP COLUMN IF EXISTS latency_ms"))
    conn.execute(sa.text("ALTER TABLE chat_message_jobs DROP COLUMN IF EXISTS cost"))
    conn.execute(sa.text("ALTER TABLE chat_message_jobs DROP COLUMN IF EXISTS model_name"))
    conn.execute(sa.text("ALTER TABLE chat_message_jobs DROP COLUMN IF EXISTS token_usage"))
    conn.execute(sa.text("ALTER TABLE chat_message_jobs DROP COLUMN IF EXISTS user_id"))
    conn.execute(sa.text("ALTER TABLE chat_message_jobs DROP COLUMN IF EXISTS chat_type"))
    # Delete bot jobs with no message before restoring NOT NULL constraint
    conn.execute(sa.text("DELETE FROM chat_message_jobs WHERE message_id IS NULL"))
    conn.execute(
        sa.text("ALTER TABLE chat_message_jobs ALTER COLUMN message_id SET NOT NULL")
    )

    # -- deal_jobs: remove added columns
    conn.execute(sa.text("ALTER TABLE deal_jobs DROP COLUMN IF EXISTS latency_ms"))
    conn.execute(sa.text("ALTER TABLE deal_jobs DROP COLUMN IF EXISTS cost"))
    conn.execute(sa.text("ALTER TABLE deal_jobs DROP COLUMN IF EXISTS model_name"))
    conn.execute(sa.text("ALTER TABLE deal_jobs DROP COLUMN IF EXISTS token_usage"))
