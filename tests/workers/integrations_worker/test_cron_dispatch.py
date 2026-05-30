"""Regression coverage for dispatching cron jobs."""

from __future__ import annotations

import pytest
from src.workers.integrations_worker.cron_dispatch import dispatch_scheduled_job
from src.workers.integrations_worker.scheduled_jobs import get_scheduled_job


def test_get_scheduled_job_returns_deal_messages_schedule_definition() -> None:
    """Regression test for get scheduled job returns deal messages schedule definition."""

    job = get_scheduled_job("sync-deal-messages-all-orgs")

    assert job.name == "sync-deal-messages-all-orgs"
    assert job.task_name == "integrations.sync_deal_messages_all_orgs"
    assert job.queue == "integrations"
    assert job.kwargs == {}


def test_get_scheduled_job_returns_hubspot_core_incremental_schedule_definition() -> None:
    """Regression test for get scheduled job returns hubspot core incremental schedule definition."""

    job = get_scheduled_job("sync-hubspot-core-incremental-all-orgs")

    assert job.name == "sync-hubspot-core-incremental-all-orgs"
    assert job.task_name == "integrations.sync_hubspot_core_incremental_all_orgs"
    assert job.schedule == "*/10 * * * *"
    assert job.max_lag_seconds == 60 * 15
    assert job.queue == "integrations"
    assert job.kwargs == {}


def test_get_scheduled_job_returns_hubspot_delete_reconciliation_schedule_definition() -> None:
    """Regression test for get scheduled job returns hubspot delete reconciliation schedule definition."""

    job = get_scheduled_job("reconcile-hubspot-deletions-all-orgs")

    assert job.name == "reconcile-hubspot-deletions-all-orgs"
    assert job.task_name == "integrations.reconcile_hubspot_deletions_all_orgs"
    assert job.schedule == "17 */12 * * *"
    assert job.max_lag_seconds == 60 * 60 * 14
    assert job.queue == "integrations"
    assert job.kwargs == {}


def test_get_scheduled_job_rejects_unknown_job_name() -> None:
    """Regression test for get scheduled job rejects unknown job name."""

    with pytest.raises(KeyError):
        get_scheduled_job("does-not-exist")


def test_dispatch_scheduled_job_enqueues_and_records_heartbeat(mocker) -> None:
    """Regression test for dispatch scheduled job enqueues and records heartbeat."""

    send_task = mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.send_integrations_task",
        return_value="task-123",
    )
    record_dispatch = mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.record_scheduler_dispatch"
    )

    task_id = dispatch_scheduled_job("sync-deal-messages-all-orgs")

    assert task_id == "task-123"
    send_task.assert_called_once_with(
        task_name="integrations.sync_deal_messages_all_orgs",
        kwargs={},
        queue="integrations",
        routing_key="integrations",
    )
    record_dispatch.assert_called_once()


def test_dispatch_scheduled_job_records_failure_before_raising(mocker) -> None:
    """Regression test for dispatch scheduled job records failure before raising."""

    mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.send_integrations_task",
        side_effect=RuntimeError("broker down"),
    )
    record_failure = mocker.patch(
        "src.workers.integrations_worker.cron_dispatch.record_scheduler_failure"
    )

    with pytest.raises(RuntimeError, match="broker down"):
        dispatch_scheduled_job("sync-deal-messages-all-orgs")

    record_failure.assert_called_once()
