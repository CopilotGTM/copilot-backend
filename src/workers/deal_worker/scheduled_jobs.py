"""Scheduled deal-worker jobs consumed by Kubernetes CronJobs.

Entries are immutable contracts used by deployment checks and dispatch tests.
The schedule strings here must stay aligned with Helm CronJob rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScheduledJob:
    """Fixed-schedule job definition for deal-worker execution."""

    name: str
    task_name: str
    schedule: str
    max_lag_seconds: int
    queue: str = "deal"
    routing_key: str = "deal"
    kwargs: dict[str, Any] = field(default_factory=dict)


SCHEDULED_JOBS: dict[str, ScheduledJob] = {
    "deal-mapping": ScheduledJob(
        name="deal-mapping",
        task_name="stakeholder.sync_all_deals_stakeholders_all_orgs",
        schedule="3 */2 * * *",
        max_lag_seconds=3 * 60 * 60,
    ),
    "stale-deal-job-cleanup": ScheduledJob(
        name="stale-deal-job-cleanup",
        task_name="deal.cleanup_stale_jobs_all_orgs",
        schedule="45 * * * *",
        max_lag_seconds=90 * 60,
    ),
    "deal-memory-backfill": ScheduledJob(
        name="deal-memory-backfill",
        task_name="deal_memory.trigger_backfill_all_orgs",
        schedule="10 */3 * * *",
        max_lag_seconds=4 * 60 * 60,
    ),
    "email-attachment-extraction": ScheduledJob(
        name="email-attachment-extraction",
        task_name="deal_asset.extract_email_attachments_all_orgs",
        schedule="20,50 * * * *",
        max_lag_seconds=45 * 60,
    ),
    "stakeholder-research-retry": ScheduledJob(
        name="stakeholder-research-retry",
        task_name="stakeholder_research.trigger_retry_all_orgs",
        schedule="20 * * * *",
        max_lag_seconds=90 * 60,
    ),
    "meeting-bot-fallback": ScheduledJob(
        name="meeting-bot-fallback",
        task_name="deal.trigger_send_unsent_meeting_recordings_all_orgs",
        schedule="0 20 * * *",
        max_lag_seconds=90 * 60,
    ),
    "sales-dashboard-snapshot": ScheduledJob(
        name="sales-dashboard-snapshot",
        task_name="dashboard.refresh_sales_snapshots_all_orgs",
        schedule="15 1 * * *",
        max_lag_seconds=26 * 60 * 60,
    ),
}


def get_scheduled_job(job_name: str) -> ScheduledJob:
    """Resolve a named scheduled job definition.

    Raises:
        KeyError: if the job name does not exist.
    """

    return SCHEDULED_JOBS[job_name]
