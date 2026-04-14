"""CRM deal write request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class DealAccountInput(BaseModel):
    """Optional account details supplied during deal creation."""

    name: str | None = None
    domain: str | None = None
    website: str | None = None
    linkedin_url: str | None = None
    industry: str | None = None
    country: str | None = None

    @field_validator("name", "domain", "website", "linkedin_url", "industry", "country")
    @classmethod
    def normalize_strings(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealAccountUpdateRequest(DealAccountInput):
    """Request payload to update account details for an internal CRM deal."""


class DealTeamMemberInput(BaseModel):
    """Internal team member added during deal creation."""

    user_id: UUID
    role: str | None = None

    @field_validator("role")
    @classmethod
    def normalize_role(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealCreateRequest(BaseModel):
    """Request payload to create an internal CRM deal."""

    name: str = Field(
        min_length=1,
        max_length=500,
        description="Display name for the deal (e.g. 'Acme Corp - Enterprise License').",
    )
    amount: float | None = Field(
        default=None,
        ge=0,
        description="Monetary value of the deal. Must be zero or positive.",
    )
    currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Three-letter ISO 4217 currency code (e.g. USD, INR, EUR).",
    )
    pipeline_id: UUID | None = Field(
        default=None,
        description="Pipeline to place the deal in. Falls back to the organisation's default pipeline when omitted.",
    )
    stage_id: UUID | None = Field(
        default=None,
        description="Initial stage within the chosen pipeline. Defaults to the first stage if omitted.",
    )
    owner_user_id: UUID | None = Field(
        default=None,
        description="User UUID of the deal owner responsible for driving this deal to close.",
    )
    close_date: datetime | None = Field(
        default=None,
        description="Actual date the deal was closed (won or lost). "
        "Only set when recording a historical close; leave empty for open deals.",
    )
    expected_close_date: datetime | None = Field(
        default=None,
        description="Projected date the deal is expected to close. "
        "Used for forecasting and pipeline planning.",
    )
    description: str | None = Field(
        default=None,
        description="Free-text notes or context about the deal.",
    )
    deal_type: str | None = Field(
        default=None,
        description="Category of the deal — e.g. 'New Business', 'Renewal', 'Expansion', 'Upsell'.",
    )
    priority: str | None = Field(
        default=None,
        description="Urgency level — 'High', 'Medium', or 'Low'.",
    )
    lead_source: str | None = Field(
        default=None,
        description="How the deal originated — e.g. 'Inbound', 'Outbound', 'Referral', 'Event'.",
    )
    source_created_at: datetime | None = Field(
        default=None,
        description="Date the deal engagement originally started. "
        "Shown as 'Start Date' in the UI. Defaults to the current timestamp when omitted.",
    )
    account: DealAccountInput | None = Field(
        default=None,
        description="Company or account details to associate with the deal at creation time.",
    )
    team_members: list[DealTeamMemberInput] | None = Field(
        default=None,
        description="Internal team members to attach as deal participants (e.g. SE, CSM).",
    )

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("Deal name cannot be empty")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().upper() or None

    @model_validator(mode="after")
    def normalize_optional_sections(self) -> DealCreateRequest:
        if self.account and not any(
            [
                self.account.name,
                self.account.domain,
                self.account.website,
                self.account.linkedin_url,
                self.account.industry,
                self.account.country,
            ]
        ):
            self.account = None
        if self.team_members is not None and len(self.team_members) == 0:
            self.team_members = None
        return self


class DealUpdateRequest(BaseModel):
    """Request payload to update an internal CRM deal."""

    name: str | None = Field(default=None, max_length=500)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    source_created_at: datetime | None = None
    close_date: datetime | None = None
    expected_close_date: datetime | None = None
    description: str | None = None
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None
    probability: float | None = Field(default=None, ge=0, le=100)
    forecast_category: str | None = None
    custom_properties: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().upper() or None

    @field_validator(
        "description",
        "deal_type",
        "priority",
        "lead_source",
        "forecast_category",
    )
    @classmethod
    def normalize_optional_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealPrimaryContactUpdateRequest(BaseModel):
    """Request payload to update primary contact details for an internal CRM deal."""

    primary_contact_name: str | None = None
    primary_contact_email: str | None = None

    @field_validator("primary_contact_name", "primary_contact_email")
    @classmethod
    def normalize_strings(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealCloseRequest(BaseModel):
    """Request payload to close an internal CRM deal as won or lost."""

    outcome: Literal["won", "lost"]
    closed_reason: str | None = Field(default=None, max_length=255)
    close_date: datetime | None = None
    stage_id: UUID | None = None

    @field_validator("closed_reason")
    @classmethod
    def normalize_closed_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @model_validator(mode="after")
    def validate_reason_policy(self) -> DealCloseRequest:
        if self.outcome == "lost" and not self.closed_reason:
            raise ValueError("closed_reason is required when outcome is 'lost'")
        return self


class DealReopenRequest(BaseModel):
    """Request payload to reopen a previously closed internal CRM deal."""

    stage_id: UUID | None = None
    clear_closed_reason: bool = False


class DealStageMoveRequest(BaseModel):
    """Request payload to move a deal to a new stage."""

    stage_id: UUID


class DealOwnerAssignRequest(BaseModel):
    """Request payload to assign or clear a deal owner."""

    owner_user_id: UUID | None = None


class DealWriteResponse(BaseModel):
    """Minimal response for deal write operations."""

    deal_id: UUID
    name: str | None
    pipeline_id: UUID | None
    stage_id: UUID | None
    deal_status: str
    owner_user_id: UUID | None
    company_id: UUID | None = None
    company_name: str | None = None
    company_domain: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
