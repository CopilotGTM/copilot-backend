"""Diagnostic widgets for the Sales Dashboard."""

from __future__ import annotations

from src.api.schemas.sales_dashboard_models import SalesDashboardFilterSet, SalesDashboardWidget
from src.service.dashboard.widgets.base import serialize_analytics


async def build_diagnostic_widgets(
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
    movement_data = serialize_analytics(analytics)
    extension_by_stage = await dashboard_data.get_extension_frequency_by_stage(
        from_date=filters.from_date,
        to_date=filters.to_date,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
    )
    owner_slippage = await dashboard_data.get_owner_slippage(
        from_date=filters.from_date,
        to_date=filters.to_date,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
    )

    stuck_stages = movement_data["stuck_stages"]
    stale_deals = sum(int(item.get("stuck_deals") or 0) for item in stuck_stages)
    average_stage_age_days = None
    if stuck_stages:
        average_stage_age_days = round(
            sum(float(item.get("average_days_in_stage") or 0) for item in stuck_stages)
            / len(stuck_stages),
            2,
        )

    return [
        SalesDashboardWidget(
            widget_id="stuck_stages",
            widget_type="bar_chart",
            title="Stuck Stages",
            data={
                "summary": {
                    "stale_deals": stale_deals,
                    "average_stage_age_days": average_stage_age_days,
                },
                "items": [
                    {
                        "label": item.get("stage"),
                        "stuck_deals": item.get("stuck_deals"),
                        "average_days_in_stage": item.get("average_days_in_stage"),
                        "max_days_in_stage": item.get("max_days_in_stage"),
                        "value": item.get("total_value"),
                    }
                    for item in stuck_stages
                ],
            },
        ),
        SalesDashboardWidget(
            widget_id="extension_frequency_by_stage",
            widget_type="bar_chart",
            title="Extension Frequency By Stage",
            data={"items": extension_by_stage},
            warnings=[
                "Uses available expected-close extension counters; detailed close-date history is not stored yet."
            ],
        ),
        SalesDashboardWidget(
            widget_id="owner_slippage",
            widget_type="bar_chart",
            title="Owner Slippage",
            data={"items": owner_slippage},
            warnings=[
                "Uses available expected-close extension counters; detailed close-date history is not stored yet."
            ],
        ),
    ]
