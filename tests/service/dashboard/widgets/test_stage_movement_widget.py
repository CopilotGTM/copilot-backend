import asyncio
from datetime import UTC, datetime

from src.api.schemas.sales_dashboard_models import (
    SalesDashboardDateRange,
    SalesDashboardFilterSet,
)
from src.service.dashboard.widgets.stage_movement import build_stage_movement_widgets


class FakeDealsAccess:
    async def get_stage_movement_analytics(self, **kwargs):
        return {
            "summary": {
                "moved_deals": 2,
                "moved_from_qualified": 1,
                "average_transition_days": 4.5,
                "stuck_deals": 1,
                "stale_threshold_days": 30,
            },
            "qualified_progression": [],
            "stage_transitions": [],
            "stuck_stages": [{"stage": "Proposal", "stuck_deals": 1}],
            "recent_movements": [{"deal_name": "Acme", "to_stage": "Proposal"}],
        }


def test_stage_movement_builder_returns_flow_widget() -> None:
    filters = SalesDashboardFilterSet(
        date_range=SalesDashboardDateRange.LAST_90_DAYS,
        from_date=datetime(2026, 2, 5, tzinfo=UTC),
        to_date=datetime(2026, 5, 6, tzinfo=UTC),
    )

    widgets = asyncio.run(build_stage_movement_widgets(FakeDealsAccess(), filters))

    assert [widget.widget_id for widget in widgets] == ["stage_movement_flow"]
    assert widgets[0].data["summary"]["moved_deals"] == 2
