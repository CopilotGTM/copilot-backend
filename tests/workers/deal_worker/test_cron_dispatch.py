from __future__ import annotations

import pytest
from src.workers.deal_worker.cron_dispatch import dispatch_scheduled_job
from src.workers.deal_worker.scheduled_jobs import get_scheduled_job


def test_get_scheduled_job_returns_deal_mapping_definition() -> None:
    job = get_scheduled_job("deal-mapping")

    assert job.name == "deal-mapping"
    assert job.task_name == "stakeholder.sync_all_deals_stakeholders_all_orgs"
    assert job.queue == "deal"
    assert job.kwargs == {}


def test_get_scheduled_job_rejects_unknown_job_name() -> None:
    with pytest.raises(KeyError):
        get_scheduled_job("does-not-exist")


def test_dispatch_scheduled_job_enqueues_and_records_heartbeat(mocker) -> None:
    send_task = mocker.patch(
        "src.workers.deal_worker.cron_dispatch.send_deal_task",
        return_value="task-123",
    )
    record_dispatch = mocker.patch(
        "src.workers.deal_worker.cron_dispatch.record_scheduler_dispatch"
    )

    task_id = dispatch_scheduled_job("deal-mapping")

    assert task_id == "task-123"
    send_task.assert_called_once_with(
        task_name="stakeholder.sync_all_deals_stakeholders_all_orgs",
        kwargs={},
        queue="deal",
        routing_key="deal",
    )
    record_dispatch.assert_called_once()


def test_dispatch_scheduled_job_records_failure_before_raising(mocker) -> None:
    mocker.patch(
        "src.workers.deal_worker.cron_dispatch.send_deal_task",
        side_effect=RuntimeError("broker down"),
    )
    record_failure = mocker.patch("src.workers.deal_worker.cron_dispatch.record_scheduler_failure")

    with pytest.raises(RuntimeError, match="broker down"):
        dispatch_scheduled_job("deal-mapping")

    record_failure.assert_called_once()
