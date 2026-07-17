"""Read-only viewer grants for durable chat sessions.

Chat ownership remains on ``ChatSession.user_id``. This table only records which
additional users may read the same canonical session and future messages.
"""

from uuid import UUID

from sqlalchemy import Index
from sqlmodel import Field

from .base_mixins import CreatedAtMixin


class ChatSessionAccess(CreatedAtMixin, table=True):
    """Grant one user read-only access to one chat session."""

    __tablename__ = "chat_session_access"
    __table_args__ = (
        Index(
            "ix_chat_session_access_viewer_chats",
            "organisation_id",
            "viewer_user_id",
            "created_at",
        ),
        {"comment": "Explicit read-only viewers for chat sessions."},
    )

    organisation_id: UUID = Field(nullable=False)
    session_id: UUID = Field(
        foreign_key="chat_sessions.session_id",
        primary_key=True,
    )
    viewer_user_id: UUID = Field(primary_key=True)
    granted_by_user_id: UUID = Field(nullable=False)
