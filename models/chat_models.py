# models/chat_models.py

from uuid import UUID
from datetime import datetime
from typing import Dict, Any

from pydantic import BaseModel, EmailStr
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, HttpUrl


class AttachmentModel(BaseModel):
    """
    Schema for an attachment in ChatMessage.attachments.
    """

    filename: str = Field(..., description="Original filename")
    url: HttpUrl = Field(..., description="Publicly accessible URL")
    mimetype: str = Field(..., description="MIME type, e.g. 'image/png'")
    size_bytes: Optional[int] = Field(None, description="File size in bytes")
    uploaded_at: datetime = Field(..., description="Upload timestamp (ISO8601)")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Free-form metadata")

class ParseTranscript(BaseModel):
    attachments: List[AttachmentModel]
    session_id: UUID
    message_id: UUID


class CreateChat(BaseModel):
    title: str


class GetChat(BaseModel):
    session_id: str


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


class UploadFileURL(BaseModel):
    file_name: str
    file_type: str
    file_size: int
    session_id: UUID
