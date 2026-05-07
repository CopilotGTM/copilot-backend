from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from src.api.schemas.sales_dashboard_models import (
    SalesDashboardDateRange,
    SalesDashboardPrimarySegment,
    SalesDashboardQueryParams,
    SalesDashboardResponse,
    SalesDashboardSection,
    SalesDashboardWidget,
    WidgetEmptyState,
)
from src.service.dashboard.defaults import DEFAULT_SALES_DASHBOARD_WIDGETS


def test_query_params_reject_from_date_after_to_date() -> None:
    with pytest.raises(ValidationError):
        SalesDashboardQueryParams(
            from_date=datetime(2026, 5, 6, tzinfo=UTC),
            to_date=datetime(2026, 5, 1, tzinfo=UTC),
        )


def test_query_params_accept_filter_lists() -> None:
    pipeline_id = uuid4()
    owner_id = uuid4()

    params = SalesDashboardQueryParams(
        date_range=SalesDashboardDateRange.LAST_90_DAYS,
        primary_segment=SalesDashboardPrimarySegment.OWNER,
        pipeline_ids=[pipeline_id],
        owner_ids=[owner_id],
        include_widgets=["total_pipeline", "stage_movement_flow"],
    )

    assert params.date_range == SalesDashboardDateRange.LAST_90_DAYS
    assert params.primary_segment == SalesDashboardPrimarySegment.OWNER
    assert params.pipeline_ids == [pipeline_id]
    assert params.owner_ids == [owner_id]
    assert params.include_widgets == ["total_pipeline", "stage_movement_flow"]


def test_query_params_accept_deal_value_bounds() -> None:
    params = SalesDashboardQueryParams(min_deal_value=50000, max_deal_value=250000)

    assert params.min_deal_value == 50000
    assert params.max_deal_value == 250000


def test_query_params_reject_min_deal_value_above_max() -> None:
    with pytest.raises(ValidationError):
        SalesDashboardQueryParams(min_deal_value=250000, max_deal_value=50000)


def test_default_widget_list_matches_deepmatrix_contract() -> None:
    assert DEFAULT_SALES_DASHBOARD_WIDGETS == [
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


def test_dashboard_response_has_stable_widget_shape() -> None:
    response = SalesDashboardResponse(
        dashboard_id="sales",
        schema_version=1,
        title="Sales Dashboard",
        generated_at=datetime(2026, 5, 6, tzinfo=UTC),
        effective_filters={
            "date_range": "last_90_days",
            "from_date": "2026-02-05T00:00:00Z",
            "to_date": "2026-05-06T00:00:00Z",
            "pipeline_ids": [],
            "owner_ids": [],
        },
        sections=[
            SalesDashboardSection(
                section_id="pipeline_health",
                title="Pipeline Health",
                widgets=[
                    SalesDashboardWidget(
                        widget_id="deal_summary",
                        widget_type="metric_group",
                        title="Deal Summary",
                        data={"total_deals": 0},
                        empty_state=WidgetEmptyState(
                            reason="no_data",
                            message="No CRM deals found for the selected filters.",
                        ),
                        warnings=[],
                    )
                ],
            )
        ],
        warnings=[],
    )

    assert response.dashboard_id == "sales"
    assert response.sections[0].widgets[0].widget_id == "deal_summary"
