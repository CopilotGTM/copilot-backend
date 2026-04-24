"""Stale job cleanup tasks for knowledge worker.

Three-tier architecture:
- Tier 1: cleanup_stale_jobs_all_orgs_task - Coordinator, dispatches to all orgs
- Tier 2: cleanup_stale_jobs_org_task - Per-org cleanup with retry support

Runs every 30 minutes via Celery Beat. Cleans up:
- Stale sources (PENDING_UPLOAD >24h, PROCESSING >2h, EXTRACTING >3h)
- Orphaned sources (active status but all jobs already terminal)
- Stale jobs (PENDING/IN_PROGRESS >2h) with source status sync
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
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.services.stale_job_cleanup_service import (
    StaleJobCleanupService,
)

logger = get_logger("stale_job_cleanup_tasks")


@app.task(
    name="knowledge.cleanup_stale_jobs_all_orgs",
    bind=False,  # Tier 1: No self binding, never retries
    max_retries=0,  # CRITICAL: Don't retry coordinator to avoid duplicate dispatches
    acks_late=True,
    soft_time_limit=300,  # 5 minutes (coordinator is fast)
    time_limit=360,
)
def cleanup_stale_jobs_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Coordinator task that dispatches cleanup to all approved organizations.

    This task:
    1. Queries all approved organizations from master schema
    2. Dispatches Tier 2 task for each organization
    3. Returns summary of dispatched tasks

    Scheduled to run every 2 minutes via Celery Beat.
    """
    logger.info("Starting stale job cleanup coordinator for all organizations")

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
            logger.info("No approved organizations found")
            return {
                "status": "completed",
                "orgs_found": 0,
                "orgs_triggered": 0,
                "org_tasks": [],
            }

        logger.info("Found %d approved organizations, dispatching cleanup tasks", len(org_ids))

        # Dispatch Tier 2 for each organization
        org_tasks = []
        for org_id in org_ids:
            try:
                # Preserve coordinator lineage into each org-level cleanup run.
                task = cleanup_stale_jobs_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    queue="knowledge_upload",
                    headers=correlation_headers(),
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "task_id": task.id,
                    }
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch stale job cleanup for org %s: %s",
                    org_id,
                    e,
                )

        logger.info(
            "Stale job cleanup coordinator completed: dispatched %d/%d org tasks",
            len(org_tasks),
            len(org_ids),
        )

        return {
            "status": "completed",
            "orgs_found": len(org_ids),
            "orgs_triggered": len(org_tasks),
            "org_tasks": org_tasks,
        }

    return run_async(_dispatch_all_orgs())


@app.task(
    name="knowledge.cleanup_stale_jobs_org",
    bind=True,  # Tier 2: WITH self binding for retry support
    max_retries=2,  # Retry up to 2 times
    default_retry_delay=60,  # Wait 60 seconds before first retry
    acks_late=True,
    soft_time_limit=300,  # 5 minutes per org
    time_limit=360,
)
def cleanup_stale_jobs_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: Per-organization stale source and job cleanup.

    This task:
    1. Marks stale sources (PENDING_UPLOAD/PROCESSING/EXTRACTING) as FAILED
    2. Fixes orphaned sources (active status but all jobs terminal)
    3. Safety-net: marks stale jobs as FAILED and syncs parent source

    Args:
        organisation_id: The organization UUID string

    Returns:
        dict with cleanup results
    """
    logger.info("Starting stale job cleanup for org=%s", organisation_id)

    try:
        org_uuid = UUID(organisation_id)
        service = StaleJobCleanupService(org_uuid)
        result = service.cleanup_stale_jobs()

        logger.info(
            "Stale cleanup completed for org=%s: stale_sources=%d, orphaned=%d, stale_jobs=%d",
            organisation_id,
            result.get("stale_sources", 0),
            result.get("orphaned_sources", 0),
            result.get("stale_jobs", 0),
        )

        return {
            "status": "success",
            **result,
        }

    except Exception as exc:
        logger.error(
            "Stale job cleanup failed for org=%s: %s",
            organisation_id,
            exc,
            exc_info=True,
        )

        # Retry with exponential backoff if retries remaining
        if self.request.retries < self.max_retries:
            countdown = 60 * (2**self.request.retries)  # 60s, 120s
            logger.info(
                "Retrying stale job cleanup for org=%s in %d seconds (attempt %d/%d)",
                organisation_id,
                countdown,
                self.request.retries + 1,
                self.max_retries,
            )
            raise self.retry(exc=exc, countdown=countdown) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "error": str(exc),
        }
