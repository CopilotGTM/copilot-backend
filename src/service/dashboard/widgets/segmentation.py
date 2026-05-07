"""Segmentation widgets for the Sales Dashboard."""

from __future__ import annotations

from src.api.schemas.sales_dashboard_models import SalesDashboardFilterSet, SalesDashboardWidget
from src.service.dashboard.defaults import DEFAULT_SALES_CYCLE_VALUE_BUCKETS


async def build_segmentation_widgets(
    dashboard_data,
    filters: SalesDashboardFilterSet,
    saved_preferences: dict,
) -> list[SalesDashboardWidget]:
    buckets = (
        saved_preferences.get("sales_cycle_value_buckets")
        if isinstance(saved_preferences, dict)
        else None
    )
    buckets = (
        buckets if isinstance(buckets, list) and buckets else DEFAULT_SALES_CYCLE_VALUE_BUCKETS
    )

    distribution = await dashboard_data.get_primary_distribution(
        from_date=filters.from_date,
        to_date=filters.to_date,
        segment=filters.primary_segment.value,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
    )
    sales_cycle = await dashboard_data.get_sales_cycle_by_value_bucket(
        from_date=filters.from_date,
        to_date=filters.to_date,
        buckets=buckets,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
    )

    return [
        SalesDashboardWidget(
            widget_id="primary_distribution",
            widget_type="bar_chart",
            title="Primary Distribution",
            data={
                "selected_segment": filters.primary_segment.value,
                "available_segments": ["stage", "pipeline", "status", "owner"],
                "items": distribution,
            },
        ),
        SalesDashboardWidget(
            widget_id="average_sales_cycle_by_value_bucket",
            widget_type="bar_chart",
            title="Average Sales Cycle By Value Bucket",
            data={"buckets": buckets, "items": sales_cycle},
        ),
    ]
