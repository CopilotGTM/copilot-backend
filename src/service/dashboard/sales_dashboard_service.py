"""Sales Dashboard orchestration service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.contracts.dashboard.sales_dashboard import (
    SalesDashboardDateRange,
    SalesDashboardFilterSet,
    SalesDashboardPreferences,
    SalesDashboardQueryParams,
    SalesDashboardResponse,
    SalesDashboardSection,
    SalesDashboardWidget,
    UpdateSalesDashboardPreferencesRequest,
)
from src.domain.models.master_tables.user_models import User
from src.service.dashboard.data_provider import SalesDashboardDataProvider
from src.service.dashboard.defaults import (
    DEFAULT_SALES_DASHBOARD_DATE_RANGE,
    SALES_DASHBOARD_ID,
    SALES_DASHBOARD_TITLE,
    resolve_date_window,
)
from src.service.dashboard.preferences import (
    extract_sales_dashboard_preferences,
    merge_sales_dashboard_preferences,
)
from src.service.dashboard.widgets import (
    build_deal_table_widgets,
    build_diagnostic_widgets,
    build_segmentation_widgets,
    build_stage_movement_widgets,
    build_summary_card_widgets,
)


class SalesDashboardService:
    def __init__(
        self,
        *,
        org_session: AsyncSession | None,
        master_session: AsyncSession,
        organisation_id,
        user_id=None,
    ) -> None:
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    @staticmethod
    def resolve_filters(
        *,
        query: SalesDashboardQueryParams,
        saved_preferences: dict[str, Any],
        now: datetime | None = None,
    ) -> SalesDashboardFilterSet:
        current = now or datetime.now(UTC)
        filters = saved_preferences.get("filters") if isinstance(saved_preferences, dict) else {}
        filters = filters if isinstance(filters, dict) else {}

        raw_range = (
            query.date_range or filters.get("date_range") or DEFAULT_SALES_DASHBOARD_DATE_RANGE
        )
        raw_segment = query.primary_segment or filters.get("primary_segment")
        date_range = (
            raw_range
            if isinstance(raw_range, SalesDashboardDateRange)
            else SalesDashboardDateRange(str(raw_range))
        )

        from_date, to_date = resolve_date_window(date_range, now=current)
        return SalesDashboardFilterSet(
            date_range=date_range,
            primary_segment=raw_segment or query.primary_segment,
            from_date=query.from_date or from_date,
            to_date=query.to_date or to_date,
            min_deal_value=(
                query.min_deal_value
                if query.min_deal_value is not None
                else filters.get("min_deal_value")
            ),
            max_deal_value=(
                query.max_deal_value
                if query.max_deal_value is not None
                else filters.get("max_deal_value")
            ),
            pipeline_ids=query.pipeline_ids or filters.get("pipeline_ids", []),
            owner_ids=query.owner_ids or filters.get("owner_ids", []),
        )

    async def get_sales_dashboard(
        self,
        query: SalesDashboardQueryParams,
    ) -> SalesDashboardResponse:
        saved_preferences = await self._load_saved_preferences()
        filters = self.resolve_filters(query=query, saved_preferences=saved_preferences)
        sections = await self._build_dashboard_sections(
            filters,
            query.include_widgets,
            saved_preferences,
        )
        return SalesDashboardResponse(
            dashboard_id=SALES_DASHBOARD_ID,
            schema_version=1,
            title=SALES_DASHBOARD_TITLE,
            generated_at=filters.to_date,
            effective_filters=filters.model_dump(mode="json"),
            sections=sections,
            warnings=[],
        )

    async def get_preferences(self) -> SalesDashboardPreferences:
        return SalesDashboardPreferences.model_validate(await self._load_saved_preferences())

    async def update_preferences(
        self,
        request: UpdateSalesDashboardPreferencesRequest,
    ) -> SalesDashboardPreferences:
        user = await self._get_current_user_row()
        current_preferences = user.preferences if isinstance(user.preferences, dict) else {}
        sales_preferences = SalesDashboardPreferences.model_validate(
            request.model_dump(mode="json")
        )
        user.preferences = merge_sales_dashboard_preferences(
            current_preferences,
            sales_preferences.model_dump(mode="json"),
        )
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return sales_preferences

    async def _get_current_user_row(self) -> User:
        result = await self.master_session.execute(
            select(User).where(
                User.user_id == self.user_id,
                User.organisation_id == self.organisation_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return user

    async def _load_saved_preferences(self) -> dict[str, Any]:
        if self.user_id is None:
            return {}
        user = await self._get_current_user_row()
        return extract_sales_dashboard_preferences(user.preferences)

    @staticmethod
    def _section(
        section_id: str,
        title: str,
        widgets: list[SalesDashboardWidget],
    ) -> SalesDashboardSection:
        return SalesDashboardSection(section_id=section_id, title=title, widgets=widgets)

    @classmethod
    def _empty_dashboard_sections(cls) -> list[SalesDashboardSection]:
        return [
            cls._section("summary", "Summary", []),
            cls._section("segmentation", "Segmentation", []),
            cls._section("diagnostics", "Diagnostics", []),
            cls._section("movement", "Stage Movement", []),
            cls._section("deal_tables", "Deal Tables", []),
        ]

    @staticmethod
    def _filter_widgets(
        widgets: list[SalesDashboardWidget],
        requested: set[str],
    ) -> list[SalesDashboardWidget]:
        if not requested:
            return widgets
        return [widget for widget in widgets if widget.widget_id in requested]

    @staticmethod
    def _should_build_section(section_widget_ids: set[str], requested: set[str]) -> bool:
        return not requested or bool(section_widget_ids.intersection(requested))

    async def _build_dashboard_sections(
        self,
        filters: SalesDashboardFilterSet,
        include_widgets: list[str],
        saved_preferences: dict[str, Any],
    ) -> list[SalesDashboardSection]:
        if self.org_session is None:
            raise RuntimeError("org_session is required to build dashboard widgets")

        requested = set(include_widgets or [])
        dashboard_data = SalesDashboardDataProvider(self.org_session, self.organisation_id)
        summary_widgets = []
        if self._should_build_section(
            {"total_pipeline", "active_deals", "extended_deals", "average_deal_age"},
            requested,
        ):
            summary_widgets = self._filter_widgets(
                await build_summary_card_widgets(dashboard_data, filters),
                requested,
            )

        segmentation_widgets = []
        if self._should_build_section(
            {"primary_distribution", "average_sales_cycle_by_value_bucket"},
            requested,
        ):
            segmentation_widgets = self._filter_widgets(
                await build_segmentation_widgets(dashboard_data, filters, saved_preferences),
                requested,
            )

        diagnostic_widgets = []
        if self._should_build_section(
            {"stuck_stages", "extension_frequency_by_stage", "owner_slippage"},
            requested,
        ):
            diagnostic_widgets = self._filter_widgets(
                await build_diagnostic_widgets(dashboard_data, filters),
                requested,
            )

        movement_widgets = []
        if self._should_build_section({"stage_movement_flow"}, requested):
            movement_widgets = self._filter_widgets(
                await build_stage_movement_widgets(dashboard_data, filters),
                requested,
            )

        deal_table_widgets = []
        if self._should_build_section({"no_movement_deals", "slipped_overdue_deals"}, requested):
            deal_table_widgets = self._filter_widgets(
                await build_deal_table_widgets(dashboard_data, filters),
                requested,
            )

        return [
            self._section("summary", "Summary", summary_widgets),
            self._section("segmentation", "Segmentation", segmentation_widgets),
            self._section("diagnostics", "Diagnostics", diagnostic_widgets),
            self._section("movement", "Stage Movement", movement_widgets),
            self._section("deal_tables", "Deal Tables", deal_table_widgets),
        ]
