from __future__ import annotations

from datetime import UTC, datetime

from src.infra.utils.agent_datetime import parse_agent_datetime


def test_parse_agent_datetime_uses_same_local_weekday_when_time_is_future() -> None:
    parsed = parse_agent_datetime(
        "Saturday 10 PM",
        timezone_name="Asia/Kolkata",
        metadata={"current_datetime": "2026-04-11T12:00:00+00:00"},
    )

    assert parsed == datetime(2026, 4, 11, 16, 30, tzinfo=UTC)


def test_parse_agent_datetime_uses_next_weekday_when_local_time_has_passed() -> None:
    parsed = parse_agent_datetime(
        "Saturday 10 PM",
        timezone_name="Asia/Kolkata",
        metadata={"current_datetime": "2026-04-11T18:00:00+00:00"},
    )

    assert parsed == datetime(2026, 4, 18, 16, 30, tzinfo=UTC)


def test_parse_agent_datetime_keeps_utc_boundary_on_user_local_day() -> None:
    parsed = parse_agent_datetime(
        "Saturday 10 AM",
        timezone_name="Asia/Kolkata",
        metadata={"current_datetime": "2026-04-10T20:30:00+00:00"},
    )

    assert parsed == datetime(2026, 4, 11, 4, 30, tzinfo=UTC)


def test_parse_agent_datetime_treats_naive_iso_as_user_local() -> None:
    parsed = parse_agent_datetime(
        "2026-04-11T10:00:00",
        timezone_name="Asia/Kolkata",
        metadata={"current_datetime": "2026-04-10T20:30:00+00:00"},
    )

    assert parsed == datetime(2026, 4, 11, 4, 30, tzinfo=UTC)


def test_parse_agent_datetime_preserves_timezone_aware_iso() -> None:
    parsed = parse_agent_datetime(
        "2026-04-11T10:00:00-07:00",
        timezone_name="Asia/Kolkata",
        metadata={"current_datetime": "2026-04-10T20:30:00+00:00"},
    )

    assert parsed == datetime(2026, 4, 11, 17, 0, tzinfo=UTC)


def test_parse_agent_datetime_can_require_valid_timezone_for_natural_language() -> None:
    parsed = parse_agent_datetime(
        "Saturday 10 AM",
        timezone_name=None,
        metadata={"current_datetime": "2026-04-10T20:30:00+00:00"},
        require_timezone_for_natural_language=True,
    )

    assert parsed is None
