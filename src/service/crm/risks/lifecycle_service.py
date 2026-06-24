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
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.risks.contracts import (
    InvalidStatusTransitionError,
    RiskBlockerNotFoundError,
    RiskBlockerWriteContext,
)
from src.service.crm.risks.mapper import RiskBlockerMapper
from src.service.crm.risks.write_service import add_risk_blocker_audit_entry
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_risk_blocker_timeline_event,
)


def _status_value(status: RiskBlockerStatus | str) -> str:
    return status.value if isinstance(status, RiskBlockerStatus) else str(status)


class RiskBlockerLifecycleService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()
        self._projection = ActivityFeedProjectionService(session, organisation_id)

    async def dismiss(
        self,
        risk_blocker_id: UUID,
        context: RiskBlockerWriteContext,
        reason: str | None = None,
    ) -> RiskBlockerDetail:
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
        self.session.add(
            build_risk_blocker_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=item.deal_id,
                risk_blocker=item,
                event_type=TimelineEventType.BLOCKER_DISMISSED,
            )
        )
        await self.session.flush()
        await self.session.refresh(item)
        await self._projection.upsert_risk_blocker_lifecycle(
            item,
            source_type=self._lifecycle_source_type(item, "DISMISSED"),
        )
        return self._mapper.to_detail(item)

    async def resolve(
        self,
        risk_blocker_id: UUID,
        resolution_notes: str,
        context: RiskBlockerWriteContext,
    ) -> RiskBlockerDetail:
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
        self.session.add(
            build_risk_blocker_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=item.deal_id,
                risk_blocker=item,
                event_type=TimelineEventType.BLOCKER_RESOLVED,
            )
        )
        await self.session.flush()
        await self.session.refresh(item)
        await self._projection.upsert_risk_blocker_lifecycle(
            item,
            source_type=self._lifecycle_source_type(item, "RESOLVED"),
        )
        return self._mapper.to_detail(item)

    async def reopen(
        self,
        risk_blocker_id: UUID,
        context: RiskBlockerWriteContext,
    ) -> RiskBlockerDetail:
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
        await self.session.flush()
        await self.session.refresh(item)
        await self._projection.upsert_risk_blocker_lifecycle(
            item,
            source_type=self._lifecycle_source_type(item, "REOPENED"),
        )
        return self._mapper.to_detail(item)

    async def get_status(self, risk_blocker_id: UUID) -> RiskBlockerStatus | str | None:
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

    @staticmethod
    def _lifecycle_source_type(item: DealRiskBlocker, action: str) -> str:
        return f"{'RISK' if item.item_type == RiskBlockerType.RISK else 'BLOCKER'}_{action}"
