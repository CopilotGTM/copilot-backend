"""DTOs for provider-backed email drafts and direct mailbox sending."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, field_validator

EMAIL_ADAPTER = TypeAdapter(EmailStr)


class EmailSendError(Exception):
    """Base error surfaced by email send services and tools."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class EmailSendAddress(BaseModel):
    """Email recipient with normalized address and optional display name."""

    email: str = Field(max_length=320)
    name: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return str(EMAIL_ADAPTER.validate_python(value)).strip().lower()


class EmailSendCreateRequest(BaseModel):
    """Service-facing request contract for creating a new email."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    organisation_id: UUID
    requested_by_user_id: UUID
    to: list[EmailSendAddress] = Field(min_length=1, max_length=100)
    cc: list[EmailSendAddress] = Field(default_factory=list, max_length=100)
    bcc: list[EmailSendAddress] = Field(default_factory=list, max_length=100)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=100000)
    body_content_type: str = Field(default="Text")
    save_to_sent_items: bool = True
    deal_id: UUID | None = None
    sender_email: str | None = Field(default=None, max_length=320)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("subject", "body", mode="before")
    @classmethod
    def clean_required_string(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("value is required")
        return cleaned

    @field_validator("body_content_type")
    @classmethod
    def validate_body_content_type(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in {"Text", "HTML"}:
            raise ValueError("body_content_type must be Text or HTML")
        return cleaned

    @field_validator("sender_email")
    @classmethod
    def normalize_sender_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(EMAIL_ADAPTER.validate_python(value)).strip().lower()


class EmailSendCreateResult(BaseModel):
    """Result returned after a new email send request is accepted by the provider."""

    request_id: UUID | None = None
    status: str
    subject: str
    provider_status_code: int | None = None
    linked_record_type: str | None = None
    linked_record_id: UUID | None = None
    provider_resource_id: str | None = None


class EmailSendProviderResult(BaseModel):
    """Provider response details that should be persisted on the audit row."""

    provider_status_code: int
    provider_resource_id: str | None = None


class EmailDraftSaveRequest(EmailSendCreateRequest):
    """Create or replace one draft in the authenticated user's mailbox.

    `draft_id` is provider-owned and may only be used after the service resolves
    the current user's integration; callers cannot use it to select a mailbox.
    """

    draft_id: str | None = Field(default=None, max_length=1024)


class EmailDraftSnapshot(BaseModel):
    """Provider-normalized draft returned for review and confirmation."""

    draft_id: str
    sender_email: str | None = None
    to: list[EmailSendAddress] = Field(default_factory=list)
    cc: list[EmailSendAddress] = Field(default_factory=list)
    bcc: list[EmailSendAddress] = Field(default_factory=list)
    subject: str
    body: str
    body_content_type: str = "Text"
    content_hash: str
    web_link: str | None = None


class EmailDraftOperationResult(BaseModel):
    """Audit-aware result for a successful draft provider operation."""

    request_id: UUID | None = None
    status: str
    snapshot: EmailDraftSnapshot | None = None
    provider_status_code: int | None = None


def email_draft_content_hash(
    *,
    sender_email: str | None,
    to: list[EmailSendAddress],
    cc: list[EmailSendAddress],
    bcc: list[EmailSendAddress],
    subject: str,
    body: str,
    body_content_type: str,
) -> str:
    """Hash the exact user-visible draft fields that one approval authorizes."""

    payload = {
        "sender_email": sender_email,
        "to": [item.model_dump() for item in to],
        "cc": [item.model_dump() for item in cc],
        "bcc": [item.model_dump() for item in bcc],
        "subject": subject,
        "body": body,
        "body_content_type": body_content_type,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
