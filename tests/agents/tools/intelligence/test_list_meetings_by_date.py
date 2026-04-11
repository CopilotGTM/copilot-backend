from __future__ import annotations

from datetime import UTC, date, datetime

from src.agents.tools.intelligence.list_meetings_by_date import _get_date_range


def test_get_date_range_today_uses_user_local_day_boundaries() -> None:
    anchor_utc = datetime(2026, 4, 10, 20, 30, tzinfo=UTC)

    start_dt, end_dt = _get_date_range(
        "today",
        7,
        None,
        None,
        "Asia/Kolkata",
        anchor_utc,
    )

    assert start_dt == datetime(2026, 4, 10, 18, 30, tzinfo=UTC)
    assert end_dt == datetime(2026, 4, 11, 18, 30, tzinfo=UTC)


def test_get_date_range_custom_interprets_dates_in_user_timezone() -> None:
    anchor_utc = datetime(2026, 4, 10, 20, 30, tzinfo=UTC)

    start_dt, end_dt = _get_date_range(
        "custom",
        7,
        date(2026, 4, 11),
        date(2026, 4, 11),
        "America/Los_Angeles",
        anchor_utc,
    )

    assert start_dt == datetime(2026, 4, 11, 7, 0, tzinfo=UTC)
    assert end_dt == datetime(2026, 4, 12, 6, 59, 59, 999999, tzinfo=UTC)
