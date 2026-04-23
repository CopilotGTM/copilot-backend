"""Tests for the Recall webhook HTTP ingress."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis


def test_recall_webhook_accepts_verified_request(client, monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler

    apply_async = MagicMock(return_value=SimpleNamespace(id="task-123"))
    monkeypatch.setattr(
        recall_webhook_handler.recall_config,
        "get_webhook_secret",
        AsyncMock(return_value="whsec_dGVzdA=="),
    )
    monkeypatch.setattr(
        recall_webhook_handler,
        "verify_recall_webhook_signature",
        MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        recall_webhook_handler.process_meeting_recording_webhook_task,
        "apply_async",
        apply_async,
    )
    monkeypatch.setattr(recall_webhook_handler, "get_redis_client", lambda: fakeredis.FakeRedis())

    response = client.post(
        "/users/meeting-bot/recall/webhook",
        json={
            "event": "bot.done",
            "data": {
                "bot": {
                    "id": "bot_123",
                    "metadata": {"organisation_id": "13d9c636-6a8a-4b8c-b610-a8812c8bda1c"},
                }
            },
        },
        headers={
            "webhook-id": "msg_123",
            "webhook-timestamp": "1710000000",
            "webhook-signature": "v1,test",
        },
    )

    assert response.status_code == 200
    apply_async.assert_called_once()


def test_recall_webhook_rejects_invalid_signature(client, monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler
    from src.service.integrations.meeting_recording.providers import RecallWebhookVerificationError

    apply_async = MagicMock()
    monkeypatch.setattr(
        recall_webhook_handler.recall_config,
        "get_webhook_secret",
        AsyncMock(return_value="whsec_dGVzdA=="),
    )
    monkeypatch.setattr(
        recall_webhook_handler,
        "verify_recall_webhook_signature",
        MagicMock(side_effect=RecallWebhookVerificationError("signature_mismatch")),
    )
    monkeypatch.setattr(
        recall_webhook_handler.process_meeting_recording_webhook_task,
        "apply_async",
        apply_async,
    )

    response = client.post(
        "/users/meeting-bot/recall/webhook",
        json={"event": "bot.done", "data": {"bot": {"id": "bot_123"}}},
        headers={
            "webhook-id": "msg_123",
            "webhook-timestamp": "1710000000",
            "webhook-signature": "v1,test",
        },
    )

    assert response.status_code == 401
    apply_async.assert_not_called()


def test_recall_webhook_dedupes_by_webhook_id(client, monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler

    apply_async = MagicMock()
    # fakeredis gives us real NX/EX semantics without depending on a live Redis.
    redis = fakeredis.FakeRedis()
    monkeypatch.setattr(
        recall_webhook_handler.recall_config,
        "get_webhook_secret",
        AsyncMock(return_value="whsec_dGVzdA=="),
    )
    monkeypatch.setattr(
        recall_webhook_handler,
        "verify_recall_webhook_signature",
        MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        recall_webhook_handler.process_meeting_recording_webhook_task,
        "apply_async",
        apply_async,
    )
    monkeypatch.setattr(recall_webhook_handler, "get_redis_client", lambda: redis)

    payload = {"event": "bot.done", "data": {"bot": {"id": "bot_123"}}}
    headers = {
        "webhook-id": "msg_123",
        "webhook-timestamp": "1710000000",
        "webhook-signature": "v1,test",
    }

    first = client.post("/users/meeting-bot/recall/webhook", json=payload, headers=headers)
    second = client.post("/users/meeting-bot/recall/webhook", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    apply_async.assert_called_once()
