"""CRM contact request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from src.api.schemas.crm_models import ContactListItem


class ContactCreateRequest(BaseModel):
    """Request payload to create an internal CRM contact."""

    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    additional_emails: list[str] = Field(default_factory=list)
    phone: str | None = None
    mobile_phone: str | None = None
    website: str | None = None
    owner_user_id: UUID | None = None
    lifecycle_stage: str | None = None
    lead_status: str | None = None
    marketing_status: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    timezone: str | None = None
    properties: dict[str, Any] | None = None


class ContactUpdateRequest(BaseModel):
    """Request payload to update an internal CRM contact."""

    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    additional_emails: list[str] | None = None
    phone: str | None = None
    mobile_phone: str | None = None
    website: str | None = None
    owner_user_id: UUID | None = None
    lifecycle_stage: str | None = None
    lead_status: str | None = None
    marketing_status: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    timezone: str | None = None
    properties: dict[str, Any] | None = None


class ContactWriteResponse(BaseModel):
    """Response payload for contact create/update/delete."""

    contact_id: UUID
    external_id: str
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    job_title: str | None = None
    company_name: str | None = None
    owner_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CRMContactsPage(BaseModel):
    """Paginated list of CRM contacts."""

    items: list[ContactListItem]
    cursor: str | None = Field(default=None, description="Cursor for next page")
