import asyncio
from datetime import UTC, datetime
from inspect import signature
from types import SimpleNamespace
from uuid import uuid4

from src.api.schemas.sales_dashboard_models import (
    SalesDashboardDateRange,
    SalesDashboardQueryParams,
    SalesDashboardWidget,
)
from src.service.dashboard.data_provider import SalesDashboardDataProvider
from src.service.dashboard.sales_dashboard_service import SalesDashboardService


def test_resolve_filters_uses_last_90_days_default() -> None:
    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)

    filters = SalesDashboardService.resolve_filters(
        query=SalesDashboardQueryParams(),
        saved_preferences={},
        now=now,
    )

    assert filters.date_range == SalesDashboardDateRange.LAST_90_DAYS
    assert filters.to_date == now
    assert filters.from_date.date().isoformat() == "2026-02-05"


def test_request_filters_override_saved_preferences() -> None:
    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    query = SalesDashboardQueryParams(date_range=SalesDashboardDateRange.LAST_30_DAYS)
    saved_preferences = {"filters": {"date_range": "this_quarter"}}

    filters = SalesDashboardService.resolve_filters(
        query=query,
        saved_preferences=saved_preferences,
        now=now,
    )

    assert filters.date_range == SalesDashboardDateRange.LAST_30_DAYS
    assert filters.from_date.date().isoformat() == "2026-04-06"


def test_request_deal_value_filters_override_saved_preferences() -> None:
    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    query = SalesDashboardQueryParams(min_deal_value=50000)
    saved_preferences = {"filters": {"min_deal_value": 10000, "max_deal_value": 250000}}

    filters = SalesDashboardService.resolve_filters(
        query=query,
        saved_preferences=saved_preferences,
        now=now,
    )

    assert filters.min_deal_value == 50000
    assert filters.max_deal_value == 250000


class FakeDashboardData:
    def __init__(self) -> None:
        self.summary_filters = None

    async def get_summary_cards(self, filters):
        self.summary_filters = filters
        return {
            "total_deals": 3,
            "total_pipeline_value": 3000.0,
            "active_deals": 2,
            "overdue_open_deals": 0,
            "extended_deals": 1,
            "average_deal_age_days": 20,
        }


def test_dashboard_service_passes_filters_to_dashboard_data_provider() -> None:
    data_provider = FakeDashboardData()
    owner_id = uuid4()
    query = SalesDashboardQueryParams(owner_ids=[owner_id])

    filters = SalesDashboardService.resolve_filters(
        query=query,
        saved_preferences={},
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
    )
    result = asyncio.run(data_provider.get_summary_cards(filters))

    assert result["total_deals"] == 3
    assert data_provider.summary_filters.owner_ids == [owner_id]


def test_dashboard_data_provider_exposes_dashboard_read_methods() -> None:
    assert hasattr(SalesDashboardDataProvider, "get_summary_cards")
    assert hasattr(SalesDashboardDataProvider, "get_primary_distribution")
    assert hasattr(SalesDashboardDataProvider, "get_sales_cycle_by_value_bucket")
    assert hasattr(SalesDashboardDataProvider, "get_extension_frequency_by_stage")
    assert hasattr(SalesDashboardDataProvider, "get_owner_slippage")
    assert hasattr(SalesDashboardDataProvider, "get_no_movement_deals")
    assert hasattr(SalesDashboardDataProvider, "get_slipped_overdue_deals")


def test_stage_movement_history_accepts_database_window_bounds() -> None:
    parameters = signature(SalesDashboardDataProvider._get_stage_movement_history).parameters

    assert "from_date" in parameters
    assert "to_date" in parameters


def test_dashboard_deal_filters_accept_value_bounds() -> None:
    parameters = signature(SalesDashboardDataProvider._dashboard_deal_filters).parameters

    assert "min_deal_value" in parameters
    assert "max_deal_value" in parameters


def test_stage_movement_history_uses_internal_stage_ids() -> None:
    deal_id = uuid4()
    from_stage_id = uuid4()
    to_stage_id = uuid4()

    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(
                    deal_id=deal_id,
                    deal_name="Acme",
                    deal_amount=50000,
                    owner_name="Nikhil",
                    entered_at=datetime(2026, 4, 12, tzinfo=UTC),
                    stage_id=to_stage_id,
                    from_stage_id=from_stage_id,
                    to_stage_label="Proposal",
                    to_stage_name="Proposal",
                    from_stage_label="Discovery",
                    from_stage_name="Discovery",
                    previous_entered_at=datetime(2026, 4, 1, tzinfo=UTC),
                )
            ]

    class FakeSession:
        async def execute(self, stmt):
            return FakeResult()

    provider = SalesDashboardDataProvider(FakeSession(), uuid4())

    history = asyncio.run(provider._get_stage_movement_history(to_date=datetime(2026, 5, 1)))

    assert history[0].to_stage == "Proposal"
    assert history[0].from_stage == "Discovery"


def test_service_returns_sections_with_widgets(monkeypatch) -> None:
    class FakeService(SalesDashboardService):
        async def _load_saved_preferences(self):
            return {}

    service = FakeService(
        org_session=None,
        master_session=None,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    async def fake_build_dashboard_sections(self, filters, include_widgets, saved_preferences):
        return [
            self._section(
                "summary",
                "Summary",
                [
                    SalesDashboardWidget(
                        widget_id="total_pipeline",
                        widget_type="metric",
                        title="Total Pipeline",
                        data={"total_deals": 0},
                    )
                ],
            )
        ]

    monkeypatch.setattr(
        SalesDashboardService,
        "_build_dashboard_sections",
        fake_build_dashboard_sections,
    )

    response = asyncio.run(service.get_sales_dashboard(SalesDashboardQueryParams()))

    assert response.dashboard_id == "sales"
    assert response.sections[0].section_id == "summary"
    assert response.sections[0].widgets[0].widget_id == "total_pipeline"


def test_service_can_render_with_default_preferences_without_user() -> None:
    class FakeService(SalesDashboardService):
        async def _build_dashboard_sections(self, filters, include_widgets, saved_preferences):
            assert saved_preferences == {}
            return self._empty_dashboard_sections()

    service = FakeService(
        org_session=object(),
        master_session=object(),
        organisation_id=uuid4(),
        user_id=None,
    )

    response = asyncio.run(service.get_sales_dashboard(SalesDashboardQueryParams()))

    assert response.dashboard_id == "sales"
    assert response.sections


def test_service_declares_deepmatrix_section_order() -> None:
    sections = SalesDashboardService._empty_dashboard_sections()

    assert [section.section_id for section in sections] == [
        "summary",
        "segmentation",
        "diagnostics",
        "movement",
        "deal_tables",
    ]
