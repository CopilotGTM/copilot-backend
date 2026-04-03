"""
Tier 1: Global deal messages sync task.

Triggered by Celery Beat to sync messages from deal-linked channels for ALL approved organizations.

This task is a coordinator - it doesn't perform actual sync work.
Instead, it:
1. Queries all approved organizations from master DB
2. Dispatches Tier 2 tasks (sync_deal_messages_org_task) for each organization
3. Returns summary of dispatched tasks

Architecture:
    Celery Beat (every 30 minutes)
        |
    sync_deal_messages_all_orgs_task [Tier 1 - this file]
        |
    sync_deal_messages_org_task [Tier 2] (one per org, async dispatch)
        |
    DealChannelMessagesService (fetches messages from deal-linked channels)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_deal_messages_all_orgs",
    bind=False,
    max_retries=0,  # Coordinator doesn't retry - individual org tasks retry
    acks_late=True,
)
def sync_deal_messages_all_orgs_task() -> dict[str, Any]:
    """
    Tier 1: Sync deal-linked channel messages for all approved organizations.

    Triggered by: Celery Beat (scheduled every 30 minutes)
    Triggers: sync_deal_messages_org_task for each approved organization

    This is a coordinator task:
    - Queries master DB for all approved organizations
    - Dispatches async Tier 2 tasks for each org
    - Returns summary of what was dispatched

    Returns:
        Dictionary with dispatch summary:
        {
            "status": "completed",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "total_orgs": 50,
                "orgs_triggered": 48,
                "orgs_skipped": 2
            },
            "org_tasks": [
                {"org_id": "uuid", "task_id": "celery-task-id", "status": "triggered"},
                ...
            ]
        }
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        """Query orgs and dispatch Tier 2 tasks."""
        # Import here to avoid circular imports
        from .deal_messages_org_task import sync_deal_messages_org_task

        logger.info("Starting deal messages sync for all organizations (Tier 1)")

        # Query all approved organizations
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning("No approved organizations found for deal messages sync")
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_orgs": 0,
                    "orgs_triggered": 0,
                    "orgs_skipped": 0,
                },
                "org_tasks": [],
            }

        logger.info("Found %d approved organization(s) for deal messages sync", len(org_ids))

        # Dispatch Tier 2 task for each organization
        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                task = sync_deal_messages_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    headers=correlation_headers(),
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "task_id": task.id,
                        "status": "triggered",
                    }
                )
                orgs_triggered += 1
                logger.debug(
                    "Dispatched deal messages sync for org: %s (task_id=%s)",
                    org_id,
                    task.id,
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch deal messages sync for org %s: %s",
                    org_id,
                    str(e),
                    exc_info=True,
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "status": "failed",
                        "error": str(e),
                    }
                )
                orgs_skipped += 1

        completed_at = datetime.now(UTC).isoformat()

        logger.info(
            "Deal messages sync dispatch complete: triggered=%d skipped=%d total=%d",
            orgs_triggered,
            orgs_skipped,
            len(org_ids),
        )

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": completed_at,
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
        logger.error(
            "Deal messages all-orgs sync failed: error=%s",
            str(e),
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
