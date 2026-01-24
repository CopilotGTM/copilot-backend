"""CRM company request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CompanyCreateRequest(BaseModel):
    """Request payload to create an internal CRM company."""

    name: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    website: str | None = None
    description: str | None = None
    industry: str | None = None
    company_type: str | None = None
    lifecycle_stage: str | None = None
    owner_user_id: UUID | None = None
    number_of_employees: int | None = None
    annual_revenue: float | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    address: str | None = None
    postal_code: str | None = None
    timezone: str | None = None
    linkedin_url: str | None = None
    is_active: bool | None = None
    properties: dict[str, Any] | None = None


class CompanyUpdateRequest(BaseModel):
    """Request payload to update an internal CRM company."""

    name: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    website: str | None = None
    description: str | None = None
    industry: str | None = None
    company_type: str | None = None
    lifecycle_stage: str | None = None
    owner_user_id: UUID | None = None
    number_of_employees: int | None = None
    annual_revenue: float | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    address: str | None = None
    postal_code: str | None = None
    timezone: str | None = None
    linkedin_url: str | None = None
    is_active: bool | None = None
    properties: dict[str, Any] | None = None


class CompanyListItem(BaseModel):
    """Simplified company model for selection lists."""

    company_id: UUID
    external_id: str
    name: str | None = None
    domain: str | None = None
    industry: str | None = None
    owner_user_id: UUID | None = None

    model_config = {"from_attributes": True}


class CompanyWriteResponse(BaseModel):
    """Response payload for company create/update/delete."""

    company_id: UUID
    external_id: str
    name: str | None = None
    domain: str | None = None
    industry: str | None = None
    owner_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CRMCompaniesPage(BaseModel):
    """Paginated list of CRM companies."""

    items: list[CompanyListItem]
    cursor: str | None = Field(default=None, description="Cursor for next page")
