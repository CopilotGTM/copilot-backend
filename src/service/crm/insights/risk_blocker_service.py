"""Compatibility facade for CRM risk/blocker services."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.risks.contracts import (
    InvalidStatusTransitionError,
    RiskBlockerActor,
    RiskBlockerNotFoundError,
    RiskBlockerServiceError,
    RiskBlockerWriteContext,
)
from src.service.crm.risks.lifecycle_service import RiskBlockerLifecycleService
from src.service.crm.risks.read_service import RiskBlockerReadService
from src.service.crm.risks.write_service import RiskBlockerWriteService

logger = get_logger("risk_blocker_service")

__all__ = [
    "InvalidStatusTransitionError",
    "RiskBlockerNotFoundError",
    "RiskBlockerService",
    "RiskBlockerServiceError",
]


class RiskBlockerService:
    """Compatibility facade; use CRM risk services for new code."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        user_name: str | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_name = user_name
        self._reader = RiskBlockerReadService(session, organisation_id)
        self._writer = RiskBlockerWriteService(session, organisation_id)
        self._lifecycle = RiskBlockerLifecycleService(session, organisation_id)

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("RiskBlockerService close exception", exc_info=True)

    def _context(self, source: str = "manual") -> RiskBlockerWriteContext:
        return RiskBlockerWriteContext(
            source=source,
            actor=RiskBlockerActor(
                actor_type="user",
                actor_id=self.user_id,
                actor_name=self.user_name,
            ),
        )

    async def list_for_deal(
        self,
        deal_id: UUID,
        limit: int = 20,
        page: int = 1,
        item_type: RiskBlockerType | None = None,
        statuses: list[RiskBlockerStatus] | None = None,
        severity: RiskBlockerSeverity | None = None,
        category: RiskBlockerCategory | None = None,
        include_resolved: bool = False,
        sort_order: str = "desc",
    ):
        return await self._reader.list_for_deal(
            deal_id=deal_id,
            limit=limit,
            page=page,
            item_type=item_type,
            statuses=statuses,
            severity=severity,
            category=category,
            include_resolved=include_resolved,
            sort_order=sort_order,
        )

    async def get_by_id(self, risk_blocker_id: UUID):
        return await self._reader.get_by_id(risk_blocker_id)

    async def create(
        self,
        deal_id: UUID,
        item_type: RiskBlockerType,
        title: str,
        description: str | None = None,
        category: RiskBlockerCategory = RiskBlockerCategory.OTHER,
        severity: RiskBlockerSeverity = RiskBlockerSeverity.MEDIUM,
        owner_name: str | None = None,
        owner_email: str | None = None,
        assigned_team: str | None = None,
    ):
        item = await self._writer.create_manual(
            deal_id=deal_id,
            item_type=item_type,
            title=title,
            context=self._context(),
            description=description,
            category=category,
            severity=severity,
            owner_name=owner_name,
            owner_email=owner_email,
            assigned_team=assigned_team,
        )
        await self.session.commit()
        return item

    async def update(self, risk_blocker_id: UUID, updates: dict[str, Any]):
        item = await self._writer.update_fields(
            risk_blocker_id=risk_blocker_id,
            updates=updates,
            context=self._context(),
        )
        if item:
            await self.session.commit()
        return item

    async def dismiss(self, risk_blocker_id: UUID):
        item = await self._lifecycle.dismiss(risk_blocker_id, context=self._context())
        await self.session.commit()
        return item

    async def resolve(self, risk_blocker_id: UUID, resolution_notes: str):
        item = await self._lifecycle.resolve(
            risk_blocker_id,
            resolution_notes,
            context=self._context(),
        )
        await self.session.commit()
        return item

    async def reopen(self, risk_blocker_id: UUID):
        item = await self._lifecycle.reopen(risk_blocker_id, context=self._context())
        await self.session.commit()
        return item

    async def delete(self, risk_blocker_id: UUID) -> None:
        await self._writer.delete(risk_blocker_id)
        await self.session.commit()
