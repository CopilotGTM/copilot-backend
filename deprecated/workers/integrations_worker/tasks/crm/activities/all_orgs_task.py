"""
Tier 1: Global CRM activities sync task.

Triggers organization-level sync for all organizations with active CRM integrations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_crm_activities_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def sync_crm_activities_all_orgs_task() -> dict[str, Any]:
    """
    Tier 1: Sync CRM activities for all approved organizations.

    Triggered by: Celery Beat schedule
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        from .org_task import sync_crm_activities_org_task

        logger.info("Starting CRM activities sync for all organizations (Tier 1)")

        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(Organisation.organisation_id).where(
                Organisation.status == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning("No approved organizations found for CRM activities sync")
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "organisations_dispatched": 0,
            }

        logger.info("Dispatching CRM activities sync to %d organization(s)", len(org_ids))

        for org_id in org_ids:
            sync_crm_activities_org_task.delay(organisation_id=str(org_id))

        return {
            "status": "dispatched",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "organisations_dispatched": len(org_ids),
        }

    return run_async(_dispatch_all_orgs())
