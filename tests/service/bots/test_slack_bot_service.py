import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from src.service.bots.slack_bot_service import SlackBotService


def test_message_dedupe_is_scoped_to_slack_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matching Slack message IDs in separate workspaces are distinct events."""
    redis = Mock()
    redis.set.return_value = True
    monkeypatch.setattr(
        "src.infra.messaging.redis_client.get_redis_client",
        lambda: redis,
    )
    bot = SlackBotService()
    event = {"client_msg_id": "shared-message-id"}

    assert bot._is_duplicate_event(event, "T1") is False
    assert bot._is_duplicate_event(event, "T2") is False

    assert [call.args[0] for call in redis.set.call_args_list] == [
        "slack:msg:T1:shared-message-id",
        "slack:msg:T2:shared-message-id",
    ]


def test_fallback_text_truncates_long_strings() -> None:
    text = "A" * 4000

    truncated = SlackBotService._fallback_text(text, limit=3000)

    assert len(truncated) == 3000
    assert truncated.endswith("…")


def test_fallback_text_preserves_short_strings() -> None:
    text = "short text"

    assert SlackBotService._fallback_text(text, limit=3000) == text


def test_is_slack_audio_recording_uses_subtype_not_mimetype() -> None:
    assert SlackBotService._is_slack_audio_recording({"subtype": "slack_audio"}) is True
    assert SlackBotService._is_slack_audio_recording({"mimetype": "audio/mp4"}) is False
    assert SlackBotService._is_slack_audio_recording({"subtype": "file_share"}) is False


def test_authenticated_message_forwards_slack_audio_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SlackBotService()
    user = SimpleNamespace(organisation_id="org-1", user_id="user-1", email="test@example.com")
    captured: dict[str, object] = {}

    async def fake_create_bot_job(**kwargs):
        captured["job_message_text"] = kwargs["message_text"]
        return "job-1"

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["task_name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["queue"] = queue
        return "task-1"

    monkeypatch.setattr(bot, "_create_bot_job", fake_create_bot_job)
    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    result = asyncio.run(
        bot._handle_authenticated_message(
            text="",
            user=user,
            slack_team_id="T1",
            channel_id="C1",
            conversation_type="personal",
            external_user_id="U1",
            external_tenant_id="T1",
            slack_files=[
                {
                    "subtype": "slack_audio",
                    "name": "voice.m4a",
                    "mimetype": "audio/mp4",
                    "size": 123,
                    "url_private_download": "https://files.slack.test/voice.m4a",
                }
            ],
        )
    )

    assert result is None
    assert captured["job_message_text"] == ""
    assert captured["args"][2] == ""
    assert captured["kwargs"]["bot_context"]["slack_audio_files"] == [
        {
            "name": "voice.m4a",
            "mimetype": "audio/mp4",
            "size": 123,
            "subtype": "slack_audio",
            "url_private_download": "https://files.slack.test/voice.m4a",
        }
    ]
