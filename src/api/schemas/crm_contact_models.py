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
    last_activity_at: datetime | None
    participant_id: UUID
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


class ContactDetailCompany(ContactListCompany):
    """Account context returned by the contact detail endpoint."""

    website: str | None
    industry: str | None
    linkedin_url: str | None
    country: str | None


class ContactResearchSummary(BaseModel):
    """Contact-level research displayed in the main overview."""

    summary: str | None
    last_researched_at: datetime | None


class ContactProfileEnrichment(BaseModel):
    """Structured enrichment metadata for a contact profile."""

    is_enriched: bool
    source: str | None
    enriched_at: datetime | None
    data: dict[str, Any] = Field(default_factory=dict)


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


class ContactDetail(BaseModel):
    """Page-ready contact detail including research and compact deal involvement."""

    contact_id: UUID
    first_name: str | None
    last_name: str | None
    full_name: str | None
    job_title: str | None
    email: str | None
    additional_emails: list[str] = Field(default_factory=list)
    phone: str | None
    mobile_phone: str | None
    linkedin_url: str | None
    website: str | None
    source: str = Field("crm_sync")
    lifecycle_stage: str | None
    lead_status: str | None
    marketing_status: str | None
    company: ContactDetailCompany | None
    owner: ContactListOwner | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    timezone: str | None
    last_contacted_at: datetime | None
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source_created_at: datetime | None
    source_modified_at: datetime | None
    synced_at: datetime
    research_summary: ContactResearchSummary | None
    profile_enrichment: ContactProfileEnrichment
    deal_count: int = Field(0)
    deal_involvement: list[ContactDealInvolvement] = Field(default_factory=list)

    model_config = {"from_attributes": True}


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
