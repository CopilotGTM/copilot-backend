from datetime import UTC, datetime

from src.workers.integrations_worker.providers.messaging.teams import (
    TEAMS_CHATS_INITIAL_LOOKBACK_DAYS,
    TEAMS_CHATS_RECURRING_LOOKBACK_DAYS,
    _resolve_chats_lookback_days,
)


def test_resolve_chats_lookback_days_initial_sync() -> None:
    days, phase = _resolve_chats_lookback_days(
        is_initial=True,
        last_synced_at=None,
        max_age_days=None,
    )

    assert phase == "initial"
    assert days == TEAMS_CHATS_INITIAL_LOOKBACK_DAYS


def test_resolve_chats_lookback_days_recurring_sync() -> None:
    days, phase = _resolve_chats_lookback_days(
        is_initial=False,
        last_synced_at=datetime.now(UTC),
        max_age_days=None,
    )

    assert phase == "recurring"
    assert days == TEAMS_CHATS_RECURRING_LOOKBACK_DAYS


def test_resolve_chats_lookback_days_custom_override() -> None:
    days, phase = _resolve_chats_lookback_days(
        is_initial=False,
        last_synced_at=datetime.now(UTC),
        max_age_days=21,
    )

    assert phase == "custom"
    assert days == 21
