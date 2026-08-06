"""Local-time recurrence and bounded direct dispatch for due schedules."""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AutomationEventDispatch,
    AutomationPrincipalType,
    AutomationTrigger,
    AutomationTriggerStatus,
)
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.triggers.direct import ManualInvocationAccess, ManualSubject

# Bound per-trigger CRM fanout and each scheduler sweep so one tenant cannot
# monopolize the worker transaction with an unbounded saved-subject list.
SCHEDULE_SUBJECT_LIMIT = 100
DEFAULT_DUE_SCHEDULE_LIMIT = 50
_SCHEDULE_EVENT_KEY = "time.schedule.due"
_Recurrence = Literal[
    "none",
    "daily",
    "weekdays",
    "weekly",
    "biweekly",
    "monthly",
    "custom",
]


class ScheduleConfig(BaseModel):
    """Exact persisted configuration for a direct schedule binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = Field(min_length=1, max_length=120)
    starts_at: datetime
    recurrence: _Recurrence
    interval_days: int | None = Field(default=None, ge=1, le=365)
    subject_type: Literal["deal", "contact", "account"] | None = None
    subject_ids: tuple[UUID, ...] = Field(default=(), max_length=SCHEDULE_SUBJECT_LIMIT)

    @field_validator("starts_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Require timezone so downstream code receives a complete, safe value."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("starts_at must include timezone")
        return value

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> ScheduleConfig:
        """Validate conditional fields against the automation contract before persistence or dispatch."""
        if (self.recurrence == "custom") != (self.interval_days is not None):
            raise ValueError(
                "custom recurrence requires interval_days and other recurrences forbid it"
            )
        if (self.subject_type is None) != (not self.subject_ids):
            raise ValueError("subject_type and subject_ids must be provided together")
        if len(self.subject_ids) != len(set(self.subject_ids)):
            raise ValueError("schedule subject_ids must be unique")
        return self


def normalize_schedule_config(
    value: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], datetime]:
    """Validate and canonicalize a schedule and return its first future occurrence."""

    try:
        config = ScheduleConfig.model_validate(value)
        _zone(config.timezone)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc
    normalized: dict[str, Any] = {
        "timezone": config.timezone,
        "starts_at": config.starts_at.isoformat(),
        "recurrence": config.recurrence,
    }
    if config.interval_days is not None:
        normalized["interval_days"] = config.interval_days
    if config.subject_type is not None:
        normalized["subject_type"] = config.subject_type
        normalized["subject_ids"] = [str(item) for item in config.subject_ids]
    return normalized, _first_occurrence(config, now.astimezone(UTC))


def next_occurrence(
    config: Mapping[str, Any] | ScheduleConfig,
    previous_utc: datetime,
) -> datetime | None:
    """Advance from one exact UTC occurrence while preserving local wall-clock intent."""

    parsed = config if isinstance(config, ScheduleConfig) else _parse_config(config)
    if parsed.recurrence == "none":
        return None
    if previous_utc.tzinfo is None or previous_utc.utcoffset() is None:
        raise ValueError("previous_utc must include timezone")

    zone = _zone(parsed.timezone)
    anchor = parsed.starts_at.astimezone(zone)
    previous_local = previous_utc.astimezone(UTC).astimezone(zone)
    next_date = _next_local_date(
        previous_local.date(),
        recurrence=parsed.recurrence,
        interval_days=parsed.interval_days,
        monthly_day=anchor.day,
    )
    return _resolve_local(next_date, anchor.timetz().replace(tzinfo=None), zone)


def _first_occurrence(config: ScheduleConfig, now: datetime) -> datetime:
    candidate = config.starts_at.astimezone(UTC)
    if (
        config.recurrence == "weekdays"
        and candidate.astimezone(_zone(config.timezone)).weekday() >= 5
    ):
        candidate = next_occurrence(config, candidate) or candidate
    if candidate > now:
        return candidate
    if config.recurrence == "none":
        raise ValueError("starts_at must be in the future for a one-time trigger")
    # copilotgtm: Recurrences advance one local period at a time. If schedules are
    # ever imported years in arrears, replace this bounded catch-up with calendar math.
    for _ in range(100_000):
        advanced = next_occurrence(config, candidate)
        if advanced is not None and advanced > now:
            return advanced
        if advanced is None:
            break
        candidate = advanced
    raise ValueError("schedule starts_at is too far in the past")


def _next_local_date(
    previous: date,
    *,
    recurrence: _Recurrence,
    interval_days: int | None,
    monthly_day: int,
) -> date:
    if recurrence == "daily":
        return previous + timedelta(days=1)
    if recurrence == "weekdays":
        candidate = previous + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    if recurrence == "weekly":
        return previous + timedelta(weeks=1)
    if recurrence == "biweekly":
        return previous + timedelta(weeks=2)
    if recurrence == "custom":
        return previous + timedelta(days=interval_days or 1)
    month = previous.month + 1
    year = previous.year + (month > 12)
    month = 1 if month > 12 else month
    return date(year, month, min(monthly_day, calendar.monthrange(year, month)[1]))


def _resolve_local(local_date: date, local_time: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(local_date, local_time)
    candidate = naive.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone)
    if round_trip.replace(tzinfo=None) != naive:
        # A nonexistent spring-forward wall time runs at the first corresponding
        # real instant after the gap; later occurrences return to the saved time.
        candidate = round_trip
    return candidate.astimezone(UTC)


def _parse_config(value: Mapping[str, Any]) -> ScheduleConfig:
    try:
        config = ScheduleConfig.model_validate(value)
        _zone(config.timezone)
        return config
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


class DueScheduleAccess(Protocol):
    """Locked due-row selection used by one tenant scheduler."""

    async def claim_due_schedules(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[AutomationTrigger]:
        """Claim due schedules atomically so competing workers cannot process the same row."""
        ...


class ScheduleSubjectAccess(Protocol):
    """Current scope-checked CRM snapshots for saved subject IDs."""

    async def load_subject_records(
        self,
        subjects: tuple[ManualSubject, ...],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Load subject records for the current organisation-scoped operation."""
        ...


