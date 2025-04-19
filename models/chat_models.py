# models/chat_models.py

from uuid import UUID
from datetime import datetime
from typing import Dict, Any

from pydantic import BaseModel, EmailStr


class CreateChat(BaseModel):
    title: str


class AccessTokenResponse(BaseModel):
    access_token: str = None
    refresh_token: str = None
    token_type: str
    expires_in: int


class ChatSessionResponse(BaseModel):
    session_id: UUID
    user_id: UUID
    organisation_id: UUID
    title: str
    chat_type: str
    chat_version: str
    properties: Dict[str, Any]
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class DeleteChat(BaseModel):
    session_id: UUID