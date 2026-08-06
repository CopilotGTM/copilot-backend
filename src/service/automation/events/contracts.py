"""Canonical event-time input shared by every automation producer.

Producers persist this envelope before a worker sees it. Keeping the contract
strict, bounded, and source-neutral makes dispatch replay deterministic and
prevents sensitive domain objects from leaking into run snapshots.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)
from src.service.automation.config_validation import validate_json_document

# Keep the persisted envelope below the 64 KiB JSON/document limit so dispatch
# retries can carry the same bounded payload without truncation.
AUTOMATION_EVENT_MAX_BYTES = 65_536

_RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_EventKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$",
    ),
]
_ConfigHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _require_strict_json(value: Any, path: str = "$") -> Any:
    """Reject Python values that JSON encoding would coerce or cannot represent."""

    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain strict JSON values")
        return value
    if value_type is list:
        for index, item in enumerate(value):
            _require_strict_json(item, f"{path}[{index}]")
        return value
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} must contain strict JSON object keys")
            _require_strict_json(item, f"{path}.{key}")
        return value
    raise ValueError(f"{path} must contain strict JSON values")


StrictJsonValue = Annotated[JsonValue, BeforeValidator(_require_strict_json)]


class AutomationRouteMode(str, Enum):
    """How activation selects the binding for an event."""

    MATCH = "match"
    DIRECT = "direct"


class AutomationSourceOperation(str, Enum):
    """Durable fact operation represented by the envelope."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SIGNAL = "signal"
    INVOKE = "invoke"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"


class AutomationSourceMode(str, Enum):
    """Origin mode used by import and backfill policy."""

    INTERACTIVE = "interactive"
    INCREMENTAL_SYNC = "incremental_sync"
    INITIAL_SYNC = "initial_sync"
    BACKFILL = "backfill"
    SYSTEM = "system"


class AutomationActorType(str, Enum):
    """Identity class that caused the durable source fact."""

    USER = "user"
    INTEGRATION = "integration"
    AGENT = "agent"
    SYSTEM = "system"
    EXTERNAL = "external"


class _FrozenContract(BaseModel):
    """Base settings shared by nested envelope contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AutomationEventRoute(_FrozenContract):
    """Candidate lookup mode and direct-binding freshness projections."""

    mode: AutomationRouteMode
    trigger_id: UUID | None = None
    config_hash: _ConfigHash | None = None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> AutomationEventRoute:
        """Validate mode fields against the automation contract before persistence or dispatch."""
        if self.mode == AutomationRouteMode.MATCH:
            if self.trigger_id is not None or self.config_hash is not None:
                raise ValueError("match route forbids trigger_id and config_hash")
        elif self.trigger_id is None or self.config_hash is None:
            raise ValueError("direct route requires trigger_id and config_hash")
        return self


class AutomationEventSource(_FrozenContract):
    """Stable producer identity and source-mode policy inputs."""

    domain: _RequiredText
    system: _RequiredText
    operation: AutomationSourceOperation
    mode: AutomationSourceMode
    source_event_id: Annotated[_RequiredText, StringConstraints(max_length=512)]


class AutomationEventActor(_FrozenContract):
    """Evidence about the actor that caused the fact, not the run principal."""

    type: AutomationActorType
    id: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None


class AutomationEventSubject(_FrozenContract):
    """Zero or one primary business object isolated for access and retry."""

    type: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    id: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def validate_identity_pair(self) -> AutomationEventSubject:
        """Validate identity pair against the automation contract before persistence or dispatch."""
        if (self.type is None) != (self.id is None):
            raise ValueError("subject type and id must both be present or both be null")
        return self


class AutomationEventChange(_FrozenContract):
    """Exact event-time values on both sides of one changed field."""

    before: StrictJsonValue
    after: StrictJsonValue


class AutomationEventEnvelope(_FrozenContract):
    """Validated replay-stable input persisted in an automation dispatch."""

    schema_version: Literal[1] = 1
    event_id: UUID
    organisation_id: UUID
    event_key: _EventKey
    route: AutomationEventRoute
    occurred_at: datetime
    source: AutomationEventSource
    actor: AutomationEventActor
    subject: AutomationEventSubject
    record: dict[str, StrictJsonValue] = Field(default_factory=dict)
    changes: dict[str, AutomationEventChange] = Field(default_factory=dict)
    attributes: dict[str, StrictJsonValue] = Field(default_factory=dict)
    references: dict[str, StrictJsonValue] = Field(default_factory=dict)
    audience_user_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=256)
    correlation_id: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    caused_by_run_id: UUID | None = None
    causation_depth: int = Field(default=0, ge=0, le=3)
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        """Require utc so downstream code receives a complete, safe value."""
        if value.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be an aware UTC datetime")
        return value.astimezone(UTC)

    @field_validator("audience_user_ids")
    @classmethod
    def require_unique_audience(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """Require unique audience so downstream code receives a complete, safe value."""
        if len(value) != len(set(value)):
            raise ValueError("audience user IDs must be unique")
        return value

    @model_validator(mode="after")
    def enforce_serialized_limit(self) -> AutomationEventEnvelope:
        """Apply the enforce serialized limit contract for this organisation-scoped module."""
        if (self.caused_by_run_id is None) != (self.causation_depth == 0):
            raise ValueError("causation lineage requires both run identity and child depth")
        validate_json_document(
            "automation event envelope",
            self.model_dump(mode="json"),
            max_bytes=AUTOMATION_EVENT_MAX_BYTES,
        )
        return self


__all__ = [
    "AUTOMATION_EVENT_MAX_BYTES",
    "AutomationActorType",
    "AutomationEventActor",
    "AutomationEventChange",
    "AutomationEventEnvelope",
    "AutomationEventRoute",
    "AutomationEventSource",
    "AutomationEventSubject",
    "AutomationRouteMode",
    "AutomationSourceMode",
    "AutomationSourceOperation",
    "StrictJsonValue",
]
