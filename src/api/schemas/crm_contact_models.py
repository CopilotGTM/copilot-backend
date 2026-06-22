from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ContactDealInvolvement(BaseModel):
    """A deal this contact participates in as a stakeholder."""

    deal_id: UUID
    deal_name: str | None
    deal_status: str | None
    stage_name: str | None
    participant_id: UUID
    stance: str | None
    decision_power: str | None
    relationship_role: str | None


class ContactListCompany(BaseModel):
    """Account context returned by the contacts list endpoint."""

    id: UUID | None
    name: str | None
    domain: str | None


class ContactListOwner(BaseModel):
    """Owner context returned by the contacts list endpoint."""

    id: UUID | None
    name: str | None
    email: str | None


class ContactListItem(BaseModel):
    """Single contact in a list response."""

    contact_id: UUID
    full_name: str | None
    email: str | None
    additional_emails: list[str] = Field(default_factory=list)
    job_title: str | None
    phone: str | None
    mobile_phone: str | None
    linkedin_url: str | None
    company: ContactListCompany | None
    owner: ContactListOwner | None
    deal_count: int = Field(0)
    last_contacted_at: datetime | None
    last_activity_at: datetime | None
    source: str = Field("crm_sync")
    is_profile_enriched: bool = Field(False)
    profile_enriched_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContactMutationItem(BaseModel):
    """Legacy contact shape returned by create and update endpoints."""

    contact_id: UUID
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str | None
    job_title: str | None
    phone: str | None
    company_name: str | None
    company_id: UUID | None
    company_domain: str | None
    lifecycle_stage: str | None
    owner_name: str | None
    deal_count: int = Field(0)
    source: str = Field("crm_sync")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContactDetail(ContactMutationItem):
    """Extended contact detail including deal involvement."""

    additional_emails: list[str] = Field(default_factory=list)
    mobile_phone: str | None
    linkedin_url: str | None
    profile_enrichment: dict[str, Any] = Field(default_factory=dict)
    profile_enrichment_source: str | None
    profile_enriched_at: datetime | None
    address: str | None
    city: str | None
    state: str | None
    country: str | None
    last_contacted_at: datetime | None
    last_activity_at: datetime | None
    deal_involvement: list[ContactDealInvolvement] = Field(default_factory=list)


class ContactsPage(BaseModel):
    """Paginated list of contacts."""

    items: list[ContactListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class CreateContactRequest(BaseModel):
    """Request to create a new contact."""

    email: str = Field(..., description="Email address (required)")
    name: str = Field(..., description="Full name (required)")
    job_title: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    company_id: UUID | None = None


class UpdateContactRequest(BaseModel):
    """Request to update an existing contact (all fields optional).

    email can only be set if the contact currently has no email.
    Changing an existing email is not allowed.
    """

    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    phone: str | None = None
    mobile_phone: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    company_id: UUID | None = None


class AddContactToDealRequest(BaseModel):
    """Request to associate a contact with a deal."""

    deal_id: UUID
    relationship_role: str | None = None
