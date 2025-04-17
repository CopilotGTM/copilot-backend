import uuid
from uuid import UUID

from typing import List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, HttpUrl

from sqlmodel import SQLModel, Field, Column, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID


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
    __table_args__ = (
        Index("ix_chat_sessions_user", "user_id"),
    )

    session_id: UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True)
    )
    user_id: str = Field(index=True, nullable=False)
    title: str = Field(default="Untitled Chat")
    properties: Dict[str, Any] = Field(
        sa_column=Column(JSONB), default_factory=dict
    )
    is_deleted: bool = Field(
        default=False,
        index=True,
        description="Soft-delete flag; True if session has been deleted"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_ts", "session_id", "timestamp"),
    )

    message_id: UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True)
    )
    session_id: UUID = Field(
        foreign_key="chat_sessions.session_id", index=True
    )

    role: str = Field(default="user")
    message_type: str = Field(default="default", index=True)
    message_version: int = Field(default=1)

    content: str
    # Embed attachments as a JSONB array of AttachmentModel
    attachments: List[AttachmentModel] = Field(
        sa_column=Column(JSONB), default_factory=list
    )
    extra: Dict[str, Any] = Field(
        sa_column=Column(JSONB), default_factory=dict
    )

    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
