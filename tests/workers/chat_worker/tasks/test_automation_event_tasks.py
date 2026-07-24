"""Celery contracts for directly delivered automation-event activation."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.workers.chat_worker.config import ChatWorkerConfig


def test_automation_event_task_is_routed_without_a_polling_schedule() -> None:
    """Direct activation uses the processing queue while recovery remains future work."""

    config = ChatWorkerConfig()

    assert config.task_routes["chat.activate_automation_event"]["queue"] == "chat_processing"
    assert "chat.recover_automation_events" not in config.task_routes
    assert "recover-automation-events" not in config.beat_schedule


def test_activation_task_retries_failures_with_exponential_backoff(monkeypatch) -> None:
    """Transient activation failures use the queue retry budget before becoming terminal."""

    from src.workers.chat_worker.tasks import automation_event_tasks

    class Service:
        """Expose the task-to-service retry contract without database work."""

        def __init__(self, _organisation_id) -> None:
            return None

        def activate(self, _dispatch_id, *, retry_allowed):
            """Return an awaitable placeholder after checking retry remains allowed."""

            assert retry_allowed
            return object()

    class RetryRequestedError(Exception):
        """Represent Celery accepting the task's retry request."""

        pass

    countdowns: list[int] = []
    task = automation_event_tasks.activate_automation_event_task

    def retry(*, countdown):
        """Capture the exponential delay before simulating Celery retry control flow."""

        countdowns.append(countdown)
        return RetryRequestedError()

    monkeypatch.setattr(
        automation_event_tasks, "AutomationEventActivationExecutionService", Service
    )
    monkeypatch.setattr(automation_event_tasks, "run_async", lambda _awaitable: {"status": "retry"})
    monkeypatch.setattr(task, "retry", retry)
    task.push_request(retries=2)
    try:
        with pytest.raises(RetryRequestedError):
            task.run(str(uuid4()), str(uuid4()))
    finally:
        task.pop_request()

    assert countdowns == [40]
