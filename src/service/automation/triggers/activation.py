"""Source-neutral activation of canonical events into durable agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from time import perf_counter
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentRunStatus,
    AutomationTrigger,
)
from src.infra.utils.logger import get_logger
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.contracts import (
    AutomationEventEnvelope,
    AutomationRouteMode,
    AutomationSourceMode,
)
from src.service.automation.triggers.activation_access import TriggerActivationAccess
from src.service.automation.triggers.crm_schema import AutomationCRMRegistryService
from src.service.automation.triggers.matcher import (
    MatcherEvaluationError,
    MatcherValidationError,
    collect_option_selections,
    matches_binding,
    validate_filter_tree,
    validate_watch,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    AutomationRegistry,
    EventDefinition,
    TriggerDefinition,
)

logger = get_logger("automation_trigger_activation")


@dataclass(frozen=True)
class TriggerActivationOutcome:
    """Audit result for one candidate binding."""

    trigger_id: UUID
    status: Literal["created", "replayed", "skipped", "paused"]
    reason: str | None = None
    run_id: UUID | None = None

    def as_dict(
        self,
        *,
        correlation_id: str | None = None,
        latency_ms: float = 0,
    ) -> dict[str, Any]:
        """Serialize the activation value into the dictionary shape consumed by callers."""
        return {
            "trigger_id": str(self.trigger_id),
            "matched": self.status in {"created", "replayed"},
            "status": self.status,
            "reason": self.reason,
            "run_id": str(self.run_id) if self.run_id else None,
            "correlation_id": correlation_id,
            "latency_ms": latency_ms,
        }


@dataclass(frozen=True)
class TriggerActivationResult:
    """Complete deterministic activation result for one event delivery."""

    event_id: UUID
    candidate_count: int
    matched_count: int
    created_run_ids: tuple[UUID, ...] = ()
    replayed_run_ids: tuple[UUID, ...] = ()
    outcomes: tuple[TriggerActivationOutcome, ...] = ()
    correlation_id: str | None = None
    latency_ms: float = 0


class ActivationAccess(Protocol):
    """Coordinate Activation Access for the current organisation-scoped operation."""

    async def list_event_candidates(self, event_key: str) -> list[AutomationTrigger]:
        """List event candidates within the organisation boundary."""
        ...

    async def get_direct_candidate(self, trigger_id: UUID) -> AutomationTrigger | None:
        """Load direct candidate within the organisation boundary."""
        ...

    async def list_active_agent_ids(self, agent_ids: set[UUID]) -> set[UUID]:
        """List active agent ids within the organisation boundary."""
        ...

    async def list_active_user_ids(self, user_ids: set[UUID]) -> set[UUID]:
        """List active user ids within the organisation boundary."""
        ...

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Validate option selection against the automation contract before persistence or dispatch."""
        ...

    async def list_subject_participant_user_ids(
        self,
        subject_type: str,
        subject_id: UUID,
    ) -> set[UUID]:
        """List subject participant user ids within the organisation boundary."""
        ...

    async def pause_invalid_trigger(
        self,
        trigger_id: UUID,
        *,
        error_code: str,
        message: str,
    ) -> bool:
        """Apply the pause invalid trigger contract for this organisation-scoped module."""
        ...

    async def list_causation_trigger_ids(
        self,
        caused_by_run_id: UUID | None,
    ) -> set[UUID]:
        """List causation trigger ids within the organisation boundary."""
        ...

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        """Apply the insert run if absent contract for this organisation-scoped module."""
        ...

    async def get_run(self, *, trigger_id: UUID, event_id: UUID) -> AgentRun | None:
        """Load run within the organisation boundary."""
        ...


