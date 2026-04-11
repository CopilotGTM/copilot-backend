from datetime import UTC, datetime

import pytest
from src.workers.integrations_worker.providers.messaging.slack import (
    SLACK_MESSAGES_INCREMENTAL_BUFFER_MINUTES,
    SLACK_MESSAGES_INITIAL_LOOKBACK_DAYS,
    _slack_oldest_timestamp,
    build_slack_channel_external_id,
    fetch_thread_replies_for_known_threads,
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


@pytest.mark.asyncio
async def test_fetch_thread_replies_for_known_threads_merges_and_sorts(monkeypatch) -> None:
    async def fake_call_with_token_retry(token_info, request_fn):
        return await request_fn("xoxp-test")

    async def fake_fetch_workspace_context(token: str):
        return ("T123", "Workspace", {})

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_fetch_thread_replies(
        client,
        *,
        raw_channel_id: str,
        external_channel_id: str,
        parent_message: dict[str, str],
        oldest_ts: str,
        user_map: dict[str, dict],
    ) -> list[dict]:
        assert raw_channel_id == "C456"
        assert external_channel_id == "T123:C456"
        if parent_message["ts"] == "100.000001":
            return [
                {"ts": "300.000001", "client_msg_id": "r-2"},
                {"ts": "200.000001", "client_msg_id": "r-1"},
            ]
        return [{"ts": "200.000001", "client_msg_id": "r-1"}]

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.messaging.slack.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.messaging.slack._fetch_workspace_context",
        fake_fetch_workspace_context,
    )
    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.messaging.slack.httpx.AsyncClient",
        FakeAsyncClient,
    )
    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.messaging.slack._fetch_thread_replies",
        fake_fetch_thread_replies,
    )

    result = await fetch_thread_replies_for_known_threads(
        token_info=None,  # type: ignore[arg-type]
        connection_id="conn_123",
        channel_id="T123:C456",
        thread_oldest_map={"100.000001": "100.000001", "150.000001": "150.000001"},
    )

    assert [message["client_msg_id"] for message in result.messages] == ["r-1", "r-2"]
    assert result.delta_link == "300.000001"
