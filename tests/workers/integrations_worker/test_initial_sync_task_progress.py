"""Regression tests proving optional progress cannot suppress provider workers."""

from unittest.mock import patch
from uuid import uuid4

from src.workers.integrations_worker.tasks.calendar.integration_task import (
    sync_calendar_integration_task,
)
from src.workers.integrations_worker.tasks.email.integration_task import (
    sync_email_integration_task,
)


def _fail_progress_then_complete(calls: list[str], result: dict):
    def run(coroutine):
        coroutine.close()
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("progress unavailable")
        return result

    return run


def test_email_progress_start_failure_does_not_suppress_provider_sync() -> None:
    """The email provider remains authoritative when visibility is unavailable."""
    calls: list[str] = []
    provider_result = {"status": "success", "messages_synced": 0}

    with patch(
        "src.workers.integrations_worker.tasks.email.integration_task.run_async",
        side_effect=_fail_progress_then_complete(calls, provider_result),
    ):
        result = sync_email_integration_task.run(
            organisation_id=str(uuid4()),
            integration_id=str(uuid4()),
            initial_sync_id=str(uuid4()),
        )

    assert result["status"] == "success"
    assert len(calls) == 2


def test_calendar_progress_start_failure_does_not_suppress_provider_sync() -> None:
    """The calendar provider remains authoritative when visibility is unavailable."""
    calls: list[str] = []
    provider_result = {"status": "success", "events_synced": 0}

    with (
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.run_async",
            side_effect=_fail_progress_then_complete(calls, provider_result),
        ),
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.acquire_sync_lock",
            return_value=True,
        ),
        patch("src.workers.integrations_worker.tasks.calendar.integration_task.release_sync_lock"),
    ):
        result = sync_calendar_integration_task.run(
            organisation_id=str(uuid4()),
            integration_id=str(uuid4()),
            initial_sync_id=str(uuid4()),
        )

    assert result["status"] == "success"
    assert len(calls) == 2
