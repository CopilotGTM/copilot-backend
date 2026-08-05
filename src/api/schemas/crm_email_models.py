"""Read-only email payloads used by CRM sidebar views."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field
from src.api.schemas.email_name_fallback import EmailNameFallbackModel


class CRMEmailItem(EmailNameFallbackModel):
    """Bounded message data safe to render in the CRM email sidebar."""

    email_id: UUID
    integration_id: UUID
    internet_message_id: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    from_name: str | None = None
    from_email: str
    to_emails: list[str] = Field(default_factory=list)
    cc_emails: list[str] = Field(default_factory=list)
    received_at: datetime
    sent_at: datetime | None = None
    direction: str
    is_read: bool
    is_important: bool
    has_attachments: bool
    labels: list[str] = Field(default_factory=list)
    snippet: str | None = None
    body_preview: str | None = None


class CRMEmailPage(BaseModel):
    """One cursor-paginated page of sidebar email messages."""

    items: list[CRMEmailItem]
    cursor: str | None = Field(None, description="Cursor for the next page; null when exhausted")
