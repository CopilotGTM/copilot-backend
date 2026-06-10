"""Tests for the meeting summary notification Celery task."""

from __future__ import annotations

import asyncio
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


def test_notification_task_passes_none_deal_id_to_service(monkeypatch):
    """Non-deal meeting summaries should load meeting-level notification context."""
    from src.workers.deal_worker.tasks.meeting_insight_tasks import (
        meeting_summary_notification_task as task_module,
    )

    captured: dict[str, object] = {}

    def _fake_run_async(coro):
        return asyncio.run(coro)

    class _FakeService:
        def __init__(self, organisation_id):
            captured["organisation_id"] = organisation_id

        async def notify(self, *, deal_id, event_id):
            captured["deal_id"] = deal_id
            captured["event_id"] = event_id
            return {
                "status": "completed",
                "targets_found": 0,
                "sent": 0,
                "skipped": 0,
                "failed": 0,
            }

    org_id = uuid4()
    event_id = uuid4()
    monkeypatch.setattr(task_module, "run_async", _fake_run_async)
    monkeypatch.setattr(task_module, "MeetingSummaryNotificationService", _FakeService)

    result = task_module.send_meeting_summary_ready_notification_task.run(
        str(org_id),
        None,
        str(event_id),
    )

    assert result["status"] == "completed"
    assert captured["organisation_id"] == org_id
    assert captured["deal_id"] is None
    assert captured["event_id"] == event_id
