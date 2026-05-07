"""Helpers for user-level Sales Dashboard preferences."""

from __future__ import annotations

from typing import Any

SALES_DASHBOARD_PREFERENCES_PATH = ("dashboard", "sales")


def extract_sales_dashboard_preferences(preferences: dict[str, Any] | None) -> dict[str, Any]:
    root = preferences if isinstance(preferences, dict) else {}
    dashboard = root.get("dashboard")
    if not isinstance(dashboard, dict):
        return {}

    sales = dashboard.get("sales")
    return sales if isinstance(sales, dict) else {}


def merge_sales_dashboard_preferences(
    preferences: dict[str, Any] | None,
    sales_preferences: dict[str, Any],
) -> dict[str, Any]:
    root = dict(preferences or {})
    dashboard = dict(root.get("dashboard") or {})
    dashboard["sales"] = dict(sales_preferences)
    root["dashboard"] = dashboard
    return root
