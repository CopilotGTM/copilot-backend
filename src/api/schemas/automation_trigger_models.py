"""HTTP contracts for registry-backed automation trigger bindings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.domain.models.organisation_tables import (
    AutomationAudienceMode,
    AutomationPrincipalType,
    AutomationScopeType,
    AutomationTargetType,
    AutomationTriggerFamily,
    AutomationTriggerStatus,
)


class TriggerWatchDecision(BaseModel):
    """Optional watched fields for registry definitions that expose advanced watch mode."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["any", "all"]
    paths: list[str] = Field(min_length=1, max_length=20)

    @field_validator("paths")
    @classmethod
    def normalize_paths(cls, value: list[str]) -> list[str]:
        """Normalize paths before the surrounding schema accepts it."""
        paths = [item.strip() for item in value]
        if any(not item for item in paths) or len(paths) != len(set(paths)):
            raise ValueError("watch paths must be non-empty and unique")
        return paths


class TriggerScopeDecision(BaseModel):
    """Subject access boundary selected from the registry's allowed combinations."""

    model_config = ConfigDict(extra="forbid")

    type: AutomationScopeType
    config: dict[str, Any] = Field(default_factory=dict, max_length=32)


class TriggerAudienceDecision(BaseModel):
    """Discovery and delivery audience, independent from execution authority."""

    model_config = ConfigDict(extra="forbid")

    mode: AutomationAudienceMode
    user_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_user_ids(self) -> Self:
        """Validate user ids against the automation contract before persistence or dispatch."""
        if len(self.user_ids) != len(set(self.user_ids)):
            raise ValueError("audience user_ids must be unique")
        if self.mode == AutomationAudienceMode.EXPLICIT_USERS and not self.user_ids:
            raise ValueError("explicit_users audience requires user_ids")
        if self.mode != AutomationAudienceMode.EXPLICIT_USERS and self.user_ids:
            raise ValueError("audience user_ids are only valid for explicit_users")
        return self


class TriggerTaskDecision(BaseModel):
    """Safe task input appended to the pinned agent prompt."""

    model_config = ConfigDict(extra="forbid")

    goal: str | None = Field(default=None, max_length=4_000)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str | None) -> str | None:
        """Normalize goal before the surrounding schema accepts it."""
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("goal cannot be blank")
        return value


class TriggerDeliveryDecision(BaseModel):
    """Code-owned delivery policy; V1 records runs without external fanout."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none"] = "none"


class AutomationTriggerDefinitionBase(BaseModel):
    """User decisions for one binding; correlated projections remain server-owned."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    trigger_key: str = Field(min_length=3, max_length=160)
    status: AutomationTriggerStatus = AutomationTriggerStatus.ACTIVE
    config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    watch: TriggerWatchDecision | None = None
    filter: dict[str, Any] | None = Field(default=None, max_length=64)
    principal_mode: Literal["personal", "organisation"]
    scope: TriggerScopeDecision
    audience: TriggerAudienceDecision
    task: TriggerTaskDecision = Field(default_factory=TriggerTaskDecision)
    delivery: TriggerDeliveryDecision = Field(default_factory=TriggerDeliveryDecision)

    @field_validator("name", "trigger_key", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> Any:
        """Normalize text before the surrounding schema accepts it."""
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value

    @model_validator(mode="after")
    def reject_archived_status(self) -> Self:
        """Reject archived status when it violates the persisted automation contract."""
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
    def require_aware_updated_at(cls, value: datetime) -> datetime:
        """Require aware updated at so downstream code receives a complete, safe value."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include timezone")
        return value.astimezone(UTC)


class AutomationTriggerRefreshRequest(BaseModel):
    """Concurrency token for explicitly repinning a trigger to its current agent."""

    model_config = ConfigDict(extra="forbid")

    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def require_aware_updated_at(cls, value: datetime) -> datetime:
        """Require aware updated at so downstream code receives a complete, safe value."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include timezone")
        return value.astimezone(UTC)


class AutomationManualInvocationSubject(BaseModel):
    """One tenant-validated CRM subject selected for a manual run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["deal", "contact", "account"]
    id: UUID


class AutomationManualInvocationRequest(BaseModel):
    """Authenticated replay identity plus an optional subject fanout."""

    model_config = ConfigDict(extra="forbid")

    invocation_id: str = Field(min_length=1, max_length=200)
    subjects: list[AutomationManualInvocationSubject] = Field(
        default_factory=list,
        max_length=100,
    )

    @field_validator("invocation_id")
    @classmethod
    def normalize_invocation_id(cls, value: str) -> str:
        """Normalize invocation id before the surrounding schema accepts it."""
        value = value.strip()
        if not value:
            raise ValueError("invocation_id cannot be blank")
        return value

    @model_validator(mode="after")
    def require_unique_subjects(self) -> Self:
        """Require unique subjects so downstream code receives a complete, safe value."""
        identities = [(subject.type, subject.id) for subject in self.subjects]
        if len(identities) != len(set(identities)):
            raise ValueError("manual invocation subjects must be unique")
        return self


class AutomationManualInvocationSubjectResult(BaseModel):
    """Durable event identity created for one manual subject or global run."""

    event_id: UUID
    subject: AutomationManualInvocationSubject | None = None


class AutomationManualInvocationResponse(BaseModel):
    """Parent invocation identity and its independently retryable events."""

    invocation_id: str
    correlation_id: str
    items: list[AutomationManualInvocationSubjectResult] = Field(min_length=1)


class AutomationWebhookCredentials(BaseModel):
    """Plaintext webhook credentials returned only when issued or rotated."""

    endpoint_token: str | None = None
    secret: str


class AutomationWebhookRotateRequest(BaseModel):
    """Concurrency token for rotating one owned webhook signing secret."""

    model_config = ConfigDict(extra="forbid")

    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def require_aware_updated_at(cls, value: datetime) -> datetime:
        """Require aware updated at so downstream code receives a complete, safe value."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include timezone")
        return value.astimezone(UTC)


