from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from src.domain.models.master_tables import OrganisationStatus
from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJobStatus,
    OrganisationJobType,
)

T = TypeVar("T")

OrganisationCrmProvider = Literal["copilotgtm", "hubspot", "salesforce"]
OrganisationChannelSource = Literal["slack", "whatsapp", "teams", "google_chat"]
OrganisationOnboardingStep = Literal["google", "microsoft", "whatsapp"]
OrganisationCrmMode = Literal["internal", "external"]
OrganisationCrmWriteScope = Literal[
    "accounts:create",
    "accounts:update",
    "contacts:create",
    "contacts:update",
    "contacts:delete",
    "custom_fields:create",
    "custom_fields:update",
    "custom_fields:delete",
    "deal_participants:create",
    "deal_participants:update",
    "deal_participants:delete",
    "deals:create",
    "deals:update",
    "deals:delete",
    "leads:create",
    "leads:update",
    "leads:convert",
    "lead_forms:create",
    "lead_forms:update",
    "lead_forms:ingest",
    "lead_ingestion_keys:create",
    "lead_ingestion_keys:update",
    "lead_ingestion_keys:delete",
    "pipelines:create",
    "pipelines:update",
    "pipelines:delete",
    "tasks:create",
    "tasks:update",
    "tasks:delete",
]


class OrganisationCrmWritePolicy(BaseModel):
    allowed_scopes: list[OrganisationCrmWriteScope] = Field(default_factory=list)


class OrganisationCrmSettings(BaseModel):
    mode: OrganisationCrmMode = "internal"
    provider: OrganisationCrmProvider = "copilotgtm"
    write_policy: OrganisationCrmWritePolicy = Field(default_factory=OrganisationCrmWritePolicy)

    @model_validator(mode="after")
    def derive_mode(self) -> "OrganisationCrmSettings":
        self.mode = "internal" if self.provider == "copilotgtm" else "external"
        return self


class OrganisationChannelSettings(BaseModel):
    enabled: list[OrganisationChannelSource] = Field(default_factory=list)


class UpdateOrganisationGoogleIntegrationSettings(BaseModel):
    gmail_read_enabled: bool | None = None


class UpdateOrganisationIntegrationSettings(BaseModel):
    google: UpdateOrganisationGoogleIntegrationSettings | None = None


class OrganisationOnboardingStepSettings(BaseModel):
    id: OrganisationOnboardingStep
    skippable: bool = False


class OrganisationOnboardingSettings(BaseModel):
    enabled: bool
    steps: list[OrganisationOnboardingStepSettings] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_flow(self) -> "OrganisationOnboardingSettings":
        if self.enabled and tuple(step.id for step in self.steps) not in {
            ("google", "whatsapp"),
            ("microsoft", "whatsapp"),
        }:
            raise ValueError("Onboarding must require a workspace provider before WhatsApp")
        if not self.enabled:
            self.steps = []
        return self


class UpdateOrganisationCrmSettingsRequest(BaseModel):
    provider: OrganisationCrmProvider


class UpdateOrganisationAppSettingsRequest(BaseModel):
    crm: UpdateOrganisationCrmSettingsRequest | None = None
    channels: OrganisationChannelSettings | None = None
    integrations: UpdateOrganisationIntegrationSettings | None = None
    onboarding: OrganisationOnboardingSettings | None = None


class ActiveJobInfo(BaseModel):
    """Summary of an active job for embedding in organisation responses."""

    job_id: UUID
    job_type: OrganisationJobType
    status: OrganisationJobStatus
    progress: int = 0
    status_message: str | None = None
    stream_url: str
    created_at: datetime

    class Config:
        from_attributes = True


class CreateOrganizationRequest(BaseModel):
    organisation_name: str
    # Name shown/used by the AI agent for this organisation (e.g., in Celcius).
    agent_name: str | None = None
    # Email domains that belong to this organisation (used for org-specific email identification).
    domain: list[str] | None = None
    country: str | None = None
    sales_team_size: int | None = None
    company_address: str | None = None
    linked_urls: list[str] | None = None
    website_urls: list[str] | None = None
    summary: str | None = None
    logo_url: str | None = None

    @field_validator("domain")
    @classmethod
    def normalize_domains(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_domains(value)

    class Config:
        extra = "ignore"


class ListOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    agent_name: str | None = None
    domain: list[str] | None = None
    is_active: bool
    country: str | None = None
    sales_team_size: int | None = None
    company_address: str | None = None
    linked_urls: list[str] | None = None
    website_urls: list[str] | None = None
    summary: str | None = None
    logo_url: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    preferences: dict[str, Any] = Field(default_factory=dict)
    # Active job info (if any provisioning/update job is in progress)
    active_job: ActiveJobInfo | None = None

    class Config:
        from_attributes = True


class AdminUserResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    organisation_role: str


class CreateOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    created_at: datetime
    updated_at: datetime


class PaginationOrganizations(BaseModel, Generic[T]):
    organizations: list[ListOrganizationResponse]
    cursor: str | None = None


class UpdateOrganizationRequest(BaseModel):
    organisation_name: str | None = None
    agent_name: str | None = None
    domain: list[str] | None = None
    country: str | None = None
    sales_team_size: int | None = None
    company_address: str | None = None
    linked_urls: list[str] | None = None
    website_urls: list[str] | None = None
    summary: str | None = None
    logo_url: str | None = None
    is_active: bool | None = None

    @field_validator("domain")
    @classmethod
    def normalize_domains(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_domains(value)

    class Config:
        extra = "ignore"


def _normalize_domains(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    normalized = [domain.strip().lower().rstrip(".") for domain in value]
    if any(not domain or "@" in domain or "." not in domain for domain in normalized):
        raise ValueError("domain entries must be valid domain names")
    return list(dict.fromkeys(normalized))


class RunMasterMigrationsResponse(BaseModel):
    target_version: str
    admin_user_id: UUID


class UpdateOrganisationStatusRequest(BaseModel):
    status: OrganisationStatus
    note: str | None = None


class UpdateOrganisationStatusResponse(BaseModel):
    organisation_id: UUID
    status: OrganisationStatus
    updated_at: datetime
    approved_at: datetime | None = None
    approved_by: UUID | None = None


class OrganisationActionHistoryResponse(BaseModel):
    id: UUID
    organisation_id: UUID
    organisation_name: str | None = None
    organisation_status: OrganisationStatus | None = None
    action: str
    note: str | None = None
    details: dict | None = None
    created_at: datetime
    actor_admin_id: UUID | None = None
    actor_admin_name: str | None = None
    actor_admin_email: str | None = None

    class Config:
        from_attributes = True


class PaginatedOrganisationActionHistoryResponse(BaseModel):
    history: list[OrganisationActionHistoryResponse]
    cursor: str | None = None


class CountryOption(BaseModel):
    code: str
    name: str


class CountriesResponse(BaseModel):
    countries: list[CountryOption]