class AutomationTriggerActivationService:
    """Match one immutable envelope without committing or publishing work."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: ActivationAccess | None = None,
        registry: AutomationRegistry | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.access = access or TriggerActivationAccess(session, organisation_id)
        self.registry = registry
        self.registry_service = AutomationCRMRegistryService(session, organisation_id)

    async def activate(self, envelope: AutomationEventEnvelope) -> TriggerActivationResult:
        """Revalidate, match, and materialize runs for one canonical event."""

        started_at = perf_counter()
        if envelope.organisation_id != self.organisation_id:
            raise AutomationServiceError("Automation event organisation does not match session")
        registry = self.registry or (await self.registry_service.snapshot()).registry
        try:
            event_definition = registry.event(envelope.event_key)
            # Queued deal facts were validated when persisted. Replay them against
            # the stable base contract while current tenant schema governs bindings.
            envelope_definition = (
                AUTOMATION_REGISTRY.event(envelope.event_key)
                if envelope.event_key.startswith("crm.deal.")
                else event_definition
            )
            envelope_definition.validate_envelope(envelope)
        except ValueError as exc:
            raise AutomationServiceError(f"Invalid automation event envelope: {exc}") from exc

        candidates, missing_direct_id = await self._candidates(envelope)
        if missing_direct_id is not None:
            return TriggerActivationResult(
                event_id=envelope.event_id,
                candidate_count=1,
                matched_count=0,
                outcomes=(
                    TriggerActivationOutcome(
                        trigger_id=missing_direct_id,
                        status="skipped",
                        reason="direct_trigger_missing",
                    ),
                ),
                correlation_id=envelope.correlation_id,
                latency_ms=_elapsed_ms(started_at),
            )

        subject_audience_ids: set[UUID] = set()
        if event_definition.supports_subject_participants and any(
            _value(item.audience_mode) == "subject_participants" for item in candidates
        ):
            subject_audience_ids = set(envelope.audience_user_ids)

        audience_configs: dict[UUID, dict[str, Any]] = {}
        audience_contract_errors: set[UUID] = set()
        audience_user_ids: set[UUID] = set(subject_audience_ids)
        for candidate in candidates:
            try:
                audience_config = self._resolved_audience_config(
                    candidate,
                    event_definition,
                    subject_audience_ids,
                )
            except ValueError:
                audience_contract_errors.add(candidate.trigger_id)
                continue
            audience_configs[candidate.trigger_id] = audience_config
            audience_user_ids.update(UUID(value) for value in audience_config.get("user_ids", []))

        active_agent_ids = await self.access.list_active_agent_ids(
            {
                candidate.target_id
                for candidate in candidates
                if _value(candidate.target_type) == "agent"
            }
        )
        active_user_ids = await self.access.list_active_user_ids(
            audience_user_ids
            | {
                candidate.execution_principal_id
                for candidate in candidates
                if _value(candidate.execution_principal_type) == "user"
            }
        )
        causation_trigger_ids = await self.access.list_causation_trigger_ids(
            envelope.caused_by_run_id
        )

        created_run_ids: list[UUID] = []
        replayed_run_ids: list[UUID] = []
        outcomes: list[TriggerActivationOutcome] = []
        matched_count = 0
        for candidate in candidates:
            if candidate.trigger_id in audience_contract_errors:
                await self._pause_invalid_binding(candidate, "invalid_authority")
                outcomes.append(
                    TriggerActivationOutcome(
                        candidate.trigger_id,
                        "paused",
                        "invalid_authority",
                    )
                )
                continue
            audience_config = audience_configs[candidate.trigger_id]
            reason = self._readiness_reason(
                candidate,
                envelope,
                active_agent_ids=active_agent_ids,
                active_user_ids=active_user_ids,
                causation_trigger_ids=causation_trigger_ids,
                audience_config=audience_config,
            )
            if reason is not None:
                outcomes.append(TriggerActivationOutcome(candidate.trigger_id, "skipped", reason))
                continue

            try:
                definition, selected_events = self._binding_contract(candidate, registry)
                compiled = definition.compile(candidate.trigger_config)
                if compiled.config != candidate.trigger_config:
                    raise ValueError("stored trigger config is not canonical")
                watch = validate_watch(candidate.watch_config, selected_events)
                filter_tree = validate_filter_tree(candidate.filter_config, selected_events)
                for source, values in collect_option_selections(
                    definition,
                    compiled.config,
                    filter_tree,
                ).items():
                    await self.access.validate_option_selection(source, values)
                if not matches_binding(
                    envelope,
                    event_definition,
                    watch=watch,
                    filter_tree=filter_tree,
                ):
                    outcomes.append(
                        TriggerActivationOutcome(
                            candidate.trigger_id,
                            "skipped",
                            "matcher_false",
                        )
                    )
                    continue
            except (MatcherEvaluationError, MatcherValidationError, ValueError) as exc:
                logger.warning(
                    "Invalid automation matcher org=%s trigger=%s event=%s error=%s",
                    self.organisation_id,
                    candidate.trigger_id,
                    envelope.event_id,
                    exc,
                )
                await self._pause_invalid_binding(candidate, "binding_contract_invalid")
                outcomes.append(
                    TriggerActivationOutcome(
                        candidate.trigger_id,
                        "paused",
                        "binding_contract_invalid",
                    )
                )
                continue

            matched_count += 1
            run = self._build_run(candidate, envelope, audience_config=audience_config)
            if await self.access.insert_run_if_absent(run):
                created_run_ids.append(run.run_id)
                outcomes.append(
                    TriggerActivationOutcome(
                        candidate.trigger_id,
                        "created",
                        run_id=run.run_id,
                    )
                )
                continue

            existing = await self.access.get_run(
                trigger_id=candidate.trigger_id,
                event_id=envelope.event_id,
            )
            if existing is None:
                raise AutomationServiceError(
                    "Trigger activation replay could not resolve its existing run",
                    status_code=409,
                )
            replayed_run_ids.append(existing.run_id)
            outcomes.append(
                TriggerActivationOutcome(
                    candidate.trigger_id,
                    "replayed",
                    run_id=existing.run_id,
                )
            )

        latency_ms = _elapsed_ms(started_at)
        logger.info(
            "Automation activation org=%s event=%s key=%s candidates=%d matched=%d "
            "created=%d replayed=%d correlation=%s latency_ms=%.3f",
            self.organisation_id,
            envelope.event_id,
            envelope.event_key,
            len(candidates),
            matched_count,
            len(created_run_ids),
            len(replayed_run_ids),
            envelope.correlation_id,
            latency_ms,
        )
        return TriggerActivationResult(
            event_id=envelope.event_id,
            candidate_count=len(candidates),
            matched_count=matched_count,
            created_run_ids=tuple(created_run_ids),
            replayed_run_ids=tuple(replayed_run_ids),
            outcomes=tuple(outcomes),
            correlation_id=envelope.correlation_id,
            latency_ms=latency_ms,
        )

    async def _candidates(
        self,
        envelope: AutomationEventEnvelope,
    ) -> tuple[list[AutomationTrigger], UUID | None]:
        if envelope.route.mode == AutomationRouteMode.MATCH:
            return await self.access.list_event_candidates(envelope.event_key), None
        trigger_id = envelope.route.trigger_id
        if trigger_id is None:
            raise AutomationServiceError("Direct automation event is missing trigger identity")
        candidate = await self.access.get_direct_candidate(trigger_id)
        return ([candidate], None) if candidate is not None else ([], trigger_id)

    def _readiness_reason(
        self,
        trigger: AutomationTrigger,
        envelope: AutomationEventEnvelope,
        *,
        active_agent_ids: set[UUID],
        active_user_ids: set[UUID],
        causation_trigger_ids: set[UUID],
        audience_config: dict[str, Any],
    ) -> str | None:
        if _value(trigger.target_type) != "agent":
            return "target_unsupported"
        if trigger.target_id not in active_agent_ids:
            return "target_inactive"
        if (
            envelope.route.mode == AutomationRouteMode.DIRECT
            and envelope.route.config_hash != trigger.config_hash
        ):
            return "stale_config"
        if (
            envelope.route.mode == AutomationRouteMode.DIRECT
            and envelope.event_key not in trigger.match_event_keys
        ):
            return "direct_event_mismatch"

        principal_type = _value(trigger.execution_principal_type)
        scope_type = _value(trigger.scope_type)
        audience_mode = _value(trigger.audience_mode)
        if principal_type == "user":
            if trigger.execution_principal_id not in active_user_ids:
                return "principal_inactive"
            if scope_type != "owned_or_created_by_principal" or audience_mode != "principal":
                return "invalid_authority"
            if envelope.subject.type is not None:
                principal_id = str(trigger.execution_principal_id)
                scoped_values = (
                    envelope.record.get("owner_id"),
                    envelope.record.get("created_by_user_id"),
                )
                if principal_id not in scoped_values:
                    return "scope_mismatch"
        elif principal_type == "organisation_service":
            if (
                trigger.execution_principal_id != self.organisation_id
                or scope_type != "organisation"
                or audience_mode not in {"none", "explicit_users", "subject_participants"}
            ):
                return "invalid_authority"
            audience_ids = {UUID(value) for value in audience_config.get("user_ids", [])}
            if audience_mode == "explicit_users" and not audience_ids <= active_user_ids:
                return "audience_inactive"
            if audience_mode == "subject_participants" and not audience_ids:
                return "audience_empty"
        else:
            return "invalid_authority"

        # copilotgtm: Bulk import execution is fail-closed until preview, rate-limit,
        # and per-binding run-cap controls exist; those controls are the upgrade path.
        if envelope.source.mode in {
            AutomationSourceMode.INITIAL_SYNC,
            AutomationSourceMode.BACKFILL,
        }:
            return "source_mode_denied"
        if envelope.causation_depth >= 3:
            return "causation_depth_exceeded"
        if trigger.trigger_id in causation_trigger_ids:
            return "causation_repeat"
        return None

    @staticmethod
    def _resolved_audience_config(
        trigger: AutomationTrigger,
        event_definition: EventDefinition,
        subject_audience_ids: set[UUID],
    ) -> dict[str, Any]:
        mode = _value(trigger.audience_mode)
        saved = trigger.audience_config
        if mode in {"principal", "none"}:
            if saved:
                raise ValueError("saved audience config must be empty")
            return {}
        if mode == "explicit_users":
            if set(saved) != {"user_ids"} or type(saved.get("user_ids")) is not list:
                raise ValueError("explicit audience config is invalid")
            if any(type(value) is not str for value in saved["user_ids"]):
                raise ValueError("explicit audience IDs are invalid")
            try:
                user_ids = {UUID(value) for value in saved["user_ids"]}
            except (TypeError, ValueError) as exc:
                raise ValueError("explicit audience IDs are invalid") from exc
            if not user_ids or len(user_ids) != len(saved["user_ids"]):
                raise ValueError("explicit audience IDs must be non-empty and unique")
            return {"user_ids": sorted(str(value) for value in user_ids)}
        if mode == "subject_participants":
            if saved or not event_definition.supports_subject_participants:
                raise ValueError("subject participant audience is invalid for this event")
            return {"user_ids": sorted(str(value) for value in subject_audience_ids)}
        raise ValueError("unknown audience mode")

    async def _pause_invalid_binding(
        self,
        trigger: AutomationTrigger,
        error_code: str,
    ) -> None:
        await self.access.pause_invalid_trigger(
            trigger.trigger_id,
            error_code=error_code,
            message="Saved trigger no longer matches the current automation catalog.",
        )

    def _binding_contract(
        self,
        trigger: AutomationTrigger,
        registry: AutomationRegistry,
    ) -> tuple[TriggerDefinition, tuple[EventDefinition, ...]]:
        definition = registry.trigger(trigger.trigger_key)
        if (
            _value(trigger.family) != definition.family.value
            or tuple(trigger.match_event_keys) != definition.match_event_keys
        ):
            raise ValueError("stored trigger projection does not match registry definition")
        events = tuple(registry.event(key) for key in trigger.match_event_keys)
        return definition, events

    def _build_run(
        self,
        trigger: AutomationTrigger,
        envelope: AutomationEventEnvelope,
        *,
        audience_config: dict[str, Any],
    ) -> AgentRun:
        return AgentRun(
            organisation_id=self.organisation_id,
            created_by_user_id=trigger.created_by_user_id,
            execution_principal_type=trigger.execution_principal_type,
            execution_principal_id=trigger.execution_principal_id,
            audience_mode=trigger.audience_mode,
            audience_config=audience_config,
            agent_id=trigger.target_id,
            trigger_id=trigger.trigger_id,
            agent_revision=trigger.target_revision,
            status=AgentRunStatus.PENDING,
            idempotency_key=f"trigger:{trigger.trigger_id}:event:{envelope.event_id}",
            event_id=envelope.event_id,
            event_key=envelope.event_key,
            scheduled_for=_scheduled_for(envelope),
            correlation_id=envelope.correlation_id or str(envelope.event_id),
            caused_by_run_id=envelope.caused_by_run_id,
            causation_depth=envelope.causation_depth,
            trigger_snapshot=_trigger_snapshot(trigger),
            agent_snapshot=dict(trigger.target_snapshot),
            input_snapshot=envelope.model_dump(mode="json"),
        )


def _trigger_snapshot(trigger: AutomationTrigger) -> dict[str, Any]:
    return {
        "trigger_id": str(trigger.trigger_id),
        "trigger_key": trigger.trigger_key,
        "family": _value(trigger.family),
        "match_event_keys": list(trigger.match_event_keys),
        "target_type": _value(trigger.target_type),
        "target_id": str(trigger.target_id),
        "target_revision": trigger.target_revision,
        "config_schema_version": trigger.config_schema_version,
        "trigger_config": dict(trigger.trigger_config),
        "watch_config": dict(trigger.watch_config) if trigger.watch_config else None,
        "filter_config": dict(trigger.filter_config) if trigger.filter_config else None,
        "execution_principal_type": _value(trigger.execution_principal_type),
        "execution_principal_id": str(trigger.execution_principal_id),
        "scope_type": _value(trigger.scope_type),
        "scope_config": dict(trigger.scope_config),
        "audience_mode": _value(trigger.audience_mode),
        "audience_config": dict(trigger.audience_config),
        "task_config": dict(trigger.task_config),
        "delivery_config": dict(trigger.delivery_config),
        "config_hash": trigger.config_hash,
    }


def _scheduled_for(envelope: AutomationEventEnvelope) -> datetime | None:
    value = envelope.attributes.get("scheduled_for")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AutomationServiceError("Scheduled event time must be an RFC 3339 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutomationServiceError("Scheduled event time must be an RFC 3339 string") from exc
    if result.utcoffset() is None:
        raise AutomationServiceError("Scheduled event time must include an offset")
    return result


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _elapsed_ms(started_at: float) -> float:
    return round(max(0.0, (perf_counter() - started_at) * 1000), 3)


__all__ = [
    "AutomationTriggerActivationService",
    "TriggerActivationOutcome",
    "TriggerActivationResult",
]
