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


class ChatMessage(BaseModel):
    """
    Schema for a chat message from the frontend.
    """

    role: str = Field(..., description="Sender's role (user, assistant, etc.)")
    message_id: UUID = Field(..., description="Unique message ID")
    content: Optional[str] = Field(None, description="Text content of the message")
    timestamp: datetime = Field(..., description="Message sent timestamp (ISO8601)")
    attachments: List[AttachmentModel] = Field(default_factory=list, description="Attached files/images/etc.")
    is_edited: bool = Field(False, description="True if the message was edited after sending")
    edited_at: Optional[datetime] = Field(None, description="Last edit timestamp (if applicable)")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Extra info (reactions, tags, etc.)")


class ChatMessageReceived(BaseModel):
    """
    Schema for a received chat message event.
    """

    session_id: UUID = Field(..., description="Conversation thread ID")
    user_id: str = Field(..., description="Sender's user ID")
    message: ChatMessage = Field(..., description="Chat message object")
