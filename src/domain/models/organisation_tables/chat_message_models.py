from enum import Enum
from uuid import UUID, uuid4
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from datetime import datetime

from pydantic import BaseModel, HttpUrl, Field as PydanticField
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, String, Text, CheckConstraint, Boolean, text
from sqlalchemy.dialects.postgresql import JSONB

from .base_mixins import TimestampMixin
from .job_models import ChatMessageJob

if TYPE_CHECKING:
    from .chat_session_models import ChatSession


class ChatMessageRole(str, Enum):
    """
    Enum for the role of the message sender.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessageType(str, Enum):
    """
    Enum for the type of chat message.
    """

    GENERAL_MESSAGE = "GENERAL_MESSAGE"
    TECHNICAL_DECK = "TECHNICAL_DECK"
    RFP_SHEET = "RFP_SHEET"


class ChatMessageAttachmentLink(SQLModel, table=True):
    """
    Association table for the many-to-many relationship between
    chat messages and attachments.
    """

    __tablename__ = "chat_message_attachment_link"

    message_id: UUID = Field(foreign_key="chat_messages.message_id", primary_key=True)
    attachment_id: UUID = Field(
        foreign_key="attachments.attachment_id", primary_key=True
    )
    attachment_order: int = Field(
        default=0, description="Display order of attachment in message"
    )


class AttachmentStatus(str, Enum):
    """Lifecycle states for an attachment.

    PENDING_UPLOAD   -> Record created; client has not (necessarily) uploaded blob yet.
    UPLOADED         -> Blob is confirmed present (basic properties fetched) and ready for processing.
    PROCESSING       -> Background tasks are performing checksum.
    COMPLETED        -> All processing succeeded; source ready for retrieval / search.
    FAILED           -> Terminal error state.
    """

    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Attachment(TimestampMixin, table=True):
    """
    Represents a centrally-managed file attachment.
    """

    __tablename__ = "attachments"

    attachment_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the attachment.",
    )
    organisation_id: UUID = Field(
        index=True, description="The organisation this attachment belongs to."
    )
    user_id: UUID = Field(
        index=True, description="The user who uploaded the attachment."
    )
    filename: str = Field(description="The original filename of the attachment.")
    storage_path: str = Field(
        unique=True,
        description="The path to the file in the storage backend (e.g., S3 key).",
    )
    mimetype: str = Field(description="The MIME type of the file.")
    size_bytes: int = Field(description="The size of the file in bytes.")
    attachment_metadata: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
        description="Additional metadata for the attachment.",
    )
    checksum: Optional[str] = Field(
        default=None, description="File checksum for integrity verification"
    )
    virus_scan_status: Optional[str] = Field(
        default=None, description="Virus scan status: pending/clean/infected"
    )
    retention_policy: Optional[str] = Field(
        default=None, description="Data retention policy identifier"
    )
    status: AttachmentStatus = Field(
        default=AttachmentStatus.PENDING_UPLOAD,
        sa_column=Column(
            String(50),
            index=True,
            server_default=text("'pending_upload'"),
            nullable=False,
        ),
        description="The current processing status of the document.",
    )
    thumbnail_small_path: Optional[str] = Field(
        default=None, description="Blob path for a small thumbnail (e.g., 128px)"
    )
    thumbnail_medium_path: Optional[str] = Field(
        default=None, description="Blob path for a medium thumbnail (e.g., 512px)"
    )
    processing_error: Optional[str] = Field(
        default=None, description="Error details if processing failed"
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
        description="Soft delete flag",
    )
    deleted_at: Optional[datetime] = Field(
        default=None, description="Soft delete timestamp"
    )

    messages: List["ChatMessage"] = Relationship(
        back_populates="attachments", link_model=ChatMessageAttachmentLink
    )


class ChatMessage(TimestampMixin, table=True):
    """
    Represents a single message within a chat session.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("message_version > 0", name="chat_message_version_positive"),
        {"comment": "Stores individual chat messages for all sessions."},
    )

    message_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the chat message.",
    )
    session_id: UUID = Field(
        foreign_key="chat_sessions.session_id",
        index=True,
        description="Foreign key linking to the chat session.",
    )
    parent_message_id: Optional[UUID] = Field(
        default=None,
        foreign_key="chat_messages.message_id",
        description="The ID of the parent message if this is a reply.",
    )

    role: ChatMessageRole = Field(
        default=ChatMessageRole.USER,
        sa_column=Column(String(50), server_default=text("'user'"), nullable=False),
        description="The role of the message sender (e.g., user, assistant).",
    )
    message_type: ChatMessageType = Field(
        default=ChatMessageType.GENERAL_MESSAGE,
        sa_column=Column(
            String(50),
            index=True,
            server_default=text("'GENERAL_MESSAGE'"),
            nullable=False,
        ),
        description="The type of message, used for categorization.",
    )
    message_version: int = Field(
        default=1, description="Version of the message schema."
    )

    content: Dict[str, Any] = Field(
        sa_column=Column(JSONB, nullable=False),
        description="The JSON content of the message.",
    )
    is_edited: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
        description="Flag to indicate if the message has been edited.",
    )
    feedback_score: Optional[int] = Field(
        default=None,
        description="User feedback score for the message, e.g., +1 or -1.",
    )
    token_usage: Optional[Dict[str, int]] = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Token usage for the message, e.g., {'prompt_tokens': 100, 'completion_tokens': 200}.",
    )
    model_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="The name of the model that generated this message.",
    )

    attachments: List["Attachment"] = Relationship(
        back_populates="messages", link_model=ChatMessageAttachmentLink
    )
    extra: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
        description="Extra metadata for the message.",
    )

    agent_data: Dict[str, Any] = Field(
        sa_column=Column("agent_data", JSONB),
        default_factory=dict,
        description="Stores agent-specific data, structure depends on message_type.",
    )

    # back‑ref to session
    session: "ChatSession" = Relationship(back_populates="messages")

    # self-referencing relationship for message threads
    parent_message: Optional["ChatMessage"] = Relationship(
        back_populates="replies",
        sa_relationship_kwargs=dict(remote_side="ChatMessage.message_id"),
    )
    replies: List["ChatMessage"] = Relationship(back_populates="parent_message")

    # Relationship to ChatMessageJob
    jobs: List["ChatMessageJob"] = Relationship(back_populates="message")
