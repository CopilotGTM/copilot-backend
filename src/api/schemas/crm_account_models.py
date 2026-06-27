from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class AccountContactItem(BaseModel):
    contact_id: UUID
    full_name: str | None
    email: str | None
    job_title: str | None
    deal_count: int = 0


class AccountDetailContactItem(BaseModel):
    contact_id: UUID
    full_name: str | None
    email: str | None
    job_title: str | None


class AccountDealItem(BaseModel):
    deal_id: UUID
    name: str | None
    amount: float | None
    currency: str | None
    deal_status: str | None
    stage_name: str | None
    owner_name: str | None


class AccountListOwner(BaseModel):
    id: UUID | None
    name: str | None
    email: str | None


class AccountListItem(BaseModel):
    company_id: UUID
    name: str | None
    domain: str | None
    website: str | None
    industry: str | None
    lifecycle_stage: str | None
    owner_name: str | None
    owner: AccountListOwner | None = None
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


class AccountSummary(BaseModel):
    status: str = Field("not_available")
    content: str | None = None
    last_researched_at: datetime | None = None


class AccountDetail(AccountListItem):
    description: str | None
    company_type: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    timezone: str | None
    account_summary: AccountSummary = Field(default_factory=AccountSummary)
    contacts: list[AccountDetailContactItem] = Field(default_factory=list)
    deals: list[AccountDealItem] = Field(default_factory=list)


class AccountsPage(BaseModel):
    items: list[AccountListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class AccountSearchItem(BaseModel):
    company_id: UUID
    name: str | None
    domain: str | None
    website: str | None
    industry: str | None


class AccountSearchPage(BaseModel):
    items: list[AccountSearchItem]
    next_cursor: str | None = None
    has_more: bool = False


class CreateAccountRequest(BaseModel):
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    linkedin_url: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_identity(self) -> CreateAccountRequest:
        if self.name:
            return self
        if self.domain and self.domain.strip():
            return self
        if self.website and self.website.strip():
            return self
        raise ValueError("name, domain, or website is required")


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
