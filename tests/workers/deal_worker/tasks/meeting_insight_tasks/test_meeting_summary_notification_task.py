"""Tests for the meeting summary notification Celery task."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


def test_notification_task_retries_when_service_reports_failed_targets(monkeypatch):
    """Provider failures are stored by the service, then retried by Celery."""
    from src.workers.deal_worker.tasks.meeting_insight_tasks import (
        meeting_summary_notification_task as task_module,
    )

    class _RetryRaisedError(Exception):
        pass

    retry_calls: list[dict[str, object]] = []

    def _fake_run_async(coro):
        coro.close()
        return {
            "status": "failed",
            "targets_found": 1,
            "sent": 0,
            "skipped": 0,
            "failed": 1,
        }

    def _fake_retry(*, exc, countdown):
        retry_calls.append({"exc": exc, "countdown": countdown})
        raise _RetryRaisedError from exc

    task = task_module.send_meeting_summary_ready_notification_task
    monkeypatch.setattr(task_module, "run_async", _fake_run_async)
    monkeypatch.setattr(task, "retry", _fake_retry)
    task.request.retries = 0

    with pytest.raises(_RetryRaisedError):
        task.run(str(uuid4()), str(uuid4()), str(uuid4()))

    assert len(retry_calls) == 1
    assert isinstance(retry_calls[0]["exc"], RuntimeError)
    assert retry_calls[0]["countdown"] == 60
