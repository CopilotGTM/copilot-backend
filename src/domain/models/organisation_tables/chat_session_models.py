from uuid import UUID, uuid4
from typing import List, Dict, Any, TYPE_CHECKING
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .chat_message_models import ChatMessage


class ChatSession(TimestampMixin, table=True):
    __tablename__ = "chat_sessions"

    session_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    user_id: UUID = Field(nullable=False, index=True)
    # must match organisations.organisation_id (assumed UUID)
    organisation_id: UUID = Field(
        nullable=False,
        index=True,
    )

    title: str = Field(default="Untitled Chat")
    chat_type: str = Field(default="GENERAL_CHAT", index=True)
    chat_version: str = Field(default="v1", index=True)

    # JSONB columns must be declared explicitly
    properties: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
    )
    is_deleted: bool = Field(default=False, index=True)

    # back‑ref to messages
    messages: List["ChatMessage"] = Relationship(back_populates="session")