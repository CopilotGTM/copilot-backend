from datetime import UTC, datetime

from src.workers.integrations_worker.providers.messaging.slack import (
    SLACK_MESSAGES_INCREMENTAL_BUFFER_MINUTES,
    SLACK_MESSAGES_INITIAL_LOOKBACK_DAYS,
    _slack_oldest_timestamp,
    build_slack_channel_external_id,
    split_slack_channel_external_id,
)


def test_build_and_split_slack_channel_external_id() -> None:
    external_id = build_slack_channel_external_id("T123", "C456")

    assert external_id == "T123:C456"
    assert split_slack_channel_external_id(external_id) == ("T123", "C456")


def test_split_slack_channel_external_id_without_workspace_prefix() -> None:
    assert split_slack_channel_external_id("C456") == (None, "C456")


def test_slack_oldest_timestamp_for_initial_sync() -> None:
    now = datetime.now(UTC)

    oldest = float(_slack_oldest_timestamp(is_initial=True, last_synced_at=None))

    assert oldest < now.timestamp()
    assert oldest >= now.timestamp() - (SLACK_MESSAGES_INITIAL_LOOKBACK_DAYS + 1) * 86400


def test_slack_oldest_timestamp_for_incremental_sync() -> None:
    last_synced_at = datetime.now(UTC)

    oldest = float(_slack_oldest_timestamp(is_initial=False, last_synced_at=last_synced_at))

    expected_upper_bound = last_synced_at.timestamp()
    expected_lower_bound = (
        last_synced_at.timestamp() - (SLACK_MESSAGES_INCREMENTAL_BUFFER_MINUTES + 1) * 60
    )
    assert expected_lower_bound <= oldest <= expected_upper_bound