class ScheduleEventEnqueuer(Protocol):
    """Transaction-local event ingestion used by the due dispatcher."""

    async def enqueue(self, envelope: AutomationEventEnvelope) -> AutomationEventDispatch:
        """Queue the current Schedule Event Enqueuer work while preserving its tenant and idempotency boundaries."""
        ...


@dataclass(frozen=True)
class ScheduleDispatchResult:
    """Bounded per-tenant schedule sweep summary."""

    claimed_count: int
    dispatched_count: int
    paused_count: int

    def as_dict(self) -> dict[str, int | str]:
        """Serialize the schedule value into the dictionary shape consumed by callers."""
        return {
            "status": "completed",
            "claimed_count": self.claimed_count,
            "dispatched_count": self.dispatched_count,
            "paused_count": self.paused_count,
        }


class ScheduleDispatchService:
    """Claim due bindings, enqueue one occurrence, and advance them atomically."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: DueScheduleAccess | None = None,
        subject_access: ScheduleSubjectAccess | None = None,
        event_enqueuer: ScheduleEventEnqueuer | None = None,
    ) -> None:
        if access is None:
            from src.service.automation.triggers.access import AutomationScheduleAccess

            access = AutomationScheduleAccess(session, organisation_id)
        self.session = session
        self.organisation_id = organisation_id
        self.access = access
        self.subject_access = subject_access or ManualInvocationAccess(session, organisation_id)
        self.event_enqueuer = event_enqueuer or AutomationEventDispatchService(
            session,
            organisation_id,
        )

    async def dispatch_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = DEFAULT_DUE_SCHEDULE_LIMIT,
    ) -> ScheduleDispatchResult:
        """Process at most one exact occurrence from each locked due binding."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        rows = await self.access.claim_due_schedules(now=current, limit=limit)
        dispatched = 0
        paused = 0
        for trigger in rows:
            occurrence = trigger.next_trigger_at
            if occurrence is None:
                continue
            occurrence = occurrence.astimezone(UTC)
            try:
                config = _parse_config(trigger.trigger_config)
                subjects = self._subjects(config)
                records = await self._records(trigger, subjects)
            except (ValueError, AutomationServiceError) as exc:
                self._pause(trigger, "schedule_config_invalid", str(exc))
                paused += 1
                continue
            if len(records) != len(subjects):
                self._pause(
                    trigger,
                    "schedule_subject_unavailable",
                    "One or more scheduled subjects are unavailable",
                )
                paused += 1
                continue

            correlation_id = f"schedule:{trigger.trigger_id}:{occurrence.isoformat()}"
            selected: tuple[ManualSubject | None, ...] = subjects or (None,)
            for subject in selected:
                envelope = self._envelope(
                    trigger,
                    config=config,
                    occurrence=occurrence,
                    correlation_id=correlation_id,
                    subject=subject,
                    record=records[subject.identity] if subject else {},
                )
                await self.event_enqueuer.enqueue(envelope)
                dispatched += 1
            trigger.next_trigger_at = next_occurrence(config, occurrence)
            trigger.last_error_code = None
            trigger.last_error = None
            # copilotgtm: One-time rows stay active because direct activation is
            # asynchronous. Null next_trigger_at prevents re-claim; add a consumed
            # status/activation acknowledgement before pausing them later.
        await self.session.flush()
        return ScheduleDispatchResult(len(rows), dispatched, paused)

    async def _records(
        self,
        trigger: AutomationTrigger,
        subjects: tuple[ManualSubject, ...],
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        if not subjects:
            return {}
        organisation_scope = (
            self._value(trigger.execution_principal_type)
            == AutomationPrincipalType.ORGANISATION_SERVICE.value
        )
        return await self.subject_access.load_subject_records(
            subjects,
            principal_id=trigger.execution_principal_id,
            organisation_scope=organisation_scope,
        )

    def _envelope(
        self,
        trigger: AutomationTrigger,
        *,
        config: ScheduleConfig,
        occurrence: datetime,
        correlation_id: str,
        subject: ManualSubject | None,
        record: dict[str, Any],
    ) -> AutomationEventEnvelope:
        subject_key = (
            "global" if subject is None else f"{subject.subject_type}:{subject.subject_id}"
        )
        idempotency_key = (
            f"schedule:{trigger.trigger_id}:{occurrence.isoformat()}:"
            f"{subject_key}:{trigger.config_hash}"
        )
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": uuid5(
                    NAMESPACE_URL,
                    f"automation:{self.organisation_id}:{idempotency_key}",
                ),
                "organisation_id": self.organisation_id,
                "event_key": _SCHEDULE_EVENT_KEY,
                "route": {
                    "mode": "direct",
                    "trigger_id": trigger.trigger_id,
                    "config_hash": trigger.config_hash,
                },
                "occurred_at": occurrence.astimezone(UTC),
                "source": {
                    "domain": "time",
                    "system": "automation_scheduler",
                    "operation": "schedule",
                    "mode": "system",
                    "source_event_id": occurrence.isoformat(),
                },
                "actor": {"type": "system"},
                "subject": {
                    "type": subject.subject_type if subject else None,
                    "id": str(subject.subject_id) if subject else None,
                },
                "record": record,
                "attributes": {
                    "scheduled_for": occurrence.isoformat(),
                    "timezone": config.timezone,
                },
                "correlation_id": correlation_id,
                "idempotency_key": idempotency_key,
            }
        )

    def _subjects(self, config: ScheduleConfig) -> tuple[ManualSubject, ...]:
        if config.subject_type is None:
            return ()
        return tuple(ManualSubject(config.subject_type, item) for item in config.subject_ids)

    def _pause(self, trigger: AutomationTrigger, code: str, message: str) -> None:
        trigger.status = AutomationTriggerStatus.PAUSED
        trigger.next_trigger_at = None
        trigger.last_error_code = code
        trigger.last_error = message[:500]

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)


__all__ = [
    "DEFAULT_DUE_SCHEDULE_LIMIT",
    "SCHEDULE_SUBJECT_LIMIT",
    "ScheduleConfig",
    "ScheduleDispatchResult",
    "ScheduleDispatchService",
    "next_occurrence",
    "normalize_schedule_config",
]
