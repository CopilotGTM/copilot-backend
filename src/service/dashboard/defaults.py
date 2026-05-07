"""Code-owned defaults for the application-provided Sales Dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.api.schemas.sales_dashboard_models import SalesDashboardDateRange

SALES_DASHBOARD_ID = "sales"
SALES_DASHBOARD_TITLE = "Sales Dashboard"
DEFAULT_SALES_DASHBOARD_DATE_RANGE = SalesDashboardDateRange.LAST_90_DAYS
DEFAULT_SALES_DASHBOARD_WIDGETS = [
    "total_pipeline",
    "active_deals",
    "extended_deals",
    "average_deal_age",
    "primary_distribution",
    "average_sales_cycle_by_value_bucket",
    "stuck_stages",
    "extension_frequency_by_stage",
    "owner_slippage",
    "stage_movement_flow",
    "no_movement_deals",
    "slipped_overdue_deals",
]
DEFAULT_SALES_CYCLE_VALUE_BUCKETS = [
    {"label": "Small", "min_value": 0, "max_value": 50000},
    {"label": "Mid-market", "min_value": 50000, "max_value": 250000},
    {"label": "Large", "min_value": 250000, "max_value": 1000000},
    {"label": "Enterprise", "min_value": 1000000, "max_value": None},
]


def resolve_date_window(
    date_range: SalesDashboardDateRange,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)

    if date_range == SalesDashboardDateRange.LAST_30_DAYS:
        return current - timedelta(days=30), current
    if date_range == SalesDashboardDateRange.THIS_QUARTER:
        quarter_start_month = ((current.month - 1) // 3) * 3 + 1
        return (
            current.replace(
                month=quarter_start_month,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ),
            current,
        )
    if date_range == SalesDashboardDateRange.YEAR_TO_DATE:
        return (
            current.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
            current,
        )
    return current - timedelta(days=90), current
