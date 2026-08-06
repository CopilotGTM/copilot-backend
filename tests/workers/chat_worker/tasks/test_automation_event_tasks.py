"""Celery contracts for directly delivered automation-event activation."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.workers.chat_worker.config import ChatWorkerConfig


def test_automation_event_activation_and_recovery_are_routed() -> None:
    """Direct publication is backed by a low-frequency durable-row recovery sweep."""

    config = ChatWorkerConfig()

    assert config.task_routes["chat.activate_automation_event"]["queue"] == "chat_processing"
    assert "chat.recover_automation_events_all_orgs" in config.task_routes
    assert "chat.recover_automation_events_org" in config.task_routes
    recovery = config.beat_schedule["recover-automation-events"]
    assert recovery["task"] == "chat.recover_automation_events_all_orgs"
    assert recovery["schedule"] == 60.0


def test_due_schedule_tasks_use_coordinator_and_retrying_org_tiers() -> None:
    """Verify the contract that due schedule tasks use coordinator and retrying org tiers."""
    from src.workers.chat_worker.tasks.automation_event_tasks import (
        dispatch_due_automation_schedules_all_orgs_task,
        dispatch_due_automation_schedules_org_task,
    )

    assert dispatch_due_automation_schedules_all_orgs_task.max_retries == 0
    assert dispatch_due_automation_schedules_org_task.max_retries == 3


def test_recovery_tasks_use_coordinator_and_retrying_org_tiers() -> None:
    """Verify the contract that recovery tasks use coordinator and retrying org tiers."""
    from src.workers.chat_worker.tasks.automation_event_tasks import (
        recover_automation_events_all_orgs_task,
        recover_automation_events_org_task,
    )

    assert recover_automation_events_all_orgs_task.max_retries == 0
    assert recover_automation_events_org_task.max_retries == 3


def test_recovery_org_task_republishes_each_pending_identity(monkeypatch) -> None:
    """Verify the contract that recovery org task republishes each pending identity."""
    from src.workers.chat_worker.tasks import automation_event_tasks

    organisation_id = uuid4()
    event_ids = [uuid4(), uuid4()]
    published = []

    def run_async(awaitable):  # noqa: ANN001
        """Build the run async fixture used by the surrounding regression cases."""
        awaitable.close()
        return event_ids

    monkeypatch.setattr(automation_event_tasks, "run_async", run_async)
    monkeypatch.setattr(
        automation_event_tasks.activate_automation_event_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    result = automation_event_tasks.recover_automation_events_org_task.run(str(organisation_id))

    assert result == {
        "status": "completed",
        "organisation_id": str(organisation_id),
        "pending_count": 2,
        "republished_count": 2,
        "publish_failures": 0,
    }
    assert [item["kwargs"]["event_id"] for item in published] == [str(item) for item in event_ids]


def test_due_schedule_coordinator_only_fans_out_returned_approved_orgs(monkeypatch) -> None:
    """Verify the contract that due schedule coordinator only fans out returned approved orgs."""
    from src.workers.chat_worker.tasks import automation_event_tasks

    organisation_ids = [uuid4(), uuid4()]

    class Rows:
        """Represent the Rows test double used by the surrounding regression cases."""

        def scalars(self):
            """Expose the scalars operation expected by the Rows test double."""
            return self

        def all(self):
            """Expose the all operation expected by the Rows test double."""
            return organisation_ids

    class Session:
        """Represent the Session test double used by the surrounding regression cases."""

        async def execute(self, statement):  # noqa: ANN001
            """Expose the execute operation expected by the Session test double."""
            sql = str(statement)
            assert "organisations.status" in sql
            assert "organisations.is_active" in sql
            return Rows()

    async def sessions():
        """Build the sessions fixture used by the surrounding regression cases."""
        yield Session()

    published = []
    monkeypatch.setattr(automation_event_tasks, "acquire_master_async_session", sessions)
    monkeypatch.setattr(
        automation_event_tasks.dispatch_due_automation_schedules_org_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    result = automation_event_tasks.dispatch_due_automation_schedules_all_orgs_task.run()

    assert result == {"status": "completed", "orgs_found": 2, "orgs_dispatched": 2}
    assert [item["kwargs"]["organisation_id"] for item in published] == [
        str(item) for item in organisation_ids
    ]


def test_due_schedule_org_task_retries_with_exponential_backoff(monkeypatch) -> None:
    """Verify the contract that due schedule org task retries with exponential backoff."""
    from src.workers.chat_worker.tasks import automation_event_tasks

    class RetryRequestedError(Exception):
        """Represent the Retry Requested Error test double used by the surrounding regression cases."""

        pass

    task = automation_event_tasks.dispatch_due_automation_schedules_org_task
    retries = []

    def retry(*, exc, countdown):  # noqa: ANN001
        """Build the retry fixture used by the surrounding regression cases."""
        retries.append((exc, countdown))
        return RetryRequestedError()

    monkeypatch.setattr(
        automation_event_tasks,
        "run_async",
        lambda _awaitable: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(task, "retry", retry)
    task.push_request(retries=2)
    try:
        with pytest.raises(RetryRequestedError):
            task.run(str(uuid4()))
    finally:
        task.pop_request()

    assert retries[0][1] == 120


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
