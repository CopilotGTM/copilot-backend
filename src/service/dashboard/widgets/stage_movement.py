"""Stage movement widgets for the Sales Dashboard."""

from __future__ import annotations

from src.contracts.dashboard.sales_dashboard import SalesDashboardFilterSet, SalesDashboardWidget
from src.service.dashboard.widgets.base import serialize_analytics


async def build_stage_movement_widgets(
    dashboard_data,
    filters: SalesDashboardFilterSet,
) -> list[SalesDashboardWidget]:
    analytics = await dashboard_data.get_stage_movement_analytics(
        from_date=filters.from_date,
        to_date=filters.to_date,
        pipeline_id=filters.pipeline_ids[0] if len(filters.pipeline_ids) == 1 else None,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
        stale_threshold_days=30,
        recent_limit=10,
    )
    data = serialize_analytics(analytics)

    return [
        SalesDashboardWidget(
            widget_id="stage_movement_flow",
            widget_type="stage_movement",
            title="Stage Movement Flow",
            data={
                "selected_pipeline_id": str(filters.pipeline_ids[0])
                if len(filters.pipeline_ids) == 1
                else None,
                "pipeline_stages": [],
                "summary": data["summary"],
                "stage_transitions": data["stage_transitions"],
                "qualified_progression": data["qualified_progression"],
            },
        )
    ]
