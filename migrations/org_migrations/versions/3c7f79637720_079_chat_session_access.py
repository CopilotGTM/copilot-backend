"""Add explicit read-only chat session access.

Revision ID: 3c7f79637720
Revises: 3c7f79637721
Create Date: 2026-07-17

Chat ownership remains on ``chat_sessions.user_id``. These rows grant additional
same-organisation users permission to read the canonical session and its messages.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637720"
down_revision: str | None = "3c7f79637721"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable viewer grants without changing chat ownership."""
    op.create_table(
        "chat_session_access",
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("viewer_user_id", sa.UUID(), nullable=False),
        sa.Column("granted_by_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.session_id"],
            name="fk_chat_session_access_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "session_id",
            "viewer_user_id",
            name="pk_chat_session_access",
        ),
    )
    op.create_index(
        "ix_chat_session_access_viewer_chats",
        "chat_session_access",
        ["organisation_id", "viewer_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove chat viewer grants and restore the pre-sharing schema."""
    op.drop_index(
        "ix_chat_session_access_viewer_chats",
        table_name="chat_session_access",
    )
    op.drop_table("chat_session_access")
