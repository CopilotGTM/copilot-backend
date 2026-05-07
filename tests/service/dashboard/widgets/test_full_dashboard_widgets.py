import asyncio
from datetime import UTC, datetime

from src.api.schemas.sales_dashboard_models import SalesDashboardDateRange, SalesDashboardFilterSet
from src.service.dashboard.widgets import (
    build_deal_table_widgets,
    build_diagnostic_widgets,
    build_segmentation_widgets,
    build_summary_card_widgets,
)


class FakeDashboardData:
    async def get_summary_cards(self, **kwargs):
        return {
            "total_pipeline_value": 100000.0,
            "total_deals": 4,
            "active_deals": 3,
            "overdue_open_deals": 1,
            "extended_deals": 2,
            "average_deal_age_days": 42,
        }

    async def get_primary_distribution(self, **kwargs):
        return [{"label": "Proposal", "count": 2, "value": 50000.0}]

    async def get_sales_cycle_by_value_bucket(self, **kwargs):
        return [{"label": "Small", "count": 1, "value": 20000.0, "average_age_days": 30}]

    async def get_stage_movement_analytics(self, **kwargs):
        return {
            "summary": {
                "moved_deals": 2,
                "moved_from_qualified": 1,
                "average_transition_days": 5,
                "stuck_deals": 1,
                "stale_threshold_days": 30,
            },
            "qualified_progression": [],
            "stage_transitions": [],
            "stuck_stages": [
                {
                    "stage": "Proposal",
                    "stuck_deals": 1,
                    "average_days_in_stage": 45,
                    "max_days_in_stage": 45,
                    "total_value": 50000.0,
                }
            ],
            "recent_movements": [],
        }

    async def get_extension_frequency_by_stage(self, **kwargs):
        return [{"label": "Proposal", "total_deals": 4, "extended_deals": 2}]

    async def get_owner_slippage(self, **kwargs):
        return [{"label": "Nikhil", "total_deals": 4, "extended_deals": 2}]

    async def get_no_movement_deals(self, **kwargs):
        return [{"deal_id": "deal-1", "name": "Acme", "days_in_stage": 45}]

    async def get_slipped_overdue_deals(self, **kwargs):
        return [{"deal_id": "deal-2", "name": "Globex", "status": "Slipped"}]


def _filters() -> SalesDashboardFilterSet:
    return SalesDashboardFilterSet(
        date_range=SalesDashboardDateRange.LAST_90_DAYS,
        from_date=datetime(2026, 2, 5, tzinfo=UTC),
        to_date=datetime(2026, 5, 6, tzinfo=UTC),
    )


def test_full_dashboard_widget_builders_return_expected_widget_ids() -> None:
    data_provider = FakeDashboardData()

    widgets = []
    widgets.extend(asyncio.run(build_summary_card_widgets(data_provider, _filters())))
    widgets.extend(asyncio.run(build_segmentation_widgets(data_provider, _filters(), {})))
    widgets.extend(asyncio.run(build_diagnostic_widgets(data_provider, _filters())))
    widgets.extend(asyncio.run(build_deal_table_widgets(data_provider, _filters())))

    assert [widget.widget_id for widget in widgets] == [
        "total_pipeline",
        "active_deals",
        "extended_deals",
        "average_deal_age",
        "primary_distribution",
        "average_sales_cycle_by_value_bucket",
        "stuck_stages",
        "extension_frequency_by_stage",
        "owner_slippage",
        "no_movement_deals",
        "slipped_overdue_deals",
    ]
    assert widgets[0].data["total_pipeline_value"] == 100000.0
