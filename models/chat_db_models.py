# app_layer/models/chat_db_models.py
import uuid
from uuid import UUID, uuid4
from datetime import datetime
from typing import List, Dict, Any

from pydantic import BaseModel, HttpUrl
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB


class AttachmentModel(BaseModel):
    """
    Predefined JSON schema for an attachment embedded in ChatMessage.attachments
    """

    filename: str
    url: HttpUrl
    mimetype: str
    size_bytes: int = None
    uploaded_at: datetime
    metadata: Dict[str, Any] = {}


class ChatSession(SQLModel, table=True):
    __tablename__ = "chat_sessions"

    session_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    user_id: str = Field(index=True, nullable=False)

    # must match organisations.organisation_id (assumed UUID)
    organisation_id: UUID = Field(
        foreign_key="organisations.organisation_id",
        nullable=False,
        index=True,
    )

    title: str = Field(default="Untitled Chat")
    chat_type: str = Field(default="general", index=True)
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
    message_type: str = Field(default="default", index=True)
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

    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)

    # back‑ref to session
    session: ChatSession = Relationship(back_populates="messages")
