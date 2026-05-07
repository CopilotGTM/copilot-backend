"""Deal table widgets for the Sales Dashboard."""

from __future__ import annotations

from src.api.schemas.sales_dashboard_models import SalesDashboardFilterSet, SalesDashboardWidget


async def build_deal_table_widgets(
    dashboard_data,
    filters: SalesDashboardFilterSet,
) -> list[SalesDashboardWidget]:
    no_movement = await dashboard_data.get_no_movement_deals(
        from_date=filters.from_date,
        to_date=filters.to_date,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
        stale_threshold_days=30,
        limit=10,
    )
    slipped_overdue = await dashboard_data.get_slipped_overdue_deals(
        from_date=filters.from_date,
        to_date=filters.to_date,
        pipeline_ids=filters.pipeline_ids,
        owner_ids=filters.owner_ids,
        min_deal_value=filters.min_deal_value,
        max_deal_value=filters.max_deal_value,
        limit=10,
    )
    return [
        SalesDashboardWidget(
            widget_id="no_movement_deals",
            widget_type="table",
            title="No-Movement Deals",
            data={"items": no_movement},
        ),
        SalesDashboardWidget(
            widget_id="slipped_overdue_deals",
            widget_type="table",
            title="Slipped And Overdue Deals",
            data={"items": slipped_overdue},
            warnings=[
                "Uses available expected-close fields; detailed close-date history is not stored yet."
            ],
        ),
    ]
