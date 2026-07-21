"""CRM event matching contracts for automation trigger activation.

The orchestration service is added after the pure matcher is locked. Keeping filter evaluation
free of database state makes replay behavior deterministic and easy to verify independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentRunStatus,
    AutomationTrigger,
)
from src.infra.utils.logger import get_logger
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.activation_access import TriggerActivationAccess
from src.service.automation.triggers.crm_events import CRM_AGENT_EVENT_SCHEMAS

logger = get_logger("automation_trigger_activation")


@dataclass(frozen=True)
class CRMDealStageEvent:
    """Normalized deal-stage mutation data evaluated against persisted trigger filters."""

    event_id: UUID
    deal_id: UUID
    pipeline_id: UUID | None
    from_stage_id: UUID | None
    to_stage_id: UUID | None
    outcome: str
    occurred_at: datetime
    changed_fields: dict[str, Any]


@dataclass(frozen=True)
class TriggerActivationResult:
    """Created and replayed durable runs produced from one source event delivery."""

    event_id: UUID
    matched_count: int
    created_run_ids: tuple[UUID, ...] = ()
    replayed_run_ids: tuple[UUID, ...] = ()
    skipped_reason: str | None = None


class ActivationAccess(Protocol):
    """Persistence operations required by CRM event activation."""

    async def get_crm_event(self, event_id: UUID) -> Any:
        """Load one source event inside the activation organization."""

        ...

    async def get_deal(self, deal_id: UUID) -> Any:
        """Load the current deal snapshot for filter evaluation."""

        ...

    async def get_crm_entity(self, entity_type: str, entity_id: UUID) -> Any:
        """Load the owner-bearing source entity inside the activation organization."""

        ...

    async def list_crm_event_candidates(
        self, *, owner_user_id: UUID, event_type: str
    ) -> list[AutomationTrigger]:
        """Return active event and target pairs for one owner."""

        ...

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        """Create one replay-safe run and report whether this call inserted it."""

        ...

    async def get_run_by_idempotency_key(self, idempotency_key: str) -> AgentRun | None:
        """Resolve the durable run created by an earlier event delivery."""

        ...


def matches_crm_deal_stage_trigger(filter_config: dict[str, Any], event: CRMDealStageEvent) -> bool:
    """Return whether one already-validated trigger filter matches a normalized CRM event."""

    pipeline_ids = {str(value) for value in filter_config.get("pipeline_ids", [])}
    if pipeline_ids and str(event.pipeline_id) not in pipeline_ids:
        return False

    mode = filter_config.get("stage_change_mode")
    if mode == "any_change":
        return True
    if mode == "outcome":
        return filter_config.get("outcome") == event.outcome
    if mode != "specific_stage":
        return False

    from_stage_ids = {str(value) for value in filter_config.get("from_stage_ids", [])}
    to_stage_ids = {str(value) for value in filter_config.get("to_stage_ids", [])}
    if from_stage_ids and str(event.from_stage_id) not in from_stage_ids:
        return False
    if to_stage_ids and str(event.to_stage_id) not in to_stage_ids:
        return False
    return bool(from_stage_ids or to_stage_ids)


class AutomationTriggerActivationService:
    """Create idempotent pending agent runs for one persisted CRM mutation event."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: ActivationAccess | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.access = access or TriggerActivationAccess(session, organisation_id)

    async def activate_crm_event(self, event_id: UUID) -> TriggerActivationResult:
        """Match one durable deal event without committing or publishing worker work."""

        source_event = await self.access.get_crm_event(event_id)
        if source_event is None:
            raise AutomationServiceError("CRM mutation event not found", status_code=404)
        event_type = next(
            (
                semantic
                for semantic in source_event.semantic_event_types
                if semantic in CRM_AGENT_EVENT_SCHEMAS
            ),
            None,
        )
        if event_type is None:
            return TriggerActivationResult(
                event_id=event_id,
                matched_count=0,
                skipped_reason="unsupported_event",
            )
        expected_entity_type = event_type.split(".")[1]
        if source_event.entity_type != expected_entity_type:
            raise AutomationServiceError("CRM event entity does not match its semantic event type")
        entity = await self.access.get_crm_entity(source_event.entity_type, source_event.entity_id)
        if entity is None:
            raise AutomationServiceError("CRM event entity not found", status_code=404)
        if entity.owner_id is None:
            return TriggerActivationResult(
                event_id=event_id,
                matched_count=0,
                skipped_reason=f"unassigned_{source_event.entity_type}",
            )

        candidates = await self.access.list_crm_event_candidates(
            owner_user_id=entity.owner_id,
            event_type=event_type,
        )
        if event_type == "crm.deal.stage_changed":
            event = self._deal_stage_event(source_event, entity)
            matched = [
                trigger
                for trigger in candidates
                if matches_crm_deal_stage_trigger(trigger.filter_config, event)
            ]
        elif event_type == "crm.deal.created":
            matched = [
                trigger
                for trigger in candidates
                if not trigger.filter_config.get("pipeline_ids")
                or str(entity.pipeline_id) in trigger.filter_config["pipeline_ids"]
            ]
        else:
            matched = candidates

        created_run_ids: list[UUID] = []
        replayed_run_ids: list[UUID] = []
        for trigger in matched:
            run = self._build_run(trigger, source_event, event_type, entity)
            if await self.access.insert_run_if_absent(run):
                created_run_ids.append(run.run_id)
                continue
            existing = await self.access.get_run_by_idempotency_key(run.idempotency_key or "")
            if existing is None:
                raise AutomationServiceError(
                    "Trigger activation replay could not resolve its existing run",
                    status_code=409,
                )
            replayed_run_ids.append(existing.run_id)

        logger.info(
            "CRM trigger activation org=%s event=%s candidates=%d matched=%d created=%d replayed=%d",
            self.organisation_id,
            event_id,
            len(candidates),
            len(matched),
            len(created_run_ids),
            len(replayed_run_ids),
        )
        return TriggerActivationResult(
            event_id=event_id,
            matched_count=len(matched),
            created_run_ids=tuple(created_run_ids),
            replayed_run_ids=tuple(replayed_run_ids),
        )

    @staticmethod
    def _deal_stage_event(source_event: Any, deal: Any) -> CRMDealStageEvent:
        stage_change = source_event.changed_fields.get("stage_id")
        if not isinstance(stage_change, dict):
            raise AutomationServiceError("Deal-stage event is missing its stage change")
        return CRMDealStageEvent(
            event_id=source_event.event_id,
            deal_id=deal.deal_id,
            pipeline_id=deal.pipeline_id,
            from_stage_id=_optional_uuid(stage_change.get("old")),
            to_stage_id=_optional_uuid(stage_change.get("new")),
            outcome=str(getattr(deal.deal_status, "value", deal.deal_status)),
            occurred_at=source_event.occurred_at,
            changed_fields=dict(source_event.changed_fields),
        )

    def _build_run(
        self, trigger: AutomationTrigger, source_event: Any, event_type: str, entity: Any
    ) -> AgentRun:
        idempotency_key = f"trigger:{trigger.trigger_id}:crm-event:{source_event.event_id}"
        return AgentRun(
            organisation_id=self.organisation_id,
            owner_user_id=trigger.owner_user_id,
            agent_id=trigger.target_id,
            trigger_id=trigger.trigger_id,
            agent_revision=trigger.target_revision,
            status=AgentRunStatus.PENDING,
            idempotency_key=idempotency_key,
            source_domain="crm",
            event_type=event_type,
            source_event_id=str(source_event.event_id),
            scheduled_for=None,
            trigger_snapshot=self._trigger_snapshot(trigger),
            agent_snapshot=dict(trigger.target_snapshot),
            input_snapshot={
                "schema_key": CRM_AGENT_EVENT_SCHEMAS[event_type],
                "event": {
                    "event_id": str(source_event.event_id),
                    "event_type": source_event.event_type,
                    "semantic_event_type": event_type,
                    "operation": source_event.operation,
                    "occurred_at": source_event.occurred_at.isoformat(),
                    "changed_fields": source_event.changed_fields,
                },
                source_event.entity_type: self._entity_snapshot(source_event.entity_type, entity),
            },
        )

    @staticmethod
    def _entity_snapshot(entity_type: str, entity: Any) -> dict[str, Any]:
        """Expose stable entity identities needed by the agent to load richer CRM context."""

        if entity_type == "deal":
            return {
                "deal_id": str(entity.deal_id),
                "pipeline_id": str(entity.pipeline_id) if entity.pipeline_id else None,
                "stage_id": str(entity.stage_id) if entity.stage_id else None,
                "outcome": str(getattr(entity.deal_status, "value", entity.deal_status)),
            }
        if entity_type == "contact":
            return {
                "contact_id": str(entity.contact_id),
                "company_id": str(entity.company_id) if entity.company_id else None,
            }
        return {"account_id": str(entity.company_id)}

    @staticmethod
    def _trigger_snapshot(trigger: AutomationTrigger) -> dict[str, Any]:
        return {
            "trigger_id": str(trigger.trigger_id),
            "target_id": str(trigger.target_id),
            "target_revision": trigger.target_revision,
            "config_schema_version": trigger.config_schema_version,
            "trigger_type": getattr(trigger.trigger_type, "value", trigger.trigger_type),
            "source_domain": trigger.source_domain,
            "event_type": trigger.event_type,
            "trigger_config": trigger.trigger_config,
            "filter_config": trigger.filter_config,
            "scope_type": getattr(trigger.scope_type, "value", trigger.scope_type),
            "scope_config": trigger.scope_config,
            "task_config": trigger.task_config,
            "delivery_config": trigger.delivery_config,
            "config_hash": trigger.config_hash,
        }


def _optional_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AutomationServiceError("Deal-stage event contains an invalid stage identity") from exc
