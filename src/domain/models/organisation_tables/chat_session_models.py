from enum import Enum
from uuid import UUID, uuid4
from typing import List, Dict, Any, TYPE_CHECKING
from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime
from sqlalchemy import Column, String, Index, Boolean, text
from sqlalchemy.dialects.postgresql import JSONB

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .chat_message_models import ChatMessage


class ChatType(str, Enum):
    """
    Enum for the type of chat session.
    """

    GENERAL_CHAT = "GENERAL_CHAT"
    TECHNICAL_DECK = "TECHNICAL_DECK"
    RFP_SHEET = "RFP_SHEET"


class ChatSession(TimestampMixin, table=True):
    """
    Represents a chat session, which contains a sequence of messages.
    """

    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_organisation_id_user_id", "organisation_id", "user_id"),
        {"comment": "Stores chat session metadata."},
    )

    session_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the chat session.",
    )
    user_id: UUID = Field(
        nullable=False,
        index=True,
        description="The ID of the user who created the session.",
    )
    organisation_id: UUID = Field(
        nullable=False,
        index=True,
        description="The ID of the organisation this session belongs to.",
    )

    title: str = Field(
        default="Untitled Chat",
        sa_column=Column(
            String(255), server_default=text("'Untitled Chat'"), nullable=False
        ),
        description="The title of the chat session.",
    )
    chat_type: ChatType = Field(
        default=ChatType.GENERAL_CHAT,
        sa_column=Column(
            String(50),
            index=True,
            server_default=text("'GENERAL_CHAT'"),
            nullable=False,
        ),
        description="The type of chat, used for categorization.",
    )
    chat_version: str = Field(
        default="v1",
        sa_column=Column(
            String(50), index=True, server_default=text("'v1'"), nullable=False
        ),
        description="Version of the chat schema.",
    )

    properties: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
        description="A flexible JSONB column for storing additional session properties.",
    )
    tags: List[str] = Field(
        sa_column=Column(JSONB, server_default=text("'[]'::jsonb")),
        default_factory=list,
        description="A list of tags for categorizing the chat session.",
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(
            Boolean, server_default=text("false"), nullable=False, index=True
        ),
        description=(
            "Flag to mark the session as deleted without permanent removal. "
            "All data-access queries should filter on this flag by default."
        ),
    )

    messages: List["ChatMessage"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
