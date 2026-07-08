"""Shared lead-form DTOs for API, service, agent, and worker layers.

These models intentionally live outside ``src.api`` because light-worker
runtime images do not package the API layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
)

MAX_ALLOWED_ORIGINS = 50
MAX_LEAD_FORM_FIELDS = 100
MAX_PUBLIC_CONTEXT_KEYS = 50
MAX_PUBLIC_FIELD_KEY_LENGTH = 128
MAX_PUBLIC_SCALAR_STRING_LENGTH = 10_000
MAX_PUBLIC_SUBMISSION_FIELDS = 200


class LeadFormFieldRequest(BaseModel):
    """Field mapping configured by an authenticated user."""

    external_key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    target_type: LeadFormFieldTargetType = LeadFormFieldTargetType.PROPERTY
    target_key: str | None = Field(default=None, max_length=128)
    data_type: LeadFormFieldDataType = LeadFormFieldDataType.TEXT
    required: bool = False
    display_order: int | None = None
    default_value: Any | None = None
    validation: dict[str, Any] = Field(default_factory=dict)


class CreateLeadFormRequest(BaseModel):
    """Create a public lead capture form and mapping."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: LeadFormStatus = LeadFormStatus.DRAFT
    allowed_origins: list[str] = Field(default_factory=list, max_length=MAX_ALLOWED_ORIGINS)
    settings: dict[str, Any] = Field(default_factory=dict)
    fields: list[LeadFormFieldRequest] = Field(
        default_factory=list, max_length=MAX_LEAD_FORM_FIELDS
    )


class UpdateLeadFormRequest(BaseModel):
    """Replace editable lead form metadata and field mapping."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: LeadFormStatus = LeadFormStatus.DRAFT
    allowed_origins: list[str] = Field(default_factory=list, max_length=MAX_ALLOWED_ORIGINS)
    settings: dict[str, Any] = Field(default_factory=dict)
    fields: list[LeadFormFieldRequest] = Field(
        default_factory=list, max_length=MAX_LEAD_FORM_FIELDS
    )


class UpdateLeadFormStatusRequest(BaseModel):
    """Change runtime availability for an existing lead form."""

    status: LeadFormStatus


class LeadFormFieldItem(BaseModel):
    """Field mapping returned to the authenticated UI."""

    field_id: UUID
    form_id: UUID
    external_key: str
    label: str
    target_type: LeadFormFieldTargetType
    target_key: str | None
    data_type: LeadFormFieldDataType
    required: bool
    display_order: int | None
    default_value: Any | None
    validation: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LeadFormItem(BaseModel):
    """Lead form definition returned to authenticated users."""

    form_id: UUID
    public_form_key: str = ""
    public_form_url: str = ""
    name: str
    description: str | None
    status: LeadFormStatus
    current_version: int
    allowed_origins: list[str] = Field(default_factory=list)
    settings: dict[str, Any]
    fields: list[LeadFormFieldItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PublicLeadFormFieldItem(BaseModel):
    """Public field metadata needed to render a browser form."""

    external_key: str
    label: str
    data_type: LeadFormFieldDataType
    required: bool
    display_order: int | None
    default_value: Any | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class PublicLeadFormDefinitionResponse(BaseModel):
    """Public form definition resolved from a browser-safe form key."""

    public_form_key: str
    name: str
    description: str | None
    current_version: int
    settings: dict[str, Any] = Field(default_factory=dict)
    fields: list[PublicLeadFormFieldItem] = Field(default_factory=list)


class PublicLeadFormSubmitRequest(BaseModel):
    """Browser/backend payload submitted to a public form key."""

    fields: dict[str, Any] = Field(default_factory=dict, max_length=MAX_PUBLIC_SUBMISSION_FIELDS)
    context: dict[str, Any] = Field(default_factory=dict, max_length=MAX_PUBLIC_CONTEXT_KEYS)
    idempotency_key: str | None = Field(default=None, max_length=255)


class PrivateLeadFormSubmitRequest(BaseModel):
    """Mapped private API payload submitted through a form-scoped API key."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any] = Field(default_factory=dict, max_length=MAX_PUBLIC_SUBMISSION_FIELDS)
    context: dict[str, Any] = Field(default_factory=dict, max_length=MAX_PUBLIC_CONTEXT_KEYS)


class PublicLeadFormSubmitResponse(BaseModel):
    """Public submission response. Keep it small to avoid leaking tenant internals."""

    submission_id: UUID
    status: Literal["accepted"] = "accepted"


class PrivateLeadFormSubmitResponse(BaseModel):
    """Private mapped form submission response with replay-safe identifiers."""

    submission_id: UUID
    lead_id: UUID
    status: Literal["accepted"] = "accepted"


class PublicLeadFormError(BaseModel):
    """Stable public error payload for unauthenticated form clients."""

    code: str
    message: str
    field: str | None = None


class PublicLeadFormErrorResponse(BaseModel):
    """Public error wrapper, separate from the authenticated app GenericResponse."""

    error: PublicLeadFormError
