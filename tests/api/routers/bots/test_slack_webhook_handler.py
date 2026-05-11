import json
from unittest.mock import AsyncMock

import pytest
from src.service.bots.auth_validators.base import AuthValidationResult


def test_events_endpoint_accepts_form_encoded_interaction_payload(client, monkeypatch) -> None:
    from src.api.routers.bots import slack_webhook_handler

    monkeypatch.setattr(slack_webhook_handler.slack_config, "SLACK_ENABLED", True)

    validate_mock = AsyncMock(return_value=AuthValidationResult(is_valid=True))
    process_interaction_mock = AsyncMock(return_value=None)
    process_event_mock = AsyncMock(return_value=None)
    close_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService, "validate_request", validate_mock, raising=True
    )
    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService,
        "process_interaction",
        process_interaction_mock,
        raising=True,
    )
    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService, "process_event", process_event_mock, raising=True
    )
    monkeypatch.setattr(slack_webhook_handler.SlackBotService, "close", close_mock, raising=True)

    interaction_payload = {
        "type": "block_actions",
        "team": {"id": "T123"},
        "user": {"id": "U123"},
        "channel": {"id": "C123"},
        "actions": [{"action_id": "create_deal_submit", "value": "{}"}],
    }

    response = client.post(
        "/users/bots/slack/events",
        data={"payload": json.dumps(interaction_payload)},
        headers={
            "X-Slack-Request-Timestamp": "1700000000",
            "X-Slack-Signature": "v0=test",
        },
    )

    assert response.status_code == 200
    assert process_interaction_mock.await_count == 1
    assert process_event_mock.await_count == 0


def test_events_endpoint_still_processes_standard_json_events(client, monkeypatch) -> None:
    from src.api.routers.bots import slack_webhook_handler

    monkeypatch.setattr(slack_webhook_handler.slack_config, "SLACK_ENABLED", True)

    validate_mock = AsyncMock(return_value=AuthValidationResult(is_valid=True))
    process_interaction_mock = AsyncMock(return_value=None)
    process_event_mock = AsyncMock(return_value=None)
    close_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService, "validate_request", validate_mock, raising=True
    )
    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService,
        "process_interaction",
        process_interaction_mock,
        raising=True,
    )
    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService, "process_event", process_event_mock, raising=True
    )
    monkeypatch.setattr(slack_webhook_handler.SlackBotService, "close", close_mock, raising=True)

    response = client.post(
        "/users/bots/slack/events",
        json={
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "",
            "event": {"type": "app_mention", "user": "U123"},
        },
        headers={
            "X-Slack-Request-Timestamp": "1700000000",
            "X-Slack-Signature": "v0=test",
        },
    )

    assert response.status_code == 200
    assert process_event_mock.await_count == 1
    assert process_interaction_mock.await_count == 0


@pytest.mark.asyncio
async def test_slack_oauth_callback_allows_org_install_without_state(monkeypatch) -> None:
    from src.api.routers.bots import slack_webhook_handler

    posted_data = {}

    class _FakeSlackResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def json(self):
            return {
                "ok": True,
                "team": {"id": "T123", "name": "Test Workspace"},
                "access_token": "xoxb-test-token",
                "bot_user_id": "U123",
                "authed_user": {"id": "U999"},
                "scope": "app_mentions:read",
                "token_type": "bot",
                "app_id": "A123",
            }

    class _FakeSlackClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def post(self, *args, **kwargs):
            posted_data.update(kwargs.get("data", {}))
            return _FakeSlackResponse()

    monkeypatch.setattr(slack_webhook_handler.aiohttp, "ClientSession", _FakeSlackClientSession)
    monkeypatch.setattr(
        slack_webhook_handler.slack_config,
        "get_credentials",
        AsyncMock(return_value=("signing", "app", "client", "secret")),
    )
    monkeypatch.setattr(slack_webhook_handler.slack_config, "SLACK_AUTH_LOGIN_URL", "")
    monkeypatch.setattr(
        slack_webhook_handler.slack_config,
        "SLACK_BOT_REDIRECT_URI",
        "https://api.example.com/users/bots/slack/oauth/callback",
        raising=False,
    )
    monkeypatch.setattr(
        slack_webhook_handler.BotInstallationService,
        "upsert_installation",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService,
        "_open_dm",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        slack_webhook_handler.SlackBotService,
        "_send_message",
        AsyncMock(return_value=True),
    )

    response = await slack_webhook_handler.slack_oauth_callback(
        code="test-auth-code",
        error=None,
        _state=None,
        session=object(),
    )

    assert response.status_code == 200
    assert posted_data["redirect_uri"] == (
        "https://api.example.com/users/bots/slack/oauth/callback"
    )
