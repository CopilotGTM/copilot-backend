"""Shared helpers for Sales Dashboard widget builders."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from src.api.schemas.sales_dashboard_models import SalesDashboardWidget, WidgetEmptyState


def empty_widget(
    *,
    widget_id: str,
    widget_type: str,
    title: str,
    message: str,
) -> SalesDashboardWidget:
    return SalesDashboardWidget(
        widget_id=widget_id,
        widget_type=widget_type,
        title=title,
        data={},
        empty_state=WidgetEmptyState(reason="no_data", message=message),
        warnings=[],
    )


def serialize_analytics(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: serialize_analytics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_analytics(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return serialize_analytics(asdict(value))
    return value
