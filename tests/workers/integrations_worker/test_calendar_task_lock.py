"""Concurrency contract tests for integration-scoped Calendar sync."""

import inspect
from unittest.mock import patch
from uuid import uuid4

from src.service.integrations.calendar_cursor_metadata import CalendarCursorMissingError
from src.workers.integrations_worker.tasks.calendar import integration_task as calendar_task_module
from src.workers.integrations_worker.tasks.calendar.integration_task import (
    sync_calendar_integration_task,
)
from src.workers.integrations_worker.utils.dedupe import acquire_sync_lock


def test_running_calendar_sync_defers_without_calling_provider() -> None:
    """A second task must queue behind the cursor owner instead of fetching."""
    organisation_id = str(uuid4())
    integration_id = str(uuid4())

    with (
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.acquire_sync_lock",
            return_value=False,
            create=True,
        ),
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.send_integrations_task",
            return_value="deferred-task-id",
            create=True,
        ) as send_task,
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.run_async"
        ) as run_async,
    ):
        result = sync_calendar_integration_task.run(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

    assert result["status"] == "deferred"
    assert result["task_id"] == "deferred-task-id"
    run_async.assert_not_called()
    send_task.assert_called_once()
    assert send_task.call_args.kwargs["countdown"] > 0


def test_calendar_lock_can_fail_closed_when_redis_is_unavailable() -> None:
    """Calendar safety prefers a deferred sync over concurrent provider reads."""
    with patch(
        "src.workers.integrations_worker.utils.dedupe.get_redis_client",
        side_effect=RuntimeError("redis unavailable"),
    ):
        assert (
            acquire_sync_lock(
                "integration-id",
                "calendar",
                fail_open=False,
            )
            is False
        )


def test_calendar_lock_is_released_after_sync() -> None:
    """The cursor owner must release its lock after every completed task."""
    organisation_id = str(uuid4())
    integration_id = str(uuid4())

    def complete_sync(coroutine):
        """Close the mocked coroutine while returning a completed task result."""
        coroutine.close()
        return {"status": "success", "events_synced": 0}

    with (
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.acquire_sync_lock",
            return_value=True,
        ),
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.release_sync_lock"
        ) as release_lock,
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.run_async",
            side_effect=complete_sync,
        ),
    ):
        result = sync_calendar_integration_task.run(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

    assert result["status"] == "success"
    release_lock.assert_called_once_with(integration_id, "calendar")


def test_missing_cursor_failure_is_not_retried() -> None:
    """Retrying a cursorless bootstrap would multiply the expensive full read."""
    organisation_id = str(uuid4())
    integration_id = str(uuid4())
    calls = 0

    def fail_fetch_then_record(coroutine):
        """Fail the provider phase once and allow failure-state persistence."""
        nonlocal calls
        coroutine.close()
        calls += 1
        if calls == 1:
            raise CalendarCursorMissingError("provider returned no cursor")
        return None

    with (
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.acquire_sync_lock",
            return_value=True,
        ),
        patch("src.workers.integrations_worker.tasks.calendar.integration_task.release_sync_lock"),
        patch(
            "src.workers.integrations_worker.tasks.calendar.integration_task.run_async",
            side_effect=fail_fetch_then_record,
        ),
        patch.object(
            sync_calendar_integration_task,
            "retry",
            side_effect=AssertionError("cursor failure must not retry"),
        ) as retry,
    ):
        result = sync_calendar_integration_task.run(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

    assert result["status"] == "failed"
    retry.assert_not_called()


def test_calendar_count_is_staged_before_provider_transaction_commit() -> None:
    """Calendar rows and their progress count must cross the transaction boundary together."""
    source = inspect.getsource(calendar_task_module)

    assert source.index("record_provider_batch_in_session") < source.index("await session.commit()")
