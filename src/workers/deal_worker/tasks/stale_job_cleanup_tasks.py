"""Stale job cleanup tasks for deal worker.

Three-tier architecture:
- Tier 1: cleanup_stale_deal_jobs_all_orgs_task - Coordinator, dispatches to all orgs
- Tier 2: cleanup_stale_deal_jobs_org_task - Per-org cleanup with retry support

Runs hourly via Celery Beat to mark stale deal jobs as FAILED:
- PENDING jobs: stuck for more than configured threshold (default 2 hours)
- IN_PROGRESS jobs: stuck for more than configured threshold (default 35 minutes)

Different job types may have different thresholds to account for varying
expected completion times.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import (
    Organisation,
    OrganisationStatus,
)
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.stale_job_cleanup_service import (
    DealStaleJobCleanupService,
)

logger = get_logger("deal_stale_job_cleanup_tasks")


@app.task(
    name="deal.cleanup_stale_jobs_all_orgs",
    bind=False,  # Tier 1: No self binding, never retries
    max_retries=0,  # CRITICAL: Don't retry coordinator to avoid duplicate dispatches
    acks_late=True,
    soft_time_limit=300,  # 5 minutes (coordinator is fast)
    time_limit=360,
)
def cleanup_stale_deal_jobs_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Coordinator task that dispatches cleanup to all approved organizations.

    This task:
    1. Queries all approved organizations from master schema
    2. Dispatches Tier 2 task for each organization
    3. Returns summary of dispatched tasks

    Scheduled to run every hour via Celery Beat.
    """
    logger.info(
        "Starting stale deal job cleanup coordinator for all organisations",
        extra={"event": "stale_job_cleanup_all_orgs_started"},
    )

    async def _dispatch_all_orgs() -> dict[str, Any]:
        org_ids: list[UUID] = []

        # Query all approved organizations from master schema
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        # Session released here (fresh session pattern)

        if not org_ids:
            logger.info(
                "No approved organisations found for stale job cleanup",
                extra={
                    "event": "stale_job_cleanup_no_organisations",
                    "organisation_count": 0,
                    "status": "no_organisations",
                },
            )
            return {
                "status": "completed",
                "orgs_found": 0,
                "orgs_triggered": 0,
                "org_tasks": [],
            }

        logger.info(
            "Found approved organisations for stale job cleanup coordinator",
            extra={
                "event": "stale_job_cleanup_all_orgs_found",
                "organisation_count": len(org_ids),
            },
        )

        # Dispatch Tier 2 for each organization
        org_tasks = []
        for org_id in org_ids:
            try:
                task = cleanup_stale_deal_jobs_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    queue="deal",
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "task_id": task.id,
                    }
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch stale deal job cleanup for organisation",
                    extra={
                        "event": "stale_job_cleanup_dispatch_failed",
                        "organisation_id": str(org_id),
                        "failure_reason": str(e),
                    },
                    exc_info=True,
                )

        logger.info(
            "Stale deal job cleanup coordinator completed",
            extra={
                "event": "stale_job_cleanup_all_orgs_completed",
                "dispatched_org_count": len(org_tasks),
                "target_org_count": len(org_ids),
            },
        )

        return {
            "status": "completed",
            "orgs_found": len(org_ids),
            "orgs_triggered": len(org_tasks),
            "org_tasks": org_tasks,
        }

    return run_async(_dispatch_all_orgs())


@app.task(
    name="deal.cleanup_stale_jobs_org",
    bind=True,  # Tier 2: WITH self binding for retry support
    max_retries=2,  # Retry up to 2 times
    default_retry_delay=60,  # Wait 60 seconds before first retry
    acks_late=True,
    soft_time_limit=300,  # 5 minutes per org
    time_limit=360,
)
def cleanup_stale_deal_jobs_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: Per-organization stale deal job cleanup.

    This task:
    1. Iterates through all deal job types
    2. For each job type, finds PENDING jobs older than threshold and marks them FAILED
    3. For each job type, finds IN_PROGRESS jobs older than threshold and marks them FAILED
    4. Returns cleanup statistics organized by job type

    Args:
        organisation_id: The organization UUID string

    Returns:
        dict with cleanup results
    """
    logger.info(
        "Starting stale deal job cleanup for organisation",
        extra={
            "event": "stale_job_cleanup_org_started",
            "organisation_id": organisation_id,
        },
    )

    try:
        org_uuid = UUID(organisation_id)
        service = DealStaleJobCleanupService(org_uuid)
        result = service.cleanup_stale_jobs()

        logger.info(
            "Stale deal job cleanup completed for organisation",
            extra={
                "event": "stale_job_cleanup_org_completed",
                "organisation_id": organisation_id,
                "stale_jobs_found": result.get("total_stale_jobs_found", 0),
                "jobs_marked_failed": result.get("total_jobs_marked_failed", 0),
            },
        )

        return {
            "status": "success",
            **result,
        }

    except Exception as exc:
        logger.error(
            "Stale deal job cleanup failed for organisation",
            extra={
                "event": "stale_job_cleanup_org_failed",
                "organisation_id": organisation_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        # Retry with exponential backoff if retries remaining
        if self.request.retries < self.max_retries:
            countdown = 60 * (2**self.request.retries)  # 60s, 120s
            logger.info(
                "Retrying stale deal job cleanup for organisation",
                extra={
                    "event": "stale_job_cleanup_org_retrying",
                    "organisation_id": organisation_id,
                    "retry_countdown_seconds": countdown,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
            )
            raise self.retry(exc=exc, countdown=countdown) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "error": str(exc),
        }
