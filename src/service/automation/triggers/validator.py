"""Strict validation and normalization for supported automation trigger definitions."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from src.api.schemas.automation_trigger_models import AutomationTriggerDefinitionBase
from src.domain.models.organisation_tables import AutomationTriggerType
from src.service.automation.config_validation import contains_credentials, validate_json_document
from src.service.automation.triggers.crm_events import CRM_AGENT_EVENT_SCHEMAS

_CONFIG_MAX_BYTES = 16_384


class TriggerOptionValidator(Protocol):
    """Minimum CRM option lookup required by this validator."""

    async def validate_crm_selection(self, pipeline_ids: set[UUID], stage_ids: set[UUID]) -> None:
        """Reject identifiers outside the current organization and pipeline selection."""

        ...


class ScheduleConfig(BaseModel):
    """Exact persisted JSON shape for a schedule source."""

    model_config = ConfigDict(extra="forbid")

    timezone: str
    starts_at: datetime
    recurrence: Literal["none", "daily", "weekdays", "weekly", "biweekly", "monthly"]

    @field_validator("starts_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("starts_at must include timezone")
        return value


class ScheduleScope(BaseModel):
    """V1 owner-scope narrowing supported for scheduled deal work."""

    model_config = ConfigDict(extra="forbid")

    entity: Literal["deal"] = "deal"
    statuses: list[Literal["open", "won", "lost"]] = ["open"]


class TaskConfig(BaseModel):
    """Safe per-trigger input supplied to the pinned agent."""

    model_config = ConfigDict(extra="forbid")

    goal: str | None = None

    @field_validator("goal")
    @classmethod
    def _goal_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("goal cannot be blank")
        return value


class CRMDealStageFilter(BaseModel):
    """Versioned predicates for the canonical deal-stage event."""

    model_config = ConfigDict(extra="forbid")

    schema_key: Literal["crm.deal.stage_changed.v1"]
    pipeline_ids: list[UUID]
    stage_change_mode: Literal["any_change", "outcome", "specific_stage"]
    from_stage_ids: list[UUID]
    to_stage_ids: list[UUID]
    outcome: Literal["any", "won", "lost"]

    @model_validator(mode="after")
    def _validate_mode(self) -> CRMDealStageFilter:
        if self.stage_change_mode == "any_change":
            if self.from_stage_ids or self.to_stage_ids or self.outcome != "any":
                raise ValueError("any_change cannot include stages or outcome")
        elif self.stage_change_mode == "outcome":
            if self.outcome == "any":
                raise ValueError("outcome mode requires won or lost outcome")
            if self.from_stage_ids or self.to_stage_ids:
                raise ValueError("outcome mode cannot include stages")
        elif len(set(self.pipeline_ids)) != 1:
            raise ValueError("specific_stage requires exactly one pipeline")
        elif not self.from_stage_ids and not self.to_stage_ids:
            raise ValueError("specific_stage requires at least one stage")
        elif self.outcome != "any":
            raise ValueError("specific_stage cannot include outcome")
        return self


class CRMDealCreatedFilter(BaseModel):
    """Optional pipeline narrowing for new-deal events."""

    model_config = ConfigDict(extra="forbid")

    schema_key: Literal["crm.deal.created.v1"]
    pipeline_ids: list[UUID]


class CRMLifecycleFilter(BaseModel):
    """Schema identity for contact and account lifecycle events without V1 predicates."""

    model_config = ConfigDict(extra="forbid")

    schema_key: str


@dataclass(frozen=True)
class ValidatedTriggerDefinition:
    """Normalized execution-affecting JSON and initial schedule projection."""

    trigger_config: dict[str, Any]
    filter_config: dict[str, Any]
    scope_config: dict[str, Any]
    task_config: dict[str, Any]
    next_trigger_at: datetime | None


class AutomationTriggerValidator:
    """Validate a complete trigger against the exact V1 source contracts."""

    def __init__(self, options: TriggerOptionValidator) -> None:
        self.options = options

    async def validate(
        self, definition: AutomationTriggerDefinitionBase, *, now: datetime | None = None
    ) -> ValidatedTriggerDefinition:
        """Return canonical JSON only when the trigger can be evaluated later."""

        now = (now or datetime.now(UTC)).astimezone(UTC)
        for field_name in ("trigger_config", "filter_config", "scope_config", "task_config"):
            value = getattr(definition, field_name)
            validate_json_document(field_name, value, max_bytes=_CONFIG_MAX_BYTES)
            if contains_credentials(value):
                raise ValueError(f"{field_name} must not contain credentials")
        try:
            task = TaskConfig.model_validate(definition.task_config)
            if definition.trigger_type == AutomationTriggerType.SCHEDULE:
                return self._validate_schedule(definition, task, now)
            return await self._validate_event(definition, task)
        except ValidationError as exc:
            raise ValueError(exc.errors()[0]["msg"]) from exc

    def _validate_schedule(
        self, definition: AutomationTriggerDefinitionBase, task: TaskConfig, now: datetime
    ) -> ValidatedTriggerDefinition:
        if definition.source_domain is not None or definition.event_type is not None:
            raise ValueError("schedule triggers cannot include source_domain or event_type")
        if definition.filter_config:
            raise ValueError("schedule filter_config must be empty")
        config = ScheduleConfig.model_validate(definition.trigger_config)
        scope = ScheduleScope.model_validate(definition.scope_config)
        try:
            zone = ZoneInfo(config.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        local = config.starts_at.astimezone(zone)
        next_at = self._next_occurrence(local, config.recurrence, now)
        return ValidatedTriggerDefinition(
            trigger_config={
                "timezone": config.timezone,
                "starts_at": config.starts_at.isoformat(),
                "recurrence": config.recurrence,
            },
            filter_config={},
            scope_config={"entity": scope.entity, "statuses": sorted(set(scope.statuses))},
            task_config=task.model_dump(exclude_none=True),
            next_trigger_at=next_at,
        )

    async def _validate_event(
        self, definition: AutomationTriggerDefinitionBase, task: TaskConfig
    ) -> ValidatedTriggerDefinition:
        if definition.source_domain != "crm":
            raise ValueError("source_domain must be crm")
        event_type = definition.event_type
        if event_type not in CRM_AGENT_EVENT_SCHEMAS:
            raise ValueError("event_type is not a supported CRM agent event")
        if definition.trigger_config or definition.scope_config:
            raise ValueError("CRM event trigger_config and scope_config must be empty")
        if event_type == "crm.deal.stage_changed":
            config = CRMDealStageFilter.model_validate(definition.filter_config)
            pipeline_ids = set(config.pipeline_ids)
            stage_ids = {*config.from_stage_ids, *config.to_stage_ids}
            await self.options.validate_crm_selection(pipeline_ids, stage_ids)
            normalized = config.model_dump(mode="json")
            for key in ("pipeline_ids", "from_stage_ids", "to_stage_ids"):
                normalized[key] = sorted(set(normalized[key]))
        elif event_type == "crm.deal.created":
            created = CRMDealCreatedFilter.model_validate(definition.filter_config)
            await self.options.validate_crm_selection(set(created.pipeline_ids), set())
            normalized = created.model_dump(mode="json")
            normalized["pipeline_ids"] = sorted(set(normalized["pipeline_ids"]))
        else:
            lifecycle = CRMLifecycleFilter.model_validate(definition.filter_config)
            if lifecycle.schema_key != CRM_AGENT_EVENT_SCHEMAS[event_type]:
                raise ValueError("schema_key does not match event_type")
            normalized = lifecycle.model_dump()
        return ValidatedTriggerDefinition(
            trigger_config={},
            filter_config=normalized,
            scope_config={},
            task_config=task.model_dump(exclude_none=True),
            next_trigger_at=None,
        )

    @classmethod
    def _next_occurrence(cls, local: datetime, recurrence: str, now: datetime) -> datetime:
        candidate = local
        if recurrence == "none":
            if candidate.astimezone(UTC) <= now:
                raise ValueError("starts_at must be in the future for a one-time trigger")
            return candidate.astimezone(UTC)
        if recurrence == "weekdays":
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
        monthly_day = local.day
        while candidate.astimezone(UTC) <= now:
            if recurrence == "daily":
                candidate += timedelta(days=1)
            elif recurrence == "weekdays":
                candidate += timedelta(days=1)
                while candidate.weekday() >= 5:
                    candidate += timedelta(days=1)
            elif recurrence == "weekly":
                candidate += timedelta(weeks=1)
            elif recurrence == "biweekly":
                candidate += timedelta(weeks=2)
            else:
                month = candidate.month + 1
                year = candidate.year + (month > 12)
                month = 1 if month > 12 else month
                candidate = candidate.replace(
                    year=year,
                    month=month,
                    day=min(monthly_day, calendar.monthrange(year, month)[1]),
                )
        return candidate.astimezone(UTC)
