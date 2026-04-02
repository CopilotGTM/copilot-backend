"""
Tier 1: Global CRM pipelines sync task.

Triggered by Celery Beat to sync pipelines for ALL approved organizations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_crm_pipelines_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def sync_crm_pipelines_all_orgs_task() -> dict[str, Any]:
    """
    Tier 1: Sync CRM pipelines for all approved organizations.

    Triggered by: Celery Beat (scheduled every 3 hours)
    Triggers: sync_crm_pipelines_org_task for each approved organization
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        from .org_task import sync_crm_pipelines_org_task

        logger.info("Starting CRM pipelines sync for all organizations (Tier 1)")

        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(Organisation.organisation_id).where(
                Organisation.status == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning("No approved organizations found for CRM pipelines sync")
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {"total_orgs": 0, "orgs_triggered": 0, "orgs_skipped": 0},
                "org_tasks": [],
            }

        logger.info("Found %d approved organization(s) for CRM pipelines sync", len(org_ids))

        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                task = sync_crm_pipelines_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    headers=correlation_headers(),
                )
                org_tasks.append({"org_id": str(org_id), "task_id": task.id, "status": "triggered"})
                orgs_triggered += 1
            except Exception as e:
                logger.error("Failed to dispatch CRM pipelines sync for org %s: %s", org_id, str(e))
                org_tasks.append({"org_id": str(org_id), "status": "failed", "error": str(e)})
                orgs_skipped += 1

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": len(org_ids),
                "orgs_triggered": orgs_triggered,
                "orgs_skipped": orgs_skipped,
            },
            "org_tasks": org_tasks,
        }

    try:
        return run_async(_dispatch_all_orgs())
    except Exception as e:
        logger.error("CRM pipelines all-orgs sync failed: error=%s", str(e), exc_info=True)
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
