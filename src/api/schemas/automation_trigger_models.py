"""HTTP contracts for user-owned automation trigger definitions.

Clients configure when an owned agent should run, but the backend owns target identity, pinned
snapshots, tenant scope, duplicate hashes, scheduling projections, and operational counters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.domain.models.organisation_tables import (
    AutomationScopeType,
    AutomationTargetType,
    AutomationTriggerStatus,
    AutomationTriggerType,
)


class AutomationTriggerDefinitionBase(BaseModel):
    """Complete set of fields a user may configure on one trigger."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    status: AutomationTriggerStatus = AutomationTriggerStatus.ACTIVE
    trigger_type: AutomationTriggerType
    source_domain: str | None = Field(default=None, max_length=64)
    event_type: str | None = Field(default=None, max_length=160)
    trigger_config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    filter_config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    scope_config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    task_config: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @field_validator("source_domain", "event_type", mode="before")
    @classmethod
    def _normalize_optional_key(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("task_config", mode="before")
    @classmethod
    def _normalize_task_goal(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "goal" not in value:
            return value
        normalized = dict(value)
        goal = normalized["goal"]
        if isinstance(goal, str):
            normalized["goal"] = goal.strip()
        return normalized

    @model_validator(mode="after")
    def _reject_archived_status(self) -> Self:
        if self.status == AutomationTriggerStatus.ARCHIVED:
            raise ValueError("archived status is only available through DELETE")
        return self


class AutomationTriggerCreateRequest(AutomationTriggerDefinitionBase):
    """Create a trigger for the agent identified by the nested route."""


class AutomationTriggerUpdateRequest(AutomationTriggerDefinitionBase):
    """Fully replace a trigger when its last-read timestamp still matches."""

    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def _require_aware_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include timezone")
        return value.astimezone(UTC)


class AutomationTriggerRefreshRequest(BaseModel):
    """Concurrency token for explicitly repinning a trigger to its current agent."""

    model_config = ConfigDict(extra="forbid")

    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def _require_aware_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include timezone")
        return value.astimezone(UTC)


class AutomationTriggerSummary(BaseModel):
    """Lightweight trigger representation returned by the agent trigger list."""

    trigger_id: UUID
    name: str
    target_type: AutomationTargetType
    target_id: UUID
    target_revision: int
    target_update_available: bool
    trigger_type: AutomationTriggerType
    source_domain: str | None
    event_type: str | None
    status: AutomationTriggerStatus
    next_trigger_at: datetime | None
    last_triggered_at: datetime | None
    trigger_count: int
    consecutive_failures: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AutomationTriggerDetail(AutomationTriggerSummary):
    """Complete trigger definition and backend-owned pinned target state."""

    target_snapshot: dict[str, Any]
    config_schema_version: int
    trigger_config: dict[str, Any]
    filter_config: dict[str, Any]
    scope_type: AutomationScopeType
    scope_config: dict[str, Any]
    task_config: dict[str, Any]
    delivery_config: dict[str, Any]
    config_hash: str


class AutomationTriggerListResponse(BaseModel):
    """Offset-paginated trigger definitions for one owned agent."""

    items: list[AutomationTriggerSummary] = Field(default_factory=list)
    total_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class TriggerCatalogField(BaseModel):
    """Frontend rendering metadata for one backend-validated trigger field."""

    field_key: str
    display_name: str
    description: str
    input_type: str
    value_type: str
    option_source: str | None = None
    options: list[str] = Field(default_factory=list)
    depends_on: str | None = None
    allows_any: bool = True


class TriggerCatalogDefinition(BaseModel):
    """One trigger identity and its backend-owned default JSON contracts."""

    trigger_key: str
    display_name: str
    description: str
    trigger_type: AutomationTriggerType
    source_domain: str | None
    event_type: str | None
    default_trigger_config: dict[str, Any] = Field(default_factory=dict)
    default_filter_config: dict[str, Any] = Field(default_factory=dict)
    default_scope_config: dict[str, Any] = Field(default_factory=dict)
    default_task_config: dict[str, Any] = Field(default_factory=dict)
    fields: list[TriggerCatalogField] = Field(default_factory=list)


class TriggerCatalogCategory(BaseModel):
    """User-facing group of related trigger definitions."""

    category_key: str
    display_name: str
    description: str
    triggers: list[TriggerCatalogDefinition] = Field(default_factory=list)


class TriggerCatalogStageOption(BaseModel):
    """CRM stage option safe to expose in the trigger builder."""

    stage_id: UUID
    label: str
    stage_state: str
    is_active: bool


class TriggerCatalogPipelineOption(BaseModel):
    """CRM pipeline with stages grouped for trigger-builder rendering."""

    pipeline_id: UUID
    label: str
    is_active: bool
    stages: list[TriggerCatalogStageOption] = Field(default_factory=list)


class AutomationTriggerOptionSets(BaseModel):
    """Organization-owned option values referenced by catalog fields."""

    pipelines: list[TriggerCatalogPipelineOption] = Field(default_factory=list)


class AutomationTriggerCatalog(BaseModel):
    """Complete backend-owned trigger-builder contract for the authenticated user."""

    categories: list[TriggerCatalogCategory] = Field(default_factory=list)
    option_sets: AutomationTriggerOptionSets
