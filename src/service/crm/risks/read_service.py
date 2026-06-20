from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.risk_blocker_models import (
    RiskBlockerDetail,
    RiskBlockerListItem,
    RiskBlockerSummary,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.service.crm.risks.contracts import RiskBlockerServiceError
from src.service.crm.risks.mapper import RiskBlockerMapper


class RiskBlockerReadService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()

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
    ) -> tuple[list[RiskBlockerListItem], int, RiskBlockerSummary]:
        base_query = select(DealRiskBlocker).where(
            col(DealRiskBlocker.deal_id) == deal_id,
            col(DealRiskBlocker.organisation_id) == self.organisation_id,
        )
        query = base_query

        if item_type:
            query = query.where(col(DealRiskBlocker.item_type) == item_type)

        if statuses:
            query = query.where(col(DealRiskBlocker.status).in_(statuses))
        elif not include_resolved:
            query = query.where(
                col(DealRiskBlocker.status).not_in(
                    [
                        RiskBlockerStatus.RESOLVED,
                        RiskBlockerStatus.DISMISSED,
                        RiskBlockerStatus.WONT_FIX,
                    ]
                )
            )

        if severity:
            query = query.where(col(DealRiskBlocker.severity) == severity)

        if category:
            query = query.where(col(DealRiskBlocker.category) == category)

        total_count = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        summary = await self._get_summary(deal_id)

        if page < 1:
            raise RiskBlockerServiceError("Page must be >= 1", status_code=400)

        if sort_order == "desc":
            query = query.order_by(
                col(DealRiskBlocker.created_at).desc(),
                col(DealRiskBlocker.risk_blocker_id).desc(),
            )
        else:
            query = query.order_by(
                col(DealRiskBlocker.created_at).asc(),
                col(DealRiskBlocker.risk_blocker_id).asc(),
            )

        query = query.offset((page - 1) * limit).limit(limit)
        result = await self.session.execute(query)
        items = list(result.scalars().all())
        return [self._mapper.to_list_item(item) for item in items], total_count or 0, summary

    async def list_models_for_deal(
        self,
        deal_id: UUID,
        statuses: list[RiskBlockerStatus] | None = None,
        limit: int = 100,
    ) -> list[DealRiskBlocker]:
        query = select(DealRiskBlocker).where(
            col(DealRiskBlocker.deal_id) == deal_id,
            col(DealRiskBlocker.organisation_id) == self.organisation_id,
        )
        if statuses:
            query = query.where(col(DealRiskBlocker.status).in_(statuses))
        query = query.order_by(col(DealRiskBlocker.created_at).desc()).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def _get_summary(self, deal_id: UUID) -> RiskBlockerSummary:
        query = select(
            func.count().label("total"),
            func.count()
            .filter(col(DealRiskBlocker.item_type) == RiskBlockerType.RISK)
            .label("risks"),
            func.count()
            .filter(col(DealRiskBlocker.item_type) == RiskBlockerType.BLOCKER)
            .label("blockers"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.OPEN)
            .label("open"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.IN_PROGRESS)
            .label("in_progress"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.RESOLVED)
            .label("resolved"),
            func.count()
            .filter(col(DealRiskBlocker.severity) == RiskBlockerSeverity.CRITICAL)
            .label("critical"),
            func.count()
            .filter(col(DealRiskBlocker.severity) == RiskBlockerSeverity.HIGH)
            .label("high"),
        ).where(
            col(DealRiskBlocker.deal_id) == deal_id,
            col(DealRiskBlocker.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(query)
        row = result.one()

        return RiskBlockerSummary(
            total_count=row.total or 0,
            risks_count=row.risks or 0,
            blockers_count=row.blockers or 0,
            open_count=row.open or 0,
            in_progress_count=row.in_progress or 0,
            resolved_count=row.resolved or 0,
            critical_count=row.critical or 0,
            high_count=row.high or 0,
        )

    async def get_by_id(self, risk_blocker_id: UUID) -> RiskBlockerDetail | None:
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()
        return self._mapper.to_detail(item) if item else None
