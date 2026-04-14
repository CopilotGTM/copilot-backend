from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AccountContactItem(BaseModel):
    contact_id: UUID
    full_name: str | None
    email: str | None
    job_title: str | None
    deal_count: int = 0


class AccountDealItem(BaseModel):
    deal_id: UUID
    name: str | None
    amount: float | None
    currency: str | None
    deal_status: str | None
    stage_name: str | None
    owner_name: str | None


class AccountListItem(BaseModel):
    company_id: UUID
    name: str | None
    domain: str | None
    website: str | None
    industry: str | None
    lifecycle_stage: str | None
    owner_name: str | None
    number_of_employees: int | None
    annual_revenue: float | None
    phone: str | None
    country: str | None
    linkedin_url: str | None
    is_active: bool = True
    contact_count: int = Field(0)
    deal_count: int = Field(0)
    pipeline_value: float = Field(0.0)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AccountDetail(AccountListItem):
    description: str | None
    company_type: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    timezone: str | None
    contacts: list[AccountContactItem] = Field(default_factory=list)
    deals: list[AccountDealItem] = Field(default_factory=list)


class AccountsPage(BaseModel):
    items: list[AccountListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class CreateAccountRequest(BaseModel):
    name: str
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    linkedin_url: str | None = None


class UpdateAccountRequest(BaseModel):
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    description: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    number_of_employees: int | None = None
    annual_revenue: float | None = None


class AccountSuggestion(BaseModel):
    """Suggested account match for a contact based on email domain."""

    company_id: UUID
    name: str | None
    domain: str | None
    match_type: str = Field(default="domain_match")
