from __future__ import annotations

from src.domain.models.organisation_tables.job_models import DealJobType
from src.workers.deal_worker.config import DealWorkerConfig
from src.workers.deal_worker.tasks.deal_insight_tasks import sync_deal_tasks_from_summary


def test_deal_task_sync_job_type_exists() -> None:
    assert DealJobType.DEAL_TASK_SYNC.value == "deal_task_sync"


def test_deal_worker_routes_deal_task_sync_to_deal_jobs() -> None:
    config = DealWorkerConfig()

    route = config.task_routes["deal_tasks.sync_from_summary"]

    assert route["queue"] == "deal_jobs"
    assert route["routing_key"] == "deal_jobs"


def test_deal_task_sync_task_is_registered_with_expected_name() -> None:
    assert sync_deal_tasks_from_summary.name == "deal_tasks.sync_from_summary"
