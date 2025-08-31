from __future__ import annotations

from uuid import UUID
from datetime import datetime
from typing import Optional, List, Dict, Any, Generic, TypeVar

from pydantic import BaseModel, Field
from pydantic.generics import GenericModel as _Base

from src.domain.models.organisation_tables.chat_session_models import ChatType

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
    tags: List[str] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DeleteChat(BaseModel):
    session_id: UUID


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
