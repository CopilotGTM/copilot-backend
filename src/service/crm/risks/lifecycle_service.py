"""Organisation-scoped risk/blocker lifecycle transitions and projections.

CRM routes, agent tools, and extraction workers call this service to apply a
status transition, append audit/provenance data, flush the row, project it to
the activity feed, and enqueue the matching automation signal. Every lookup
keeps the organisation predicate so a caller-supplied row ID cannot cross the
tenant boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.contracts.crm.risk_blockers import RiskBlockerDetail
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.service.automation.events.contracts import (
    AutomationActorType,
    AutomationEventActor,
    AutomationSourceMode,
)
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.producers.signals import (
    AISignalEventAdapter,
    SignalTransition,
    prepare_manual_signal_transition,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.risks.contracts import (
    InvalidStatusTransitionError,
    RiskBlockerNotFoundError,
    RiskBlockerWriteContext,
)
from src.service.crm.risks.mapper import RiskBlockerMapper
from src.service.crm.risks.write_service import (
    AutomationEventEnqueuer,
    add_risk_blocker_audit_entry,
    persist_risk_blocker_inference,
)


def _status_value(status: RiskBlockerStatus | str) -> str:
    return status.value if isinstance(status, RiskBlockerStatus) else str(status)


class RiskBlockerLifecycleService:
    """Coordinate Risk Blocker Lifecycle Service for the current organisation-scoped operation."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        automation_events: AutomationEventEnqueuer | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()
        self._projection = ActivityFeedProjectionService(session, organisation_id)
        self._automation_events = automation_events or AutomationEventDispatchService(
            session,
            organisation_id,
        )

    async def dismiss(
        self,
        risk_blocker_id: UUID,
        context: RiskBlockerWriteContext,
        reason: str | None = None,
    ) -> RiskBlockerDetail:
        """Dismiss the Risk Blocker Lifecycle Service record and publish the durable lifecycle projection."""
        item = await self._load_item_or_raise(risk_blocker_id)
        previous_status = _status_value(item.status)
        now = datetime.now(UTC)
        item.status = RiskBlockerStatus.DISMISSED
        item.closed_at = now
        item.closed_by_user_id = context.actor.actor_id
        item.closed_by_user_name = context.actor.actor_name
        item.last_modified_by_user_id = context.actor.actor_id
        item.last_modified_by_user_name = context.actor.actor_name
        item.updated_at = now

        details: dict[str, str] = {"previous_status": previous_status}
        if reason:
            details["reason"] = reason
        add_risk_blocker_audit_entry(item, "dismissed", details, context)
        if context.inference is not None:
            persist_risk_blocker_inference(item, context.inference)
        # Timeline is deprecated; create activity-feed projection instead.
        await self.session.flush()
        await self.session.refresh(item)
        await self._projection.upsert_risk_blocker_lifecycle(
            item,
            source_type=self._lifecycle_source_type(item, "DISMISSED"),
        )
        await self._enqueue_signal(item, context, "resolved")
        return self._mapper.to_detail(item)

    async def resolve(
        self,
        risk_blocker_id: UUID,
        resolution_notes: str,
        context: RiskBlockerWriteContext,
    ) -> RiskBlockerDetail:
        """Resolve the Risk Blocker Lifecycle Service record and publish the durable lifecycle projection."""
        item = await self._load_item_or_raise(risk_blocker_id)
        previous_status = _status_value(item.status)
        now = datetime.now(UTC)
        item.status = RiskBlockerStatus.RESOLVED
        item.resolution_notes = resolution_notes
        item.closed_at = now
        item.closed_by_user_id = context.actor.actor_id
        item.closed_by_user_name = context.actor.actor_name
        item.last_modified_by_user_id = context.actor.actor_id
        item.last_modified_by_user_name = context.actor.actor_name
        item.updated_at = now

        add_risk_blocker_audit_entry(
            item,
            "resolved",
            {"previous_status": previous_status, "resolution_notes": resolution_notes},
            context,
        )
        if context.inference is not None:
            persist_risk_blocker_inference(item, context.inference)
        # Timeline is deprecated; create activity-feed projection instead.
        await self.session.flush()
        await self.session.refresh(item)
        await self._projection.upsert_risk_blocker_lifecycle(
            item,
            source_type=self._lifecycle_source_type(item, "RESOLVED"),
        )
        await self._enqueue_signal(item, context, "resolved")
        return self._mapper.to_detail(item)

    async def reopen(
        self,
        risk_blocker_id: UUID,
        context: RiskBlockerWriteContext,
    ) -> RiskBlockerDetail:
        """Reopen the Risk Blocker Lifecycle Service record after validating its current lifecycle state."""
        item = await self._load_item_or_raise(risk_blocker_id)
        current_status = _status_value(item.status)
        if current_status not in [
            RiskBlockerStatus.RESOLVED.value,
            RiskBlockerStatus.DISMISSED.value,
        ]:
            raise InvalidStatusTransitionError(current_status, "OPEN")

        item.status = RiskBlockerStatus.OPEN
        item.resolution_notes = None
        item.closed_at = None
        item.closed_by_user_id = None
        item.closed_by_user_name = None
        item.last_modified_by_user_id = context.actor.actor_id
        item.last_modified_by_user_name = context.actor.actor_name
        item.updated_at = datetime.now(UTC)
        add_risk_blocker_audit_entry(
            item,
            "reopened",
            {"previous_status": current_status},
            context,
        )
        if context.inference is not None:
            persist_risk_blocker_inference(item, context.inference)
        await self.session.flush()
        await self.session.refresh(item)
        await self._projection.upsert_risk_blocker_lifecycle(
            item,
            source_type=self._lifecycle_source_type(item, "REOPENED"),
        )
        await self._enqueue_signal(item, context, "changed")
        return self._mapper.to_detail(item)

    async def get_status(self, risk_blocker_id: UUID) -> RiskBlockerStatus | str | None:
        """Load status within the organisation boundary."""
        item = await self._load_item(risk_blocker_id)
        return item.status if item else None

    async def _load_item_or_raise(self, risk_blocker_id: UUID) -> DealRiskBlocker:
        item = await self._load_item(risk_blocker_id)
        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)
        return item

    async def _load_item(self, risk_blocker_id: UUID) -> DealRiskBlocker | None:
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        return result.scalar_one_or_none()

    async def _enqueue_signal(
        self,
        item: DealRiskBlocker,
        context: RiskBlockerWriteContext,
        transition: SignalTransition,
    ) -> None:
        if context.inference is None:
            if not prepare_manual_signal_transition(item):
                return
            actor_type = {
                "user": AutomationActorType.USER,
                "system": AutomationActorType.SYSTEM,
                "workflow": AutomationActorType.AGENT,
            }[context.actor.actor_type]
            envelope = AISignalEventAdapter(self.organisation_id).from_risk_blocker(
                item,
                transition=transition,
                actor=AutomationEventActor(
                    type=actor_type,
                    id=str(context.actor.actor_id),
                ),
                source_mode=AutomationSourceMode.INTERACTIVE,
            )
        else:
            envelope = AISignalEventAdapter(self.organisation_id).from_risk_blocker(
                item,
                transition=transition,
            )
        if envelope is not None:
            await self._automation_events.enqueue(envelope)

    @staticmethod
    def _lifecycle_source_type(item: DealRiskBlocker, action: str) -> str:
        return f"{'RISK' if item.item_type == RiskBlockerType.RISK else 'BLOCKER'}_{action}"
