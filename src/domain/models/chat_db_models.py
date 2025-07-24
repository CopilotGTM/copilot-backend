# app_layer/models/chat_db_models.py
import uuid
from uuid import UUID, uuid4
from datetime import datetime
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, HttpUrl, Field
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB


def utcnow() -> datetime:
    return datetime.utcnow()


class AttachmentModel(BaseModel):
    """
    Predefined JSON schema for an attachment embedded in ChatMessage.attachments
    """

    filename: str
    url: HttpUrl
    mimetype: str
    size_bytes: Optional[int] = None
    uploaded_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatSession(SQLModel, table=True):
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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # back‑ref to messages
    messages: List["ChatMessage"] = Relationship(back_populates="session")


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    message_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    session_id: UUID = Field(
        foreign_key="chat_sessions.session_id",
        index=True,
    )

    role: str = Field(default="user")
    message_type: str = Field(default="GENERAL_MESSAGE", index=True)
    message_version: int = Field(default=1)

    content: str

    # embed attachments as JSONB
    attachments: List[AttachmentModel] = Field(
        sa_column=Column(JSONB),
        default_factory=list,
    )
    extra: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
    )

    # timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    technical_deck_details: Dict[str, Any] = Field(
        sa_column=Column("technical_deck_details", JSONB),
        default_factory=dict,
    )
    rfp_details: Dict[str, Any] = Field(
        sa_column=Column("rfp_details", JSONB),
        default_factory=dict,
    )

    # back‑ref to session
    session: ChatSession = Relationship(back_populates="messages")