class AutomationWebhookIngressResponse(BaseModel):
    """Durable identity accepted for asynchronous trigger activation."""

    event_id: UUID


class AutomationTriggerSummary(BaseModel):
    """Lightweight trigger representation returned by the agent trigger list."""

    trigger_id: UUID
    name: str
    target_type: AutomationTargetType
    target_id: UUID
    target_revision: int
    target_update_available: bool
    trigger_key: str
    family: AutomationTriggerFamily
    match_event_keys: list[str]
    principal_mode: Literal["personal", "organisation"]
    status: AutomationTriggerStatus
    display_status: Literal["active", "paused", "archived", "completed", "needs_attention"]
    next_trigger_at: datetime | None
    last_triggered_at: datetime | None
    trigger_count: int
    consecutive_failures: int
    last_error_code: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AutomationTriggerDetail(AutomationTriggerSummary):
    """Complete binding decisions plus backend-owned pinned execution state."""

    target_snapshot: dict[str, Any]
    config_schema_version: int
    config: dict[str, Any]
    watch: TriggerWatchDecision | None
    filter: dict[str, Any] | None
    execution_principal_type: AutomationPrincipalType
    execution_principal_id: UUID
    scope: TriggerScopeDecision
    audience: TriggerAudienceDecision
    task: TriggerTaskDecision
    delivery: TriggerDeliveryDecision
    config_hash: str
    webhook_credentials: AutomationWebhookCredentials | None = None


class AutomationTriggerListResponse(BaseModel):
    """Offset-paginated trigger definitions for one owned agent."""

    items: list[AutomationTriggerSummary] = Field(default_factory=list)
    total_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class TriggerCatalogWatch(BaseModel):
    """Non-removable watched fields compiled by a trigger preset."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["any", "all"]
    paths: list[str] = Field(min_length=1)


class TriggerCatalogField(BaseModel):
    """One opaque event path supported by the typed matcher."""

    model_config = ConfigDict(extra="forbid")

    path: str
    label: str
    description: str = ""
    value_type: str
    operators: list[str] = Field(min_length=1)
    option_source: str | None = None
    required: bool = False
    exact: bool = False


class TriggerCatalogConfigField(BaseModel):
    """One trigger option rendered and validated from registry metadata."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value_type: str
    input_type: str
    required: bool = False
    default: Any = None
    options: list[str] = Field(default_factory=list)
    option_source: str | None = None
    depends_on: str | None = None


class TriggerCatalogDefinition(BaseModel):
    """One user-selectable trigger intent from the backend registry."""

    model_config = ConfigDict(extra="forbid")

    trigger_key: str
    family: Literal["event", "manual", "schedule", "webhook"]
    event_label: str
    description: str
    match_event_keys: list[str] = Field(min_length=1)
    subject_type: str | None
    subject_required: bool
    allow_user_watch: bool
    watch_paths: list[str] = Field(default_factory=list)
    fixed_watch: TriggerCatalogWatch | None = None
    fixed_filter: dict[str, Any] | None = None
    default_config: dict[str, Any] = Field(default_factory=dict)
    config_fields: list[TriggerCatalogConfigField] = Field(default_factory=list)
    fields: list[TriggerCatalogField] = Field(default_factory=list)
    principal_modes: list[str] = Field(default_factory=list)
    scope_types: list[str] = Field(default_factory=list)
    audience_modes: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None


class TriggerCatalogResource(BaseModel):
    """Resource group inside one trigger category."""

    model_config = ConfigDict(extra="forbid")

    resource_key: str
    label: str
    triggers: list[TriggerCatalogDefinition] = Field(default_factory=list)


class TriggerCatalogCategory(BaseModel):
    """Top-level trigger picker group containing generic resources."""

    model_config = ConfigDict(extra="forbid")

    category_key: str
    label: str
    resources: list[TriggerCatalogResource] = Field(default_factory=list)


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


class TriggerCatalogValueOption(BaseModel):
    """Stable enum value and mutable display label."""

    value: str
    label: str


class AutomationTriggerOptionSets(BaseModel):
    """Organization-owned option values referenced by catalog fields."""

    pipelines: list[TriggerCatalogPipelineOption] = Field(default_factory=list)
    enums: dict[str, list[TriggerCatalogValueOption]] = Field(default_factory=dict)


class AutomationTriggerCatalogLimits(BaseModel):
    """Backend-enforced limits the trigger builder must reflect before submission."""

    webhook_max_body_bytes: int = Field(gt=0)
    max_direct_subject_ids: int = Field(gt=0)
    max_filter_group_levels: int = Field(gt=0)
    max_filter_conditions: int = Field(gt=0)


class AutomationTriggerCatalog(BaseModel):
    """Complete backend-owned trigger-builder contract for the authenticated user."""

    schema_version: Literal[1] = 1
    categories: list[TriggerCatalogCategory] = Field(default_factory=list)
    option_sets: AutomationTriggerOptionSets
    limits: AutomationTriggerCatalogLimits
