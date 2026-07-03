from __future__ import annotations

import pytest
from src.workers.knowledge_worker.cron_dispatch import dispatch_scheduled_job
from src.workers.knowledge_worker.scheduled_jobs import SCHEDULED_JOBS, get_scheduled_job


def test_get_scheduled_job_returns_prod_knowledge_cleanup_definition() -> None:
    job = get_scheduled_job("cleanup-stale-knowledge-jobs")

    assert job.name == "cleanup-stale-knowledge-jobs"
    assert job.task_name == "knowledge.cleanup_stale_jobs_all_orgs"
    assert job.schedule == "0 */6 * * *"
    assert job.max_lag_seconds == 7 * 60 * 60
    assert job.queue == "knowledge_upload"
    assert job.kwargs == {}
    assert "cleanup-stale-db-connections-every-10-min" not in SCHEDULED_JOBS


def test_get_scheduled_job_rejects_unknown_job_name() -> None:
    with pytest.raises(KeyError):
        get_scheduled_job("does-not-exist")


def test_dispatch_scheduled_job_enqueues_and_records_heartbeat(mocker) -> None:
    send_task = mocker.patch(
        "src.workers.knowledge_worker.cron_dispatch.send_knowledge_task",
        return_value="task-123",
    )
    record_dispatch = mocker.patch(
        "src.workers.knowledge_worker.cron_dispatch.record_scheduler_dispatch"
    )

    task_id = dispatch_scheduled_job("cleanup-stale-knowledge-jobs")

    assert task_id == "task-123"
    send_task.assert_called_once_with(
        task_name="knowledge.cleanup_stale_jobs_all_orgs",
        kwargs={},
        queue="knowledge_upload",
        routing_key="knowledge_upload",
    )
    record_dispatch.assert_called_once()
