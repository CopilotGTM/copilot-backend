from uuid import UUID, uuid4
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from datetime import datetime

from pydantic import BaseModel, HttpUrl, Field as PydanticField
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .chat_session_models import ChatSession


class AttachmentModel(BaseModel):
    """
    Predefined JSON schema for an attachment embedded in ChatMessage.attachments
    """

    filename: str
    url: HttpUrl
    mimetype: str
    size_bytes: Optional[int] = None
    uploaded_at: datetime
    metadata: Dict[str, Any] = PydanticField(default_factory=dict)


class ChatMessage(TimestampMixin, table=True):
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
    message_type: str = Field(default="GENERAL_MESSAGE", index=True)
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

    technical_deck_details: Dict[str, Any] = Field(
        sa_column=Column("technical_deck_details", JSONB),
        default_factory=dict,
    )
    rfp_details: Dict[str, Any] = Field(
        sa_column=Column("rfp_details", JSONB),
        default_factory=dict,
    )

    # back‑ref to session
    session: "ChatSession" = Relationship(back_populates="messages")