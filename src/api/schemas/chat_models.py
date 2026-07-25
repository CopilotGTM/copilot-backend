from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.job_models import JobStatus

T = TypeVar("T")


class ChatSessionCreate(BaseModel):
    """Create payload for a chat session."""

    title: str = Field(..., description="The title of the chat session")
    tags: list[str] | None = None


class ChatSessionCapabilities(BaseModel):
    """Operations the current caller may perform on one chat session."""

    read: bool
    send: bool
    rename: bool
    delete: bool
    manage_shares: bool


class ChatSessionOut(BaseModel):
    """Response model mirroring ChatSession domain model."""

    session_id: UUID
    title: str
    chat_type: str
    is_favorite: bool = False
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime
    access: Literal["owner", "viewer"] = "owner"
    capabilities: ChatSessionCapabilities

    # Pydantic v2 config
    model_config = {"from_attributes": True}


class ChatSessionShareOut(BaseModel):
    """One explicit read-only viewer grant for a chat session."""

    session_id: UUID
    viewer_user_id: UUID
    granted_by_user_id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSharedByOut(BaseModel):
    """User who granted the current caller access to a chat."""

    user_id: UUID
    name: str | None = None
    email: str | None = None


class ChatSharedWithMeOut(ChatSessionOut):
    """Shared chat metadata enriched for the settings screen."""

    shared_at: datetime
    shared_by: ChatSharedByOut


class SharedChatsPage(BaseModel):
    items: list[ChatSharedWithMeOut]
    cursor: str | None = None


class DeleteChat(BaseModel):
    session_id: UUID


class RenameChat(BaseModel):
    session_id: UUID
    title: str


class FavoriteChat(BaseModel):
    session_id: UUID
    favorite: bool


# Backwards-compat aliases (legacy names)
CreateChat = ChatSessionCreate
ChatSessionResponse = ChatSessionOut


# Legacy pagination helpers kept for compatibility with older handlers
class Pagination(BaseModel, Generic[T]):
    chats: list[ChatSessionOut]
    cursor: str | None = None


class PaginationChatMessages(BaseModel, Generic[T]):
    chat: dict[str, Any]
    cursor: str | None = None


class ChatSessionsPage(BaseModel):
    items: list[ChatSessionOut]
    cursor: str | None = None


# Chat messages (API layer)
class SendMessageRequest(BaseModel):
    """Request model for sending a chat message."""

    session_id: UUID
    content: str
    attachments: list[UUID] | None = None
    message_type: ChatMessageType | None = ChatMessageType.GENERAL_MESSAGE
    role: ChatMessageRole = ChatMessageRole.USER
    is_edited: bool = False
    edited_message_id: UUID | None = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v: str) -> str:
        stripped_v = v.strip()
        if not stripped_v:
            raise ValueError("content must not be empty")
        return stripped_v

    @field_validator("role")
    @classmethod
    def role_must_be_user(cls, v: ChatMessageRole) -> ChatMessageRole:
        if v != ChatMessageRole.USER:
            raise ValueError("role must be USER")
        return v

    @model_validator(mode="after")
    def check_edited_message_id_required(self) -> SendMessageRequest:
        if self.is_edited and not self.edited_message_id:
            raise ValueError("edited_message_id is required when is_edited is True")
        return self


class SendMessageResponse(BaseModel):
    """Response after sending a message with IDs for frontend tracking."""

    job_id: UUID = Field(..., description="Job ID for SSE streaming connection")
    user_message_id: UUID = Field(..., description="Created user message ID")
    assistant_message_id: UUID = Field(
        ..., description="Pre-created assistant message placeholder ID"
    )


class ChatMessageListItem(BaseModel):
    """Slim message item for lists/infinite scroll."""

    message_id: UUID
    session_id: UUID
    role: ChatMessageRole
    message_type: ChatMessageType
    content: str
    is_edited: bool = False
    created_at: datetime
    updated_at: datetime
    # Enriched fields
    attachments: list[ChatAttachmentView] = []
    confidence_score: Literal["high", "medium", "low", "neutral"] | None = Field(
        default=None,
        description="Confidence score: high, medium, low, or neutral (for greetings/pleasantries)",
    )
    sources: list[dict[str, Any]] | None = None
    suggestions: list[str] | None = None
    feedback_score: int | None = Field(
        default=None,
        description="User feedback: 1 (thumbs up), -1 (thumbs down), None (no feedback)",
    )


class ActiveChatJob(BaseModel):
    """Durable identifiers needed to resume an unfinished chat response."""

    job_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    message_type: ChatMessageType
    status: JobStatus


class ChatMessagesPage(BaseModel):
    """Authorized session metadata with one cursor page of committed messages."""

    session: ChatSessionOut
    items: list[ChatMessageListItem]
    cursor: str | None = None
    active_jobs: list[ActiveChatJob] = Field(default_factory=list)


class ChatAttachmentView(BaseModel):
    attachment_id: UUID
    filename: str
    mimetype: str
    thumbnail_small_url: str | None = None
    playback_url: str | None = None


class MessageFeedbackRequest(BaseModel):
    """Request model for providing feedback on a message."""

    message_id: UUID = Field(..., description="The message ID to provide feedback for")
    feedback_score: int = Field(
        ...,
        description="Feedback score: 1 for thumbs up, -1 for thumbs down, 0 to clear feedback",
        ge=-1,
        le=1,
    )

    @field_validator("feedback_score")
    @classmethod
    def validate_feedback_score(cls, v: int) -> int:
        if v not in {-1, 0, 1}:
            raise ValueError("feedback_score must be -1 (thumbs down), 0 (clear), or 1 (thumbs up)")
        return v


class MessageFeedbackResponse(BaseModel):
    """Response after updating message feedback."""

    message_id: UUID = Field(..., description="The message ID that was updated")
    feedback_score: int | None = Field(
        ..., description="The current feedback score (None if cleared)"
    )
