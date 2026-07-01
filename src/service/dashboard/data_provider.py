"""Dashboard-owned CRM data projections for the application Sales Dashboard."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Integer, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
)
from src.infra.utils.logger import get_logger
from src.service.crm.analytics.stage_movement_service import (
    StageMovementAnalytics,
    StageMovementDeal,
    StageMovementHistory,
    calculate_stage_movement_analytics,
)

logger = get_logger("sales_dashboard_data_provider")


class SalesDashboardDataProviderError(Exception):
    """Raised when dashboard CRM read projections cannot be built."""


class SalesDashboardDataProvider:
    """Read model provider for dashboard widgets backed by CRM tables.

    The dashboard owns these projections because the grouping, labels, and warning
    semantics are dashboard contracts, not general CRM API behavior.
    """

    def __init__(self, org_session: AsyncSession, organisation_id: UUID) -> None:
        self.org_session = org_session
        self.organisation_id = organisation_id

    def _dashboard_deal_filters(
        self,
        c,
        *,
        from_date: datetime,
        to_date: datetime,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[Any]:
        filters = [
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            c.created_at >= from_date,
            c.created_at <= to_date,
        ]
        pipeline_id_values = list(pipeline_ids or [])
        owner_id_values = list(owner_ids or [])
        if pipeline_id_values:
            filters.append(c.pipeline_id.in_(pipeline_id_values))
        if owner_id_values:
            filters.append(c.owner_id.in_(owner_id_values))
        if min_deal_value is not None:
            filters.append(c.amount >= min_deal_value)
        if max_deal_value is not None:
            filters.append(c.amount <= max_deal_value)
        return filters

    @staticmethod
    def _expected_close_extension_count_expr(c):
        return func.coalesce(
            c.properties["expected_close_extended_count"].astext.cast(Integer),
            0,
        )

    async def get_summary_cards(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> dict[str, Any]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            extension_count = self._expected_close_extension_count_expr(c)
            age_days = (
                func.extract(
                    "epoch",
                    to_date - func.coalesce(c.source_created_at, c.created_at),
                )
                / 86400
            )
            stmt = select(
                func.count(c.deal_id).label("total_deals"),
                func.coalesce(
                    func.sum(c.amount).filter(c.deal_status == CRMDealStatus.OPEN.value),
                    0,
                ).label("total_pipeline_value"),
                func.count(c.deal_id)
                .filter(c.deal_status == CRMDealStatus.OPEN.value)
                .label("active_deals"),
                func.count(c.deal_id)
                .filter(
                    and_(
                        c.deal_status == CRMDealStatus.OPEN.value,
                        c.close_date.is_not(None),
                        c.close_date < to_date,
                    )
                )
                .label("overdue_open_deals"),
                func.count(c.deal_id).filter(extension_count > 0).label("extended_deals"),
                func.avg(age_days).label("average_deal_age_days"),
            ).where(
                *self._dashboard_deal_filters(
                    c,
                    from_date=from_date,
                    to_date=to_date,
                    pipeline_ids=pipeline_ids,
                    owner_ids=owner_ids,
                    min_deal_value=min_deal_value,
                    max_deal_value=max_deal_value,
                )
            )

            row = (await self.org_session.execute(stmt)).one()
            average_age = row.average_deal_age_days
            return {
                "total_pipeline_value": float(row.total_pipeline_value or 0),
                "total_deals": int(row.total_deals or 0),
                "active_deals": int(row.active_deals or 0),
                "overdue_open_deals": int(row.overdue_open_deals or 0),
                "extended_deals": int(row.extended_deals or 0),
                "average_deal_age_days": round(float(average_age), 2)
                if average_age is not None
                else None,
            }
        except Exception as e:
            logger.error("Failed to calculate dashboard summary cards: %s", e, exc_info=True)
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard summary cards"
            ) from e

    async def get_primary_distribution(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        segment: str,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[dict[str, Any]]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            segment_columns = {
                "stage": (c.stage_label, c.stage_name),
                "pipeline": (c.pipeline_label, c.pipeline_name),
                "status": (c.deal_status, None),
                "owner": (c.owner_name, c.owner_email),
            }
            primary_column, fallback_column = segment_columns.get(segment, segment_columns["stage"])
            label_expr = (
                func.coalesce(primary_column, fallback_column, "Unknown")
                if fallback_column is not None
                else func.coalesce(primary_column, "Unknown")
            )
            stmt = (
                select(
                    label_expr.label("label"),
                    func.count(c.deal_id).label("count"),
                    func.coalesce(func.sum(c.amount), 0).label("value"),
                )
                .where(
                    *self._dashboard_deal_filters(
                        c,
                        from_date=from_date,
                        to_date=to_date,
                        pipeline_ids=pipeline_ids,
                        owner_ids=owner_ids,
                        min_deal_value=min_deal_value,
                        max_deal_value=max_deal_value,
                    )
                )
                .group_by(label_expr)
                .order_by(func.count(c.deal_id).desc(), func.coalesce(func.sum(c.amount), 0).desc())
            )
            result = await self.org_session.execute(stmt)
            return [
                {
                    "label": str(row.label),
                    "count": int(row.count or 0),
                    "value": float(row.value or 0),
                }
                for row in result.all()
            ]
        except Exception as e:
            logger.error("Failed to calculate dashboard primary distribution: %s", e, exc_info=True)
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard primary distribution"
            ) from e

    async def get_sales_cycle_by_value_bucket(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        buckets: list[dict[str, Any]],
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[dict[str, Any]]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            items: list[dict[str, Any]] = []
            age_days = (
                func.extract(
                    "epoch",
                    to_date - func.coalesce(c.source_created_at, c.created_at),
                )
                / 86400
            )
            for bucket in buckets:
                stmt = select(
                    func.count(c.deal_id).label("count"),
                    func.coalesce(func.sum(c.amount), 0).label("value"),
                    func.avg(age_days).label("average_age_days"),
                    func.count(c.deal_id)
                    .filter(c.deal_status == CRMDealStatus.OPEN.value)
                    .label("open_deals"),
                ).where(
                    *self._dashboard_deal_filters(
                        c,
                        from_date=from_date,
                        to_date=to_date,
                        pipeline_ids=pipeline_ids,
                        owner_ids=owner_ids,
                        min_deal_value=min_deal_value,
                        max_deal_value=max_deal_value,
                    )
                )
                if bucket.get("min_value") is not None:
                    stmt = stmt.where(c.amount >= bucket["min_value"])
                if bucket.get("max_value") is not None:
                    stmt = stmt.where(c.amount < bucket["max_value"])

                row = (await self.org_session.execute(stmt)).one()
                average_age = row.average_age_days
                items.append(
                    {
                        "label": bucket["label"],
                        "count": int(row.count or 0),
                        "value": float(row.value or 0),
                        "average_age_days": round(float(average_age), 2)
                        if average_age is not None
                        else None,
                        "open_deals": int(row.open_deals or 0),
                    }
                )
            return items
        except Exception as e:
            logger.error(
                "Failed to calculate dashboard sales cycle by value bucket: %s",
                e,
                exc_info=True,
            )
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard sales cycle by value bucket"
            ) from e

    async def get_extension_frequency_by_stage(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[dict[str, Any]]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            extension_count = self._expected_close_extension_count_expr(c)
            stage_label = func.coalesce(c.stage_label, c.stage_name, "Unknown")
            stmt = (
                select(
                    stage_label.label("label"),
                    func.count(c.deal_id).label("total_deals"),
                    func.count(c.deal_id).filter(extension_count > 0).label("extended_deals"),
                    func.coalesce(func.sum(c.amount), 0).label("value"),
                )
                .where(
                    *self._dashboard_deal_filters(
                        c,
                        from_date=from_date,
                        to_date=to_date,
                        pipeline_ids=pipeline_ids,
                        owner_ids=owner_ids,
                        min_deal_value=min_deal_value,
                        max_deal_value=max_deal_value,
                    )
                )
                .group_by(stage_label)
                .order_by(
                    func.count(c.deal_id).filter(extension_count > 0).desc(),
                    func.count(c.deal_id).desc(),
                )
            )
            result = await self.org_session.execute(stmt)
            rows = []
            for row in result.all():
                total_deals = int(row.total_deals or 0)
                extended_deals = int(row.extended_deals or 0)
                rows.append(
                    {
                        "label": row.label,
                        "total_deals": total_deals,
                        "extended_deals": extended_deals,
                        "extension_rate": round((extended_deals / total_deals) * 100, 2)
                        if total_deals
                        else 0,
                        "value": float(row.value or 0),
                    }
                )
            return rows
        except Exception as e:
            logger.error(
                "Failed to calculate dashboard extension frequency by stage: %s",
                e,
                exc_info=True,
            )
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard extension frequency by stage"
            ) from e

    async def get_owner_slippage(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[dict[str, Any]]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            extension_count = self._expected_close_extension_count_expr(c)
            owner_label = func.coalesce(c.owner_name, c.owner_email, "Unassigned")
            stmt = (
                select(
                    owner_label.label("label"),
                    func.count(c.deal_id).label("total_deals"),
                    func.count(c.deal_id).filter(extension_count > 0).label("extended_deals"),
                    func.coalesce(func.sum(c.amount), 0).label("value"),
                )
                .where(
                    *self._dashboard_deal_filters(
                        c,
                        from_date=from_date,
                        to_date=to_date,
                        pipeline_ids=pipeline_ids,
                        owner_ids=owner_ids,
                        min_deal_value=min_deal_value,
                        max_deal_value=max_deal_value,
                    )
                )
                .group_by(owner_label)
                .order_by(
                    func.count(c.deal_id).filter(extension_count > 0).desc(),
                    func.count(c.deal_id).desc(),
                )
            )
            result = await self.org_session.execute(stmt)
            rows = []
            for row in result.all():
                total_deals = int(row.total_deals or 0)
                extended_deals = int(row.extended_deals or 0)
                rows.append(
                    {
                        "label": row.label,
                        "total_deals": total_deals,
                        "extended_deals": extended_deals,
                        "extension_rate": round((extended_deals / total_deals) * 100, 2)
                        if total_deals
                        else 0,
                        "value": float(row.value or 0),
                    }
                )
            return rows
        except Exception as e:
            logger.error("Failed to calculate dashboard owner slippage: %s", e, exc_info=True)
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard owner slippage"
            ) from e

    async def get_no_movement_deals(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
        stale_threshold_days: int = 30,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            days_in_stage = func.extract("epoch", to_date - c.stage_entered_at) / 86400
            stmt = (
                select(
                    c.deal_id,
                    c.name,
                    c.stage_label,
                    c.stage_name,
                    c.owner_name,
                    days_in_stage.label("days_in_stage"),
                    c.amount,
                    c.stage_entered_at,
                )
                .where(
                    *self._dashboard_deal_filters(
                        c,
                        from_date=from_date,
                        to_date=to_date,
                        pipeline_ids=pipeline_ids,
                        owner_ids=owner_ids,
                        min_deal_value=min_deal_value,
                        max_deal_value=max_deal_value,
                    ),
                    c.deal_status == CRMDealStatus.OPEN.value,
                    c.stage_entered_at.is_not(None),
                    days_in_stage >= stale_threshold_days,
                )
                .order_by(days_in_stage.desc(), c.amount.desc().nulls_last())
                .limit(limit)
            )
            result = await self.org_session.execute(stmt)
            return [
                {
                    "deal_id": row.deal_id,
                    "name": row.name,
                    "stage": row.stage_label or row.stage_name,
                    "owner": row.owner_name,
                    "days_in_stage": int(row.days_in_stage or 0),
                    "amount": float(row.amount) if row.amount is not None else None,
                    "last_movement_date": row.stage_entered_at,
                }
                for row in result.all()
            ]
        except Exception as e:
            logger.error("Failed to calculate dashboard no movement deals: %s", e, exc_info=True)
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard no movement deals"
            ) from e

    async def get_slipped_overdue_deals(
        self,
        *,
        from_date: datetime,
        to_date: datetime,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        try:
            c = cast(Any, CRMDeal).__table__.c
            extension_count = self._expected_close_extension_count_expr(c)
            days_overdue = func.extract("epoch", to_date - c.close_date) / 86400
            stmt = (
                select(
                    c.deal_id,
                    c.name,
                    c.deal_status,
                    c.stage_label,
                    c.stage_name,
                    c.owner_name,
                    c.expected_close_date,
                    c.close_date,
                    c.properties,
                    days_overdue.label("days_overdue"),
                    extension_count.label("extension_count"),
                )
                .where(
                    *self._dashboard_deal_filters(
                        c,
                        from_date=from_date,
                        to_date=to_date,
                        pipeline_ids=pipeline_ids,
                        owner_ids=owner_ids,
                        min_deal_value=min_deal_value,
                        max_deal_value=max_deal_value,
                    ),
                    or_(
                        extension_count > 0,
                        and_(
                            c.deal_status == CRMDealStatus.OPEN.value,
                            c.close_date.is_not(None),
                            c.close_date < to_date,
                        ),
                    ),
                )
                .order_by(extension_count.desc(), days_overdue.desc().nulls_last())
                .limit(limit)
            )
            result = await self.org_session.execute(stmt)
            rows = []
            for row in result.all():
                properties = row.properties if isinstance(row.properties, dict) else {}
                current_expected = properties.get("current_expected_close_date") or row.close_date
                reason = properties.get("latest_expected_close_change_reason")
                extension_count_value = int(row.extension_count or 0)
                overdue_days = max(int(row.days_overdue or 0), 0)
                rows.append(
                    {
                        "deal_id": row.deal_id,
                        "name": row.name,
                        "status": "Slipped" if extension_count_value > 0 else "Overdue",
                        "stage": row.stage_label or row.stage_name,
                        "owner": row.owner_name,
                        "expected_close_date": row.expected_close_date,
                        "current_expected_close_date": current_expected,
                        "reason": reason,
                        "days_deviation": extension_count_value,
                        "days_overdue": overdue_days,
                    }
                )
            return rows
        except Exception as e:
            logger.error(
                "Failed to calculate dashboard slipped overdue deals: %s",
                e,
                exc_info=True,
            )
            raise SalesDashboardDataProviderError(
                "Failed to calculate dashboard slipped overdue deals"
            ) from e

    async def get_stage_movement_analytics(
        self,
        *,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        pipeline_id: UUID | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
        stale_threshold_days: int = 30,
        recent_limit: int = 10,
    ) -> StageMovementAnalytics:
        try:
            deals = await self._get_stage_movement_deals(
                pipeline_id=pipeline_id,
                min_deal_value=min_deal_value,
                max_deal_value=max_deal_value,
            )
            history = await self._get_stage_movement_history(
                from_date=from_date,
                to_date=to_date,
                pipeline_id=pipeline_id,
                min_deal_value=min_deal_value,
                max_deal_value=max_deal_value,
            )
            return calculate_stage_movement_analytics(
                deals=deals,
                history=history,
                from_date=from_date,
                to_date=to_date,
                now=datetime.now(UTC),
                stale_threshold_days=stale_threshold_days,
                recent_limit=recent_limit,
            )
        except Exception as e:
            logger.error("Failed to calculate stage movement analytics: %s", e, exc_info=True)
            raise SalesDashboardDataProviderError(
                "Failed to calculate stage movement analytics"
            ) from e

    async def _get_stage_movement_deals(
        self,
        *,
        pipeline_id: UUID | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[StageMovementDeal]:
        c = cast(Any, CRMDeal).__table__.c
        stmt = select(
            c.deal_id,
            c.name,
            c.amount,
            c.deal_status,
            c.stage_label,
            c.stage_name,
            c.stage_entered_at,
        ).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        if pipeline_id is not None:
            stmt = stmt.where(c.pipeline_id == pipeline_id)
        if min_deal_value is not None:
            stmt = stmt.where(c.amount >= min_deal_value)
        if max_deal_value is not None:
            stmt = stmt.where(c.amount <= max_deal_value)
        result = await self.org_session.execute(stmt)
        deals: list[StageMovementDeal] = []
        for row in result.all():
            status = row.deal_status.value if hasattr(row.deal_status, "value") else row.deal_status
            deals.append(
                StageMovementDeal(
                    deal_id=row.deal_id,
                    name=row.name,
                    amount=float(row.amount) if row.amount is not None else None,
                    status=status,
                    stage=row.stage_label or row.stage_name,
                    stage_entered_at=row.stage_entered_at,
                )
            )
        return deals

    async def _get_stage_movement_history(
        self,
        *,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        pipeline_id: UUID | None = None,
        min_deal_value: float | None = None,
        max_deal_value: float | None = None,
    ) -> list[StageMovementHistory]:
        h_c = cast(Any, CRMDealStageHistory).__table__.c
        d_c = cast(Any, CRMDeal).__table__.c
        to_stage = cast(Any, CRMDealStage).__table__.alias("to_stage")
        from_stage = cast(Any, CRMDealStage).__table__.alias("from_stage")

        base_stmt = (
            select(
                h_c.deal_id,
                d_c.name.label("deal_name"),
                d_c.amount.label("deal_amount"),
                d_c.owner_name,
                h_c.entered_at,
                h_c.stage_id,
                h_c.from_stage_id,
                to_stage.c.label.label("to_stage_label"),
                to_stage.c.name.label("to_stage_name"),
                from_stage.c.label.label("from_stage_label"),
                from_stage.c.name.label("from_stage_name"),
                func.lag(h_c.entered_at)
                .over(partition_by=h_c.deal_id, order_by=h_c.entered_at)
                .label("previous_entered_at"),
            )
            .join(
                cast(Any, CRMDeal).__table__,
                and_(
                    h_c.deal_id == d_c.deal_id,
                    d_c.organisation_id == self.organisation_id,
                    d_c.is_deleted.is_(False),
                ),
            )
            .outerjoin(
                to_stage,
                and_(
                    h_c.stage_id == to_stage.c.stage_id,
                    to_stage.c.organisation_id == self.organisation_id,
                ),
            )
            .outerjoin(
                from_stage,
                and_(
                    h_c.from_stage_id == from_stage.c.stage_id,
                    from_stage.c.organisation_id == self.organisation_id,
                ),
            )
            .where(
                h_c.organisation_id == self.organisation_id,
                h_c.is_deleted.is_(False),
            )
        )
        if pipeline_id is not None:
            base_stmt = base_stmt.where(d_c.pipeline_id == pipeline_id)
        if min_deal_value is not None:
            base_stmt = base_stmt.where(d_c.amount >= min_deal_value)
        if max_deal_value is not None:
            base_stmt = base_stmt.where(d_c.amount <= max_deal_value)
        if to_date is not None:
            base_stmt = base_stmt.where(h_c.entered_at <= to_date)
        if from_date is not None:
            window_h = cast(Any, CRMDealStageHistory).__table__.alias("window_history")
            window_d = cast(Any, CRMDeal).__table__.alias("window_deals")
            window_deals_stmt = (
                select(window_h.c.deal_id)
                .join(
                    window_d,
                    and_(
                        window_h.c.deal_id == window_d.c.deal_id,
                        window_d.c.organisation_id == self.organisation_id,
                        window_d.c.is_deleted.is_(False),
                    ),
                )
                .where(
                    window_h.c.organisation_id == self.organisation_id,
                    window_h.c.is_deleted.is_(False),
                    window_h.c.entered_at >= from_date,
                )
            )
            if to_date is not None:
                window_deals_stmt = window_deals_stmt.where(window_h.c.entered_at <= to_date)
            if pipeline_id is not None:
                window_deals_stmt = window_deals_stmt.where(window_d.c.pipeline_id == pipeline_id)
            if min_deal_value is not None:
                window_deals_stmt = window_deals_stmt.where(window_d.c.amount >= min_deal_value)
            if max_deal_value is not None:
                window_deals_stmt = window_deals_stmt.where(window_d.c.amount <= max_deal_value)
            base_stmt = base_stmt.where(h_c.deal_id.in_(window_deals_stmt))

        history_rows = base_stmt.subquery()
        stmt = select(history_rows).order_by(
            history_rows.c.deal_id.asc(),
            history_rows.c.entered_at.asc(),
        )
        if from_date is not None:
            stmt = stmt.where(history_rows.c.entered_at >= from_date)

        result = await self.org_session.execute(stmt)
        history: list[StageMovementHistory] = []
        for row in result.all():
            to_stage_label = (
                row.to_stage_label
                or row.to_stage_name
                or (str(row.stage_id) if row.stage_id else None)
            )
            from_stage_label = (
                row.from_stage_label
                or row.from_stage_name
                or (str(row.from_stage_id) if row.from_stage_id else None)
            )
            history.append(
                StageMovementHistory(
                    deal_id=row.deal_id,
                    deal_name=row.deal_name,
                    amount=float(row.deal_amount) if row.deal_amount is not None else None,
                    owner_name=row.owner_name,
                    from_stage=from_stage_label,
                    to_stage=to_stage_label,
                    entered_at=row.entered_at,
                    previous_entered_at=row.previous_entered_at,
                )
            )

        return history
