"""Create or reuse a deal task sync job and enqueue the Celery task.

Example:
    uv run python scripts/local/development/trigger_deal_task_sync.py \
        --organisation-id 00000000-0000-0000-0000-000000000000 \
        --deal-id 11111111-1111-1111-1111-111111111111
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from uuid import UUID

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import org_async_session_scope
from src.infra.utils.async_runner import run_async
from src.workers.deal_worker.tasks.deal_insight_tasks.deal_task_sync_task import (
    sync_deal_tasks_from_summary,
)
from src.workers.deal_worker.utils.pipeline_dispatch import AUTOMATED_PIPELINE_USER_ID


def build_deal_task_sync_job(*, organisation_id: UUID, deal_id: UUID) -> DealJobs:
    """Build the minimal persisted job required by the Celery task."""
    return DealJobs(
        organisation_id=organisation_id,
        user_id=AUTOMATED_PIPELINE_USER_ID,
        job_type=DealJobType.DEAL_TASK_SYNC,
        deal_id=deal_id,
        status=JobStatus.PENDING,
        job_metadata={"source": "manual_local_test"},
    )


async def create_deal_task_sync_job(*, organisation_id: UUID, deal_id: UUID) -> UUID:
    job = build_deal_task_sync_job(organisation_id=organisation_id, deal_id=deal_id)

    async with org_async_session_scope(organisation_id) as session:
        session.add(job)
        await session.commit()
        await session.refresh(job)

    return job.job_id


def enqueue_deal_task_sync(*, organisation_id: UUID, deal_id: UUID, job_id: UUID) -> str:
    async_result = sync_deal_tasks_from_summary.apply_async(
        args=[str(organisation_id), str(deal_id), str(job_id)],
        queue="deal_jobs",
        routing_key="deal_jobs",
    )
    return str(async_result.id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trigger the deal task sync Celery task for a deal.",
    )
    parser.add_argument("--organisation-id", required=True, help="Organisation UUID")
    parser.add_argument("--deal-id", required=True, help="Deal UUID")
    parser.add_argument(
        "--job-id",
        help="Existing DEAL_TASK_SYNC job UUID. If omitted, a new pending job is created.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    organisation_id = UUID(args.organisation_id)
    deal_id = UUID(args.deal_id)
    job_id = (
        UUID(args.job_id)
        if args.job_id
        else run_async(create_deal_task_sync_job(organisation_id=organisation_id, deal_id=deal_id))
    )

    celery_task_id = enqueue_deal_task_sync(
        organisation_id=organisation_id,
        deal_id=deal_id,
        job_id=job_id,
    )
    print(f"DEAL_TASK_SYNC job_id={job_id}")
    print(f"Celery task_id={celery_task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
