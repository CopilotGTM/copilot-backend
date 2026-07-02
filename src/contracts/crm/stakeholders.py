"""Shared stakeholder contracts for API, service, agent, and worker layers.

These DTOs intentionally live outside ``src.api`` so background workers can use
the same application contracts without depending on FastAPI or API-only code.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.service.crm.utils.account import PERSON_LINKEDIN_URL_ERROR, is_person_linkedin_url


class StakeholderLink(BaseModel):
    """External profile link attached to a stakeholder."""

    platform: str = Field(..., description="Platform name (e.g., linkedin, twitter, crm)")
    url: str = Field(..., description="The URL")
    label: str | None = Field(None, description="Optional display label")
    added_at: datetime | None = Field(default_factory=datetime.utcnow)


class DealJobResponse(BaseModel):
    """Generic deal job reference used by stakeholder contracts."""

    job_id: UUID | None = Field(None, description="Job ID for SSE subscription")
    job_type: str = Field(..., description="Type of job")
    status: str = Field(..., description="Current status of the job")


class StakeholderDetail(BaseModel):
    """Detailed view of a deal stakeholder."""

    participant_id: UUID = Field(..., description="Canonical CRMDealParticipant ID")
    deal_id: UUID
    contact_id: UUID | None = None
    email: str | None = None
    name: str | None = None
    job_title: str | None = None
    phone_number: str | None = Field(None, description="Phone number as provided")
    department: str | None = None
    linkedin_url: str | None = Field(None, description="LinkedIn profile URL")
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance
    decision_power: DecisionPower
    motivation: str | None = None
    pain_points: list[str] | None = None
    keywords: list[str] | None = None
    last_contact_at: datetime | None = None
    temperature: StakeholderTemperature
    source: StakeholderSource = Field(..., description="Stakeholder source")
    approval_status: ApprovalStatus = Field(..., description="Approval status")
    active_research_job: DealJobResponse | None = None
    stakeholder_research: str | None = None
    last_researched_at: datetime | None = None

    model_config = {"from_attributes": True}


class CreateStakeholderCommand(BaseModel):
    """Application command for manually creating a stakeholder."""

    contact_id: UUID | None = Field(None, description="Existing CRM contact to add")
    email: str | None = Field(None, description="Email address")
    name: str | None = None
    job_title: str | None = None
    phone_number: str | None = None
    linkedin_url: str | None = None
    stance: StakeholderStance = StakeholderStance.UNKNOWN
    decision_power: DecisionPower = DecisionPower.UNKNOWN
    department: str | None = None
    motivation: str | None = None
    pain_points: list[str] | None = None

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url(cls, value: str | None) -> str | None:
        """Accept only person profile LinkedIn URLs for stakeholder identity."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not is_person_linkedin_url(cleaned):
            raise ValueError(PERSON_LINKEDIN_URL_ERROR)
        return cleaned

    @model_validator(mode="after")
    def require_identity(self) -> CreateStakeholderCommand:
        """Require one explicit person key unless a CRM contact was selected."""
        if self.contact_id:
            return self
        if any(value and value.strip() for value in (self.email, self.linkedin_url, self.name)):
            return self
        raise ValueError("Provide a stakeholder name, email, LinkedIn URL, or contact ID")


class UpdateStakeholderCommand(BaseModel):
    """Application command for partially updating a stakeholder."""

    name: str | None = None
    job_title: str | None = None
    phone_number: str | None = None
    linkedin_url: str | None = None
    department: str | None = None
    stance: StakeholderStance | None = None
    decision_power: DecisionPower | None = None
    motivation: str | None = None
    pain_points: list[str] | None = None
    external_links: list[StakeholderLink] | None = None

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url(cls, value: str | None) -> str | None:
        """Accept only person profile LinkedIn URLs for stakeholder identity."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not is_person_linkedin_url(cleaned):
            raise ValueError(PERSON_LINKEDIN_URL_ERROR)
        return cleaned


class SkippedContact(BaseModel):
    """Details about a contact skipped during stakeholder creation."""

    contact_id: UUID = Field(..., description="ID of the skipped contact")
    reason: str = Field(..., description="Reason why the contact was skipped")


class AddContactsAsStakeholdersResponse(BaseModel):
    """Response for adding contacts as stakeholders."""

    added: list[StakeholderDetail] = Field(default_factory=list)
    skipped: list[SkippedContact] = Field(default_factory=list)
    total_requested: int
    total_added: int
    total_skipped: int


class StakeholderListResponse(BaseModel):
    """Response for listing stakeholders with deal context."""

    # Company context is included so callers can safely reject stale responses
    # when a deal has moved across domains and still render a stale stakeholder list.

    company_domain: str | None = None
    company_name: str | None = None
    stakeholders: list[StakeholderDetail] = Field(default_factory=list)


# Backward-compatible names while API handlers migrate request terminology.
# Kept during the rollout so legacy service imports remain valid. New callers
# should use the command names and these aliases can be removed after the handler
# migration completes.
CreateStakeholderRequest = CreateStakeholderCommand
UpdateStakeholderRequest = UpdateStakeholderCommand
