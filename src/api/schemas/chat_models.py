from __future__ import annotations

from uuid import UUID
from datetime import datetime
from typing import Optional, List, Dict, Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.generics import GenericModel as _Base

from src.domain.models.organisation_tables.chat_session_models import ChatType
from src.domain.models.organisation_tables.chat_message_models import ChatMessageRole, ChatMessageType
T = TypeVar("T")


class ChatSessionCreate(BaseModel):
    """Create payload for a chat session."""

    title: str = Field(..., description="The title of the chat session")
    tags: Optional[List[str]] = None


class ChatSessionOut(BaseModel):
    """Response model mirroring ChatSession domain model."""

    session_id: UUID
    title: str
    chat_type: str
    is_favorite: bool = False
    tags: List[str] = []
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
    chats: List[ChatSessionOut]
    cursor: Optional[str] = None


class PaginationChatMessages(_Base, Generic[T]):
    chat: Dict[str, Any]
    cursor: Optional[str] = None


class ChatSessionsPage(BaseModel):
    items: List[ChatSessionOut]
    cursor: Optional[str] = None


# Chat messages (API layer)
class SendMessageRequest(BaseModel):
    """Request model for sending a chat message."""
    session_id: UUID
    content: str
    attachments: Optional[List[UUID]] = None
    message_type: Optional[ChatMessageType] = ChatMessageType.GENERAL_MESSAGE
    role: ChatMessageRole = ChatMessageRole.USER
    is_edited: bool = False
    edited_message_id: Optional[UUID] = None

    @field_validator('content')
    def content_must_not_be_empty(cls, v: str) -> str:
        stripped_v = v.strip()
        if not stripped_v:
            raise ValueError('content must not be empty')
        return stripped_v

    @field_validator('role')
    def role_must_be_user(cls, v: ChatMessageRole) -> ChatMessageRole:
        if v != ChatMessageRole.USER:
            raise ValueError('role must be USER')
        return v

    @model_validator(mode='after')
    def check_edited_message_id_required(self) -> "SendMessageRequest":
        if self.is_edited and not self.edited_message_id:
            raise ValueError("edited_message_id is required when is_edited is True")
        return self


class SendMessageResponse(BaseModel):
    message_id: UUID
    job_id: UUID
