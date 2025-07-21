"""Initial organization tables

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create chat_sessions table
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("chat_type", sa.String(), nullable=False),
        sa.Column("chat_version", sa.String(), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(op.f("ix_chat_sessions_chat_type"), "chat_sessions", ["chat_type"], unique=False)
    op.create_index(op.f("ix_chat_sessions_chat_version"), "chat_sessions", ["chat_version"], unique=False)
    op.create_index(op.f("ix_chat_sessions_is_deleted"), "chat_sessions", ["is_deleted"], unique=False)
    op.create_index(op.f("ix_chat_sessions_organisation_id"), "chat_sessions", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_chat_sessions_user_id"), "chat_sessions", ["user_id"], unique=False)

    # Create chat_messages table
    op.create_table(
        "chat_messages",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("message_type", sa.String(), nullable=False),
        sa.Column("message_version", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("technical_deck_details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rfp_details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(op.f("ix_chat_messages_message_type"), "chat_messages", ["message_type"], unique=False)
    op.create_index(op.f("ix_chat_messages_session_id"), "chat_messages", ["session_id"], unique=False)

    # Create knowledge_base table
    op.create_table(
        "knowledge_base",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("meta_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content", sa.String(), nullable=True),
        sa.Column("embedding", postgresql.VECTOR(), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_base_content_hash"), "knowledge_base", ["content_hash"], unique=False)
    op.create_index(op.f("ix_knowledge_base_id"), "knowledge_base", ["id"], unique=False)
    op.create_index(op.f("ix_knowledge_base_name"), "knowledge_base", ["name"], unique=False)

    # Add foreign key constraints
    op.create_foreign_key(None, "chat_messages", "chat_sessions", ["session_id"], ["session_id"])


def downgrade() -> None:
    # Remove foreign key constraints
    op.drop_constraint(None, "chat_messages", type_="foreignkey")

    # Drop tables
    op.drop_index(op.f("ix_knowledge_base_name"), table_name="knowledge_base")
    op.drop_index(op.f("ix_knowledge_base_id"), table_name="knowledge_base")
    op.drop_index(op.f("ix_knowledge_base_content_hash"), table_name="knowledge_base")
    op.drop_table("knowledge_base")

    op.drop_index(op.f("ix_chat_messages_session_id"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_message_type"), table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index(op.f("ix_chat_sessions_user_id"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_organisation_id"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_is_deleted"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_chat_version"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_chat_type"), table_name="chat_sessions")
    op.drop_table("chat_sessions")
