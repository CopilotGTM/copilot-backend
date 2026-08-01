"""Delayed-work recovery tasks hosted by the chat worker.

Two-tier task pattern:
- Tier 1: cleanup_delayed_work_all_orgs_task - Coordinator, dispatches to all orgs
- Tier 2: cleanup_delayed_work_org_task - Per-org cleanup with retry support

Runs every minute via Celery Beat. Recovers delayed asynchronous work:
- Attachment processing whose post-upload task was lost (>12min)
- Stuck TRIGGERED reminders (>30min), reset to ACTIVE or FAILED
- Stale UserReminderRuns (IN_PROGRESS >30min)
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
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services.stale_job_cleanup_service import (
    ChatStaleJobCleanupService,
)

logger = get_logger("chat_stale_job_cleanup_tasks")


@app.task(
    name="chat.cleanup_stale_jobs_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    ignore_result=True,
    soft_time_limit=300,
    time_limit=360,
)
def cleanup_delayed_work_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Coordinator task that dispatches cleanup to all approved organizations.

    Scheduled every minute for delayed workflows where recovery remains useful.
    """
    logger.info("Starting delayed-work cleanup coordinator for all organizations")

    async def _dispatch_all_orgs() -> dict[str, Any]:
        org_ids: list[UUID] = []

        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.info("No approved organizations found")
            return {
                "status": "completed",
                "orgs_found": 0,
                "orgs_triggered": 0,
                "org_tasks": [],
            }

        logger.info("Found %d approved organizations, dispatching cleanup tasks", len(org_ids))

        org_tasks = []
        for org_id in org_ids:
            try:
                # Keep coordinator and per-org cleanup logs under the same request/task lineage.
                task = cleanup_delayed_work_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    queue="chat_processing",
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
                    "Failed to dispatch delayed-work cleanup for org %s: %s",
                    org_id,
                    e,
                )

        logger.info(
            "Delayed-work cleanup coordinator completed: dispatched %d/%d org tasks",
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
    name="chat.cleanup_stale_jobs_org",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    ignore_result=True,
    soft_time_limit=300,
    time_limit=360,
)
def cleanup_delayed_work_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: Per-organization attachment and reminder recovery.

    Args:
        organisation_id: The organization UUID string
    """
    logger.info("Starting delayed-work cleanup for org=%s", organisation_id)

    try:
        org_uuid = UUID(organisation_id)
        service = ChatStaleJobCleanupService(org_uuid)
        result = service.cleanup_stale_jobs()

        logger.info(
            "Delayed-work cleanup completed for org=%s: "
            "attachments_requeued=%d, attachment_requeue_failed=%d, "
            "stuck_reminders=%d, stale_runs=%d",
            organisation_id,
            result.get("attachments_requeued", 0),
            result.get("attachment_requeue_failed", 0),
            result.get("stuck_triggered_reminders", 0),
            result.get("stale_reminder_runs", 0),
        )

        return {
            "status": "success",
            **result,
        }

    except Exception as exc:
        logger.error(
            "Delayed-work cleanup failed for org=%s: %s",
            organisation_id,
            exc,
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            countdown = 60 * (2**self.request.retries)
            logger.info(
                "Retrying delayed-work cleanup for org=%s in %d seconds (attempt %d/%d)",
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
