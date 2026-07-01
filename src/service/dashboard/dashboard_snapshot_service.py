from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.contracts.dashboard.sales_dashboard import (
    SalesDashboardPrimarySegment,
    SalesDashboardQueryParams,
    SalesDashboardResponse,
)
from src.domain.models.organisation_tables.dashboard_models import DashboardSnapshot
from src.service.dashboard.defaults import (
    DEFAULT_SALES_DASHBOARD_DATE_RANGE,
    DEFAULT_SALES_DASHBOARD_WIDGETS,
    SALES_DASHBOARD_ID,
    SALES_DASHBOARD_TITLE,
)
from src.service.dashboard.sales_dashboard_service import SalesDashboardService


def _is_default_sales_dashboard_query(query: SalesDashboardQueryParams) -> bool:
    return (
        query.date_range is None
        and query.primary_segment == SalesDashboardPrimarySegment.STAGE
        and query.from_date is None
        and query.to_date is None
        and query.min_deal_value is None
        and query.max_deal_value is None
        and not query.pipeline_ids
        and not query.owner_ids
        and not query.include_widgets
    )


class DashboardSnapshotService:
    def __init__(
        self,
        *,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
    ) -> None:
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id

    async def regenerate_sales_snapshot(
        self,
        *,
        user_id: UUID | None = None,
        query: SalesDashboardQueryParams | None = None,
    ) -> DashboardSnapshot:
        snapshot = await self._get_or_create_sales_snapshot(created_by_user_id=user_id)
        try:
            response = await SalesDashboardService(
                org_session=self.org_session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                user_id=user_id,
            ).get_sales_dashboard(query or SalesDashboardQueryParams())
            snapshot.latest_data = response.model_dump(mode="json")
            snapshot.latest_data_generated_at = response.generated_at
            snapshot.latest_generation_status = "success"
            snapshot.latest_generation_error = None
            await self.org_session.commit()
            await self.org_session.refresh(snapshot)
            return snapshot
        except Exception as exc:
            snapshot.latest_generation_status = "failed"
            snapshot.latest_generation_error = str(exc)
            await self.org_session.commit()
            raise

    async def get_sales_dashboard(
        self,
        query: SalesDashboardQueryParams,
        *,
        user_id: UUID,
        now: datetime | None = None,
    ):
        if not _is_default_sales_dashboard_query(query):
            return await self._render_sales_dashboard(query=query, user_id=user_id)

        snapshot = await self._get_sales_snapshot()
        if self._has_fresh_successful_data(snapshot, now=now):
            return SalesDashboardResponse.model_validate(snapshot.latest_data)

        regenerated = await self.regenerate_sales_snapshot(user_id=user_id, query=query)
        return SalesDashboardResponse.model_validate(regenerated.latest_data)

    async def get_latest_sales_snapshot_data(self) -> dict[str, Any] | None:
        snapshot = await self._get_sales_snapshot()
        if snapshot is None or snapshot.latest_generation_status != "success":
            return None
        return snapshot.latest_data

    async def _get_sales_snapshot(self) -> DashboardSnapshot | None:
        result = await self.org_session.execute(
            select(DashboardSnapshot).where(
                DashboardSnapshot.organisation_id == self.organisation_id,
                DashboardSnapshot.dashboard_key == SALES_DASHBOARD_ID,
                DashboardSnapshot.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def _get_or_create_sales_snapshot(
        self,
        *,
        created_by_user_id: UUID | None,
    ) -> DashboardSnapshot:
        snapshot = await self._get_sales_snapshot()
        if snapshot is not None:
            return snapshot

        snapshot = DashboardSnapshot(
            organisation_id=self.organisation_id,
            dashboard_key=SALES_DASHBOARD_ID,
            dashboard_name=SALES_DASHBOARD_TITLE,
            dashboard_type="system",
            schema_version=1,
            definition={
                "enabled_widget_ids": DEFAULT_SALES_DASHBOARD_WIDGETS,
                "default_filters": {
                    "date_range": DEFAULT_SALES_DASHBOARD_DATE_RANGE.value,
                },
            },
            latest_generation_status="pending",
            created_by_user_id=created_by_user_id,
        )
        self.org_session.add(snapshot)
        return snapshot

    async def _render_sales_dashboard(
        self,
        *,
        query: SalesDashboardQueryParams,
        user_id: UUID | None,
    ):
        return await SalesDashboardService(
            org_session=self.org_session,
            master_session=self.master_session,
            organisation_id=self.organisation_id,
            user_id=user_id,
        ).get_sales_dashboard(query)

    @staticmethod
    def _has_fresh_successful_data(
        snapshot: DashboardSnapshot | None,
        *,
        now: datetime | None,
    ) -> bool:
        if (
            snapshot is None
            or snapshot.latest_generation_status != "success"
            or not snapshot.latest_data
            or snapshot.latest_data_generated_at is None
        ):
            return False

        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        generated_at = snapshot.latest_data_generated_at
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        return generated_at + timedelta(seconds=snapshot.refresh_interval_seconds) >= current
