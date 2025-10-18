from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.generics import GenericModel as _Base
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessageRole,
    ChatMessageType,
)

T = TypeVar("T")


class ChatSessionCreate(BaseModel):
    """Create payload for a chat session."""

    title: str = Field(..., description="The title of the chat session")
    tags: list[str] | None = None


class ChatSessionOut(BaseModel):
    """Response model mirroring ChatSession domain model."""

    session_id: UUID
    title: str
    chat_type: str
    is_favorite: bool = False
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
class Pagination(_Base, Generic[T]):
    chats: list[ChatSessionOut]
    cursor: str | None = None


class PaginationChatMessages(_Base, Generic[T]):
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
    def content_must_not_be_empty(self, v: str) -> str:
        stripped_v = v.strip()
        if not stripped_v:
            raise ValueError("content must not be empty")
        return stripped_v

    @field_validator("role")
    def role_must_be_user(self, v: ChatMessageRole) -> ChatMessageRole:
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
    confidence_score: str | None = None
    sources: list[dict[str, Any]] | None = None
    suggestions: list[str] | None = None


class ChatMessagesPage(BaseModel):
    items: list[ChatMessageListItem]
    cursor: str | None = None


class ChatAttachmentView(BaseModel):
    attachment_id: UUID
    filename: str
    mimetype: str
    thumbnail_small_url: str | None = None
