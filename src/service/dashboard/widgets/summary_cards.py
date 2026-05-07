"""Summary card widgets for the Sales Dashboard."""

from __future__ import annotations

from src.api.schemas.sales_dashboard_models import SalesDashboardFilterSet, SalesDashboardWidget


async def build_summary_card_widgets(
    dashboard_data,
    filters: SalesDashboardFilterSet,
) -> list[SalesDashboardWidget]:
    data = await dashboard_data.get_summary_cards(
        from_date=filters.from_date,
        to_date=filters.to_date,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
    )
    return [
        SalesDashboardWidget(
            widget_id="total_pipeline",
            widget_type="metric",
            title="Total Pipeline",
            data={
                "total_pipeline_value": data["total_pipeline_value"],
                "total_deals": data["total_deals"],
            },
        ),
        SalesDashboardWidget(
            widget_id="active_deals",
            widget_type="metric",
            title="Active Deals",
            data={
                "active_deals": data["active_deals"],
                "overdue_open_deals": data["overdue_open_deals"],
            },
        ),
        SalesDashboardWidget(
            widget_id="extended_deals",
            widget_type="metric",
            title="Extended Deals",
            data={"extended_deals": data["extended_deals"]},
            warnings=[
                "Uses available expected-close extension counters; detailed close-date history is not stored yet."
            ],
        ),
        SalesDashboardWidget(
            widget_id="average_deal_age",
            widget_type="metric",
            title="Average Deal Age",
            data={"average_deal_age_days": data["average_deal_age_days"]},
        ),
    ]
