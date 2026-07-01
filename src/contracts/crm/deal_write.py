"""Shared CRM deal write contracts for API, agent, and worker callers.

Request DTOs live outside ``src.api`` because deal workers import agent tools in
the light-worker image, where the API package is intentionally not copied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictDealWriteModel(BaseModel):
    """Base class for write payloads where unknown keys should fail fast."""

    model_config = ConfigDict(extra="forbid")


def _reject_null_custom_properties(data: Any) -> Any:
    """Reject explicit null custom field payloads while still allowing omission."""
    if not isinstance(data, dict):
        return data
    for field_name in ("custom_properties", "custom_columns"):
        if data.get(field_name) is None and field_name in data:
            raise ValueError(
                f"{field_name} must be an object; omit it when not updating custom fields"
            )
    if (
        "custom_properties" in data
        and "custom_columns" in data
        and data["custom_properties"] != data["custom_columns"]
    ):
        raise ValueError("custom_columns and custom_properties must match when both are provided")
    return data


def _is_valid_email(value: str) -> bool:
    """Validate simple business-email syntax without pulling in deliverability checks."""
    local, separator, domain = value.partition("@")
    if not local or not separator or not domain or "@" in domain:
        return False
    if any(ch.isspace() for ch in value):
        return False
    return all(part for part in domain.split(".")) and "." in domain


class DealAccountInput(_StrictDealWriteModel):
    """Optional account details supplied during deal creation."""

    company_id: UUID | None = None
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    linkedin_url: str | None = None
    industry: str | None = None
    country: str | None = None

    @field_validator("name", "domain", "website", "linkedin_url", "industry", "country")
    @classmethod
    def normalize_strings(cls, v: str | None) -> str | None:
        """Trim optional account fields so empty sections collapse cleanly."""
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealAccountUpdateRequest(DealAccountInput):
    """Request payload to update account details for an internal CRM deal."""

    @model_validator(mode="before")
    @classmethod
    def reject_identity_field_clears(cls, data: Any) -> Any:
        """Prevent clearing account identity fields through the account update action."""
        if isinstance(data, dict):
            for field_name in ("name", "domain"):
                if field_name in data and data[field_name] is None:
                    raise ValueError(f"{field_name} cannot be cleared")
        return data

    @field_validator(
        "name", "domain", "website", "linkedin_url", "industry", "country", mode="before"
    )
    @classmethod
    def reject_blank_strings(cls, v: str | None) -> str | None:
        """Treat blank strings as invalid updates instead of silent clears."""
        if isinstance(v, str) and not v.strip():
            raise ValueError("Account update fields cannot be blank")
        return v

    @model_validator(mode="after")
    def require_account_field(self) -> DealAccountUpdateRequest:
        """Require at least one account field, including explicit nullable profile clears."""
        if not any(
            [
                self.company_id,
                self.name,
                self.domain,
                self.website,
                self.linkedin_url,
                self.industry,
                self.country,
                "website" in self.model_fields_set,
                "linkedin_url" in self.model_fields_set,
                "industry" in self.model_fields_set,
                "country" in self.model_fields_set,
            ]
        ):
            raise ValueError("At least one account field is required")
        return self


class DealTeamMemberInput(_StrictDealWriteModel):
    """Internal team member added during deal creation."""

    user_id: UUID
    role: str | None = None

    @field_validator("role")
    @classmethod
    def normalize_role(cls, v: str | None) -> str | None:
        """Trim optional team-member roles before creating participant rows."""
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealContactParticipantInput(_StrictDealWriteModel):
    """External CRM contact added as a deal participant during deal creation."""

    contact_id: UUID


class DealCreateRequest(_StrictDealWriteModel):
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
        description="Category of the deal - e.g. 'New Business', 'Renewal', 'Expansion', 'Upsell'.",
    )
    priority: str | None = Field(
        default=None,
        description="Urgency level - 'High', 'Medium', or 'Low'.",
    )
    lead_source: str | None = Field(
        default=None,
        description="How the deal originated - e.g. 'Inbound', 'Outbound', 'Referral', 'Event'.",
    )
    probability: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Win probability percentage for forecasting. Must be between 0 and 100.",
    )
    forecast_category: str | None = Field(
        default=None,
        description="Forecast bucket - e.g. 'Pipeline', 'Best Case', 'Commit', 'Closed'.",
    )
    custom_properties: dict[str, Any] | None = Field(
        default=None,
        min_length=1,
        description="Internal deal custom field values keyed by custom field key.",
    )
    custom_columns: dict[str, Any] | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        description="Public alias for internal deal custom field values keyed by custom field key.",
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
    contact_participants: list[DealContactParticipantInput] | None = Field(
        default=None,
        description="External CRM contacts to attach as deal participants/stakeholders.",
    )
    primary_contact_name: str | None = Field(
        default=None,
        description="Primary contact display name snapshot to attach to the deal.",
    )
    primary_contact_email: str | None = Field(
        default=None,
        description="Primary contact email snapshot to attach to the deal.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_null_custom_properties(cls, data: Any) -> Any:
        """Share custom field null/alias validation with deal update payloads."""
        return _reject_null_custom_properties(data)

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        """Normalize the required deal name and reject whitespace-only values."""
        value = v.strip()
        if not value:
            raise ValueError("Deal name cannot be empty")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        """Store currency codes in uppercase ISO-style form."""
        if v is None:
            return None
        return v.strip().upper() or None

    @field_validator(
        "description",
        "deal_type",
        "priority",
        "lead_source",
        "forecast_category",
        "primary_contact_name",
        "primary_contact_email",
    )
    @classmethod
    def normalize_optional_text(cls, v: str | None) -> str | None:
        """Trim optional text fields and drop blanks during create."""
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("primary_contact_email")
    @classmethod
    def validate_primary_contact_email(cls, v: str | None) -> str | None:
        """Reject malformed primary-contact snapshots before deal creation."""
        if v is not None and not _is_valid_email(v):
            raise ValueError("primary_contact_email must be a valid email")
        return v

    @model_validator(mode="after")
    def normalize_optional_sections(self) -> DealCreateRequest:
        """Collapse empty nested sections and map public custom_columns into storage keys."""
        if self.custom_columns is not None:
            self.custom_properties = self.custom_columns
        if self.account and not any(
            [
                self.account.name,
                self.account.company_id,
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
        if self.contact_participants is not None and len(self.contact_participants) == 0:
            self.contact_participants = None
        return self


class DealUpdateRequest(_StrictDealWriteModel):
    """Request payload to update an internal CRM deal."""

    name: str | None = Field(default=None, max_length=500)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    source_created_at: datetime | None = None
    expected_close_date: datetime | None = None
    description: str | None = None
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None
    probability: float | None = Field(default=None, ge=0, le=100)
    forecast_category: str | None = None
    custom_properties: dict[str, Any] | None = Field(default=None, min_length=1)
    custom_columns: dict[str, Any] | None = Field(default=None, min_length=1, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def reject_null_custom_properties(cls, data: Any) -> Any:
        """Keep custom field clears field-by-field instead of accepting null maps."""
        return _reject_null_custom_properties(data)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        """Reject empty deal names when the update explicitly includes name."""
        if v is None:
            raise ValueError("Deal name cannot be empty")
        value = v.strip() if isinstance(v, str) else str(v).strip()
        if not value:
            raise ValueError("Deal name cannot be empty")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        """Normalize update currency codes to uppercase."""
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
        """Trim optional update text while preserving explicit null clears."""
        if v is None:
            return None
        value = v.strip()
        return value or None

    @model_validator(mode="after")
    def normalize_custom_columns_alias(self) -> DealUpdateRequest:
        """Let clients use custom_columns while the service layer consumes custom_properties."""
        if self.custom_columns is not None:
            self.custom_properties = self.custom_columns
        return self


class DealPrimaryContactUpdateRequest(_StrictDealWriteModel):
    """Request payload to update primary contact details for an internal CRM deal."""

    primary_contact_name: str | None = None
    primary_contact_email: str | None = None

    @field_validator("primary_contact_name", "primary_contact_email", mode="before")
    @classmethod
    def reject_blank_strings(cls, v: str | None) -> str | None:
        """Reject ambiguous blank primary-contact values before normalization."""
        if isinstance(v, str) and not v.strip():
            raise ValueError("Primary contact fields cannot be blank")
        return v

    @field_validator("primary_contact_name", "primary_contact_email")
    @classmethod
    def normalize_strings(cls, v: str | None) -> str | None:
        """Trim primary-contact snapshot values while preserving explicit clear requests."""
        if v is None:
            return None
        return v.strip()

    @field_validator("primary_contact_email")
    @classmethod
    def validate_primary_contact_email(cls, v: str | None) -> str | None:
        """Validate email snapshots before they become deal participant identity."""
        if v is not None and not _is_valid_email(v):
            raise ValueError("primary_contact_email must be a valid email")
        return v

    @property
    def is_clear_request(self) -> bool:
        """Return True only for the explicit two-field primary-contact clear payload."""
        return (
            {"primary_contact_name", "primary_contact_email"} <= self.model_fields_set
            and self.primary_contact_name is None
            and self.primary_contact_email is None
        )

    @model_validator(mode="after")
    def require_contact_field(self) -> DealPrimaryContactUpdateRequest:
        """Allow explicit clears but reject no-op primary-contact updates."""
        if self.is_clear_request:
            return self
        if not self.primary_contact_name and not self.primary_contact_email:
            raise ValueError("At least one primary contact field is required")
        return self


class DealCloseRequest(_StrictDealWriteModel):
    """Request payload to close an internal CRM deal as won or lost."""

    outcome: Literal["won", "lost"]
    closed_reason: str | None = Field(default=None, max_length=255)
    close_date: datetime | None = None
    stage_id: UUID | None = None

    @field_validator("outcome", mode="before")
    @classmethod
    def normalize_outcome(cls, v: str) -> str:
        """Accept mixed-case close outcomes from tools while storing canonical values."""
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("closed_reason")
    @classmethod
    def normalize_closed_reason(cls, v: str | None) -> str | None:
        """Trim lost reasons and let blank values fail the lost-deal policy check."""
        if v is None:
            return None
        value = v.strip()
        return value or None

    @model_validator(mode="after")
    def validate_reason_policy(self) -> DealCloseRequest:
        """Require lost reasons and clear stale reason text when closing as won."""
        if self.outcome == "won":
            self.closed_reason = None
        if self.outcome == "lost" and not self.closed_reason:
            raise ValueError("closed_reason is required when outcome is 'lost'")
        return self


class DealReopenRequest(_StrictDealWriteModel):
    """Request payload to reopen a previously closed internal CRM deal."""

    stage_id: UUID | None = None
    clear_closed_reason: bool = False


class DealStageMoveRequest(_StrictDealWriteModel):
    """Request payload to move a deal to a new stage."""

    stage_id: UUID


class DealOwnerAssignRequest(_StrictDealWriteModel):
    """Request payload to assign or clear a deal owner."""

    owner_user_id: UUID | None
