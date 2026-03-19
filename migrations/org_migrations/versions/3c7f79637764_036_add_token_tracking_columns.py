"""036 add token tracking columns to job tables.

Add token_usage (JSONB), model_name (varchar), cost (numeric), and
latency_ms (integer) to deal_jobs and chat_message_jobs for per-agent
cost tracking and latency monitoring. Add user_id to chat_message_jobs
for uniform per-user cost queries.
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

    # -- chat_message_jobs: add token tracking columns + user_id
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

    # -- chat_message_jobs: remove added columns
    op.drop_index("ix_chat_message_jobs_user_id", table_name="chat_message_jobs")
    op.drop_column("chat_message_jobs", "latency_ms")
    op.drop_column("chat_message_jobs", "cost")
    op.drop_column("chat_message_jobs", "model_name")
    op.drop_column("chat_message_jobs", "token_usage")
    op.drop_column("chat_message_jobs", "user_id")

    # -- deal_jobs: remove added columns
    op.drop_column("deal_jobs", "latency_ms")
    op.drop_column("deal_jobs", "cost")
    op.drop_column("deal_jobs", "model_name")
    op.drop_column("deal_jobs", "token_usage")
