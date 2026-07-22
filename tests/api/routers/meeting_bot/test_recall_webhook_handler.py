"""Tests for the Recall webhook HTTP ingress."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.meeting_bot import meeting_bot_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(meeting_bot_router, prefix="/users/meeting-bot")
    return TestClient(app)


def test_recall_webhook_accepts_verified_request(monkeypatch, caplog) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler

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
        recall_webhook_handler,
        "send_deal_task_async",
        AsyncMock(return_value="task-123"),
    )
    client = _client()
    payload = {
        "event": "bot.done",
        "data": {
            "bot": {
                "id": "bot_123",
                "metadata": {"organisation_id": "13d9c636-6a8a-4b8c-b610-a8812c8bda1c"},
            }
        },
    }
    headers = {
        "webhook-id": "msg_123",
        "webhook-timestamp": "1710000000",
        "webhook-signature": "v1,test",
    }

    with caplog.at_level(logging.INFO, logger=recall_webhook_handler.logger.name):
        response = client.post(
            "/users/meeting-bot/recall/webhook",
            json=payload,
            headers=headers,
        )

    assert response.status_code == 200
    recall_webhook_handler.send_deal_task_async.assert_awaited_once()
    queued_logs = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "recall_webhook_task_queued"
    ]
    assert len(queued_logs) == 1
    assert queued_logs[0].webhook_id == "msg_123"
    assert queued_logs[0].recall_event == "bot.done"
    assert queued_logs[0].provider_session_id == "bot_123"
    assert queued_logs[0].organisation_id == "13d9c636-6a8a-4b8c-b610-a8812c8bda1c"
    assert queued_logs[0].celery_task_id == "task-123"


def test_recall_webhook_rejects_invalid_signature(monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler
    from src.service.meeting_bot.providers import RecallWebhookVerificationError

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
        recall_webhook_handler,
        "send_deal_task_async",
        AsyncMock(),
    )

    client = _client()
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
    recall_webhook_handler.send_deal_task_async.assert_not_awaited()


def test_recall_webhook_redelivery_reaches_idempotent_worker(monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler

    # Ingress must not acknowledge-and-drop a retry when the first worker can
    # still fail; session-level idempotency owns duplicate processing.
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
        recall_webhook_handler,
        "send_deal_task_async",
        AsyncMock(return_value="task-123"),
    )
    payload = {"event": "bot.done", "data": {"bot": {"id": "bot_123"}}}
    headers = {
        "webhook-id": "msg_123",
        "webhook-timestamp": "1710000000",
        "webhook-signature": "v1,test",
    }

    client = _client()
    first = client.post("/users/meeting-bot/recall/webhook", json=payload, headers=headers)
    second = client.post("/users/meeting-bot/recall/webhook", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert recall_webhook_handler.send_deal_task_async.await_count == 2


def test_recall_webhook_provider_can_retry_when_queueing_fails(monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler

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
        recall_webhook_handler,
        "send_deal_task_async",
        AsyncMock(side_effect=[RuntimeError("queue unavailable"), "task-123"]),
    )
    payload = {"event": "bot.done", "data": {"bot": {"id": "bot_123"}}}
    headers = {
        "webhook-id": "msg_123",
        "webhook-timestamp": "1710000000",
        "webhook-signature": "v1,test",
    }

    client = _client()
    first = client.post("/users/meeting-bot/recall/webhook", json=payload, headers=headers)
    second = client.post("/users/meeting-bot/recall/webhook", json=payload, headers=headers)

    assert first.status_code == 502
    assert second.status_code == 200
    assert recall_webhook_handler.send_deal_task_async.await_count == 2


def test_recall_webhook_rejects_oversized_body_before_dispatch(monkeypatch) -> None:
    """Chunked or declared oversized traffic cannot reach JSON parsing or Celery."""
    from src.api.routers.meeting_bot import recall_webhook_handler

    monkeypatch.setattr(
        recall_webhook_handler,
        "send_deal_task_async",
        AsyncMock(),
    )
    client = _client()
    response = client.post(
        "/users/meeting-bot/recall/webhook",
        content=b"x" * (recall_webhook_handler.RECALL_WEBHOOK_MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    recall_webhook_handler.send_deal_task_async.assert_not_awaited()


def test_recall_webhook_rejects_non_object_json_after_verification(monkeypatch) -> None:
    from src.api.routers.meeting_bot import recall_webhook_handler

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
    monkeypatch.setattr(recall_webhook_handler, "send_deal_task_async", AsyncMock())

    response = _client().post(
        "/users/meeting-bot/recall/webhook",
        json=["not", "an", "event"],
    )

    assert response.status_code == 400
    recall_webhook_handler.send_deal_task_async.assert_not_awaited()
