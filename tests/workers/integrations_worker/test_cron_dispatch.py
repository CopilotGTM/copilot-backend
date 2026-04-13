from __future__ import annotations

import pytest
from src.workers.integrations_worker.cron_dispatch import dispatch_scheduled_job
from src.workers.integrations_worker.scheduled_jobs import get_scheduled_job


def test_get_scheduled_job_returns_calendar_schedule_definition() -> None:
    job = get_scheduled_job("sync-calendar-events")

    assert job.name == "sync-calendar-events"
    assert job.task_name == "integrations.sync_calendar_all_orgs"
    assert job.queue == "integrations"
    assert job.kwargs == {}


def test_get_scheduled_job_rejects_unknown_job_name() -> None:
    with pytest.raises(KeyError):
        get_scheduled_job("does-not-exist")


def test_dispatch_scheduled_job_enqueues_and_records_heartbeat(mocker) -> None:
    send_task = mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.send_integrations_task",
        return_value="task-123",
    )
    record_dispatch = mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.record_scheduler_dispatch"
    )

    task_id = dispatch_scheduled_job("sync-calendar-events")

    assert task_id == "task-123"
    send_task.assert_called_once_with(
        task_name="integrations.sync_calendar_all_orgs",
        kwargs={},
        queue="integrations",
        routing_key="integrations",
    )
    record_dispatch.assert_called_once()


def test_dispatch_scheduled_job_records_failure_before_raising(mocker) -> None:
    mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.send_integrations_task",
        side_effect=RuntimeError("broker down"),
    )
    record_failure = mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.record_scheduler_failure"
    )

    with pytest.raises(RuntimeError, match="broker down"):
        dispatch_scheduled_job("sync-calendar-events")

    record_failure.assert_called_once()
