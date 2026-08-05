"""Lead domain schemas for CRM lead listing, conversion, and mutations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from src.api.schemas.email_name_fallback import EmailNameFallbackModel
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLeadParticipantType,
    CRMLeadPriority,
    CRMLeadSourceType,
    CRMLeadStatus,
)


class LeadParticipantItem(EmailNameFallbackModel):
    """Participant linked to a lead."""

    participant_id: UUID
    lead_id: UUID
    participant_type: CRMLeadParticipantType
    relationship_role: str | None
    is_primary: bool
    related_local_id: UUID | None
    name: str | None
    email: str | None
    phone: str | None
    job_title: str | None
    company_name: str | None
    company_domain: str | None
    source: str | None
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LeadListItem(EmailNameFallbackModel):
    """Single lead in a list response."""

    lead_id: UUID
    status: CRMLeadStatus
    source_type: CRMLeadSourceType
    source_channel: str | None
    source_name: str | None
    source_ref: str | None
    priority: CRMLeadPriority | None
    score: float | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str | None
    phone: str | None
    job_title: str | None
    company_name: str | None
    company_domain: str | None
    company_website: str | None
    industry: str | None
    country: str | None
    region: str | None
    city: str | None
    message: str | None
    interest: str | None
    budget_amount: float | None
    budget_currency: str | None
    owner_id: UUID | None
    owner_name: str | None
    owner_email: str | None
    assigned_at: datetime | None
    contact_id: UUID | None
    company_id: UUID | None
    converted_deal_id: UUID | None
    converted_at: datetime | None
    disqualified_reason: str | None
    archive_reason: str | None
    form_id: UUID | None
    form_version_id: UUID | None
    form_submission_id: UUID | None
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    utm_term: str | None
    utm_content: str | None
    attribution: dict[str, Any] = Field(default_factory=dict)
    consent: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime
    last_activity_at: datetime | None
    last_contacted_at: datetime | None
    qualified_at: datetime | None
    disqualified_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LeadDetail(LeadListItem):
    """Extended lead detail including participants."""

    participants: list[LeadParticipantItem] = Field(default_factory=list)


class LeadsPage(BaseModel):
    """Paginated list of leads."""

    items: list[LeadListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class CreateLeadRequest(BaseModel):
    """Request to create a lead."""

    source_type: CRMLeadSourceType = CRMLeadSourceType.MANUAL
    source_channel: str | None = None
    source_name: str | None = None
    source_ref: str | None = None
    status: CRMLeadStatus = CRMLeadStatus.NEW
    priority: CRMLeadPriority | None = None
    score: float | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    company_website: str | None = None
    industry: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    message: str | None = None
    interest: str | None = None
    budget_amount: float | None = None
    budget_currency: str | None = None
    owner_id: UUID | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    contact_id: UUID | None = None
    company_id: UUID | None = None
    form_id: UUID | None = None
    form_version_id: UUID | None = None
    form_submission_id: UUID | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_term: str | None = None
    utm_content: str | None = None
    attribution: dict[str, Any] = Field(default_factory=dict)
    consent: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def reject_converted_status(cls, status: CRMLeadStatus) -> CRMLeadStatus:
        """Lead conversion owns converted status because it must stamp deal metadata."""
        if status == CRMLeadStatus.CONVERTED:
            raise ValueError("Use the lead conversion endpoint to set converted status")
        return status


class UpdateLeadRequest(BaseModel):
    """Request to update a lead."""

    source_channel: str | None = None
    source_name: str | None = None
    source_ref: str | None = None
    status: CRMLeadStatus | None = None
    priority: CRMLeadPriority | None = None
    score: float | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    company_website: str | None = None
    industry: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    message: str | None = None
    interest: str | None = None
    budget_amount: float | None = None
    budget_currency: str | None = None
    owner_id: UUID | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    contact_id: UUID | None = None
    company_id: UUID | None = None
    disqualified_reason: str | None = None
    archive_reason: str | None = None
    attribution: dict[str, Any] | None = None
    consent: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None

    @field_validator("status")
    @classmethod
    def reject_converted_status(cls, status: CRMLeadStatus | None) -> CRMLeadStatus | None:
        """Lead conversion owns converted status because it must stamp deal metadata."""
        if status == CRMLeadStatus.CONVERTED:
            raise ValueError("Use the lead conversion endpoint to set converted status")
        return status


class CreateLeadParticipantRequest(BaseModel):
    """Request to add a participant to a lead."""

    participant_type: CRMLeadParticipantType
    relationship_role: str = "participant"
    is_primary: bool = False
    related_local_id: UUID | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    job_title: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    source: str = "manual"
    properties: dict[str, Any] = Field(default_factory=dict)


class ConvertLeadRequest(BaseModel):
    """Request to convert a lead into an internal CRM deal."""

    deal_name: str | None = Field(default=None, min_length=1, max_length=500)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    owner_user_id: UUID | None = None
    expected_close_date: datetime | None = None
    deal_type: str | None = None
    priority: str | None = None
    custom_properties: dict[str, Any] | None = None


class LeadConversionResponse(BaseModel):
    """Response returned after converting a lead to a deal."""

    lead_id: UUID
    status: CRMLeadStatus
    converted_deal_id: UUID
    converted_at: datetime
    copied_participant_count: int = 0
