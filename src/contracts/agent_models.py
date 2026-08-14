"""Shared contracts for private user-owned agent definitions.

The API accepts only fields a sales user may configure. Tenant identity, ownership,
provenance, visibility, runtime type, and revision counters are supplied by the backend so a
client cannot move or elevate an agent by crafting a request body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from src.domain.models.organisation_tables import (
    AgentOutputMode,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
)

AgentOutputDeliveryChannel = Literal["email_notification"]


class AgentCapabilitySelection(BaseModel):
    """Complete desired tool and skill allowlists for a user-owned agent."""

    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[str] = Field(default_factory=list, max_length=128)
    allowed_skills: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("allowed_tools", "allowed_skills", mode="before")
    @classmethod
    def _normalize_capability_keys(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, list):
            return value
        cleaned = [item.strip() if isinstance(item, str) else item for item in value]
        if not all(isinstance(item, str) for item in cleaned):
            return cleaned
        if any(not item for item in cleaned):
            raise ValueError(f"{info.field_name} values cannot be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError(f"{info.field_name} values must be unique")
        return cleaned


class AgentOutputDeliveryUpdateRequest(BaseModel):
    """Replace the agent's external delivery channels at a known delivery version."""

    model_config = ConfigDict(extra="forbid")

    channels: list[AgentOutputDeliveryChannel]
    expected_updated_at: datetime

    @field_validator("channels")
    @classmethod
    def _require_unique_channels(
        cls,
        value: list[AgentOutputDeliveryChannel],
    ) -> list[AgentOutputDeliveryChannel]:
        if len(value) != len(set(value)):
            raise ValueError("channels values must be unique")
        return sorted(value)

    @field_validator("expected_updated_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value


class AgentOutputDeliveryState(BaseModel):
    """Current agent-level delivery decision and its concurrency token."""

    channels: list[AgentOutputDeliveryChannel]
    updated_at: datetime


class AgentDefinitionBase(AgentCapabilitySelection):
    """Complete set of user-editable fields for one executable agent definition."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    prompt: str = Field(min_length=1, max_length=50_000)
    model_name: str | None = Field(default=None, max_length=200)
    agent_model_config: dict[str, Any] = Field(default_factory=dict)
    max_turns: int | None = Field(default=None, ge=1, le=100)
    input_schema: dict[str, Any] | None = None
    output_mode: AgentOutputMode = AgentOutputMode.TEXT
    output_schema: dict[str, Any] | None = None

    @field_validator("name", "prompt", mode="before")
    @classmethod
    def _normalize_required_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

    @field_validator("description", "model_name", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @model_validator(mode="after")
    def _require_json_output_schema(self) -> Self:
        if self.output_mode == AgentOutputMode.JSON and self.output_schema is None:
            raise ValueError("output_schema is required when output_mode is json")
        return self


class AgentCreateRequest(AgentDefinitionBase):
    """Create one private user-authored agent for the authenticated principal."""


class AgentUpdateRequest(AgentDefinitionBase):
    """Fully replace mutable fields when the stored revision still matches."""

    expected_revision: int = Field(ge=1)
    status: AgentStatus

    @field_validator("status")
    @classmethod
    def _reject_archived_status(cls, value: AgentStatus) -> AgentStatus:
        if value == AgentStatus.ARCHIVED:
            raise ValueError("archived status is only available through DELETE")
        return value


class AgentCapabilitiesUpdateRequest(AgentCapabilitySelection):
    """Atomically replace capability allowlists at a known agent revision."""

    expected_revision: int = Field(ge=1)


class AgentCapabilityItem(BaseModel):
    """One code-owned capability available to the agent editor."""

    key: str
    label: str
    category: str


class AgentOutputDeliveryChannelItem(BaseModel):
    """One server-owned destination available in the output-delivery editor."""

    key: AgentOutputDeliveryChannel
    label: str
    description: str
    sender_mode: Literal["platform"]
    body_source: Literal["agent_output_markdown"]


class AgentCapabilityCatalog(BaseModel):
    """Selectable tool and skill metadata sourced from the running backend."""

    tools: list[AgentCapabilityItem]
    skills: list[AgentCapabilityItem]
    output_delivery_channels: list[AgentOutputDeliveryChannelItem]


class AgentTemplateSummary(BaseModel):
    """Display metadata for one code-owned base agent."""

    model_config = ConfigDict(frozen=True)

    template_id: UUID
    version: int = Field(ge=1)
    slug: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    agent_type: Literal["general"] = "general"
    model_name: str | None = Field(default=None, max_length=200)
    allowed_tools: list[str] = Field(default_factory=list, max_length=128)
    allowed_skills: list[str] = Field(default_factory=list, max_length=64)
    output_mode: AgentOutputMode = AgentOutputMode.TEXT
    published_at: datetime


class AgentTemplateDetail(AgentTemplateSummary):
    """Complete code-owned definition copied when a user selects a template."""

    prompt: str = Field(min_length=1, max_length=50_000)
    agent_model_config: dict[str, Any] = Field(default_factory=dict)
    max_turns: int | None = Field(default=None, ge=1, le=100)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class AgentTemplateListResponse(BaseModel):
    """Current code-owned templates available to authenticated users."""

    items: list[AgentTemplateSummary] = Field(default_factory=list)
    total_count: int = Field(ge=0)


class UseAgentTemplateRequest(BaseModel):
    """Exact metadata version the user chose from the template catalog."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class AgentSummary(BaseModel):
    """Lightweight agent fields returned by the owner list endpoint."""

    agent_id: UUID
    revision: int
    source_type: AgentSourceType
    status: AgentStatus
    name: str
    description: str | None
    agent_type: str
    model_name: str | None
    allowed_tools: list[str]
    allowed_skills: list[str]
    output_mode: AgentOutputMode
    created_at: datetime
    updated_at: datetime


class AutomationEligibilityMode(BaseModel):
    """Whether the current caller may bind this agent to one execution principal."""

    available: bool
    reason_code: (
        Literal[
            "retry_unsafe_tools",
            "organisation_requires_admin",
            "organisation_tools_not_supported",
            "organisation_skills_not_supported",
            "no_compatible_principal_mode",
        ]
        | None
    ) = None
    reason: str | None = None


class AgentAutomationEligibility(BaseModel):
    """Caller-specific automation principal eligibility for an agent."""

    personal: AutomationEligibilityMode
    organisation: AutomationEligibilityMode


class AgentDetail(AgentSummary):
    """Complete owned definition returned after selection or mutation."""

    owner_user_id: UUID
    source_agent_id: UUID | None
    source_version: int | None
    visibility: AgentVisibility
    prompt: str
    agent_model_config: dict[str, Any]
    max_turns: int | None
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    output_delivery: AgentOutputDeliveryState
    automation_eligibility: AgentAutomationEligibility


class AgentListResponse(BaseModel):
    """Offset-paginated list of the authenticated user's active or paused agents."""

    items: list[AgentSummary] = Field(default_factory=list)
    total_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
