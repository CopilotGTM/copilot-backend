"""DTOs and errors for direct mailbox email sending."""

from __future__ import annotations

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
