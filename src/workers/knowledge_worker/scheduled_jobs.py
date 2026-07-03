from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScheduledJob:
    """Configuration for a fixed-schedule knowledge trigger."""

    name: str
    task_name: str
    schedule: str
    max_lag_seconds: int
    queue: str = "knowledge_upload"
    routing_key: str = "knowledge_upload"
    kwargs: dict[str, Any] = field(default_factory=dict)


SCHEDULED_JOBS: dict[str, ScheduledJob] = {
    "cleanup-stale-knowledge-jobs": ScheduledJob(
        name="cleanup-stale-knowledge-jobs",
        task_name="knowledge.cleanup_stale_jobs_all_orgs",
        schedule="0 */6 * * *",
        max_lag_seconds=7 * 60 * 60,
    ),
}


def get_scheduled_job(job_name: str) -> ScheduledJob:
    """Return the configured schedule definition for a named knowledge job."""

    return SCHEDULED_JOBS[job_name]
