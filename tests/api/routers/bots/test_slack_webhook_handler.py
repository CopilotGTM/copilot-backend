import json
from unittest.mock import AsyncMock

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
