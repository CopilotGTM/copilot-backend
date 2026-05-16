from __future__ import annotations

from unittest.mock import Mock

import pytest
from src.workers.integrations_worker.tasks.crm.hubspot.webhooks import event_task


def _task_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "subscription_type": "deal.creation",
        "portal_id": "123",
        "object_id": "456",
        "event_id": "789",
        "occurred_at": None,
        "event_payload": {
            "subscriptionType": "deal.creation",
            "portalId": 123,
            "objectId": 456,
            "eventId": 789,
        },
    }
    kwargs.update(overrides)
    return kwargs


def test_hubspot_webhook_event_task_retries_transient_dispatch_failures(monkeypatch) -> None:
    class RetryRequestedError(Exception):
        pass

    failure = RuntimeError("hubspot timeout")
    retry = Mock(side_effect=RetryRequestedError)

    monkeypatch.setattr(
        event_task.HubSpotWebhookDispatcher,
        "dispatch",
        Mock(return_value="dispatch-coro"),
    )
    monkeypatch.setattr(event_task, "run_async", Mock(side_effect=failure))
    monkeypatch.setattr(event_task.process_hubspot_webhook_event_task, "retry", retry)
    event_task.process_hubspot_webhook_event_task.request.retries = 1

    with pytest.raises(RetryRequestedError):
        event_task.process_hubspot_webhook_event_task.run(**_task_kwargs())

    retry.assert_called_once()
    assert retry.call_args.kwargs["exc"] is failure
    assert retry.call_args.kwargs["countdown"] > event_task._L.get("retry_delay", 60)


def test_hubspot_webhook_event_task_does_not_retry_after_max_attempts(monkeypatch) -> None:
    failure = RuntimeError("database unavailable")
    retry = Mock()

    monkeypatch.setattr(
        event_task.HubSpotWebhookDispatcher,
        "dispatch",
        Mock(return_value="dispatch-coro"),
    )
    monkeypatch.setattr(event_task, "run_async", Mock(side_effect=failure))
    monkeypatch.setattr(event_task.process_hubspot_webhook_event_task, "retry", retry)
    event_task.process_hubspot_webhook_event_task.request.retries = (
        event_task.process_hubspot_webhook_event_task.max_retries
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        event_task.process_hubspot_webhook_event_task.run(**_task_kwargs())

    retry.assert_not_called()


def test_hubspot_webhook_event_task_returns_unhandled_for_unsupported_event(monkeypatch) -> None:
    dispatch = Mock(return_value=False)
    monkeypatch.setattr(
        event_task.HubSpotWebhookDispatcher,
        "dispatch",
        Mock(return_value="dispatch-coro"),
    )
    monkeypatch.setattr(event_task, "run_async", dispatch)
    event_task.process_hubspot_webhook_event_task.request.retries = 0

    result = event_task.process_hubspot_webhook_event_task.run(
        **_task_kwargs(subscription_type="company.creation")
    )

    assert result == {
        "subscription_type": "company.creation",
        "portal_id": "123",
        "object_id": "456",
        "event_id": "789",
        "handled": False,
    }


def test_hubspot_webhook_event_task_rejects_malformed_payload_without_retry(monkeypatch) -> None:
    retry = Mock()
    dispatch = Mock()

    monkeypatch.setattr(event_task.HubSpotWebhookDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(event_task, "run_async", dispatch)
    monkeypatch.setattr(event_task.process_hubspot_webhook_event_task, "retry", retry)

    result = event_task.process_hubspot_webhook_event_task.run(
        **_task_kwargs(subscription_type="", event_payload="not-a-dict")
    )

    assert result["handled"] is False
    assert result["status"] == "rejected"
    retry.assert_not_called()
    dispatch.assert_not_called()
