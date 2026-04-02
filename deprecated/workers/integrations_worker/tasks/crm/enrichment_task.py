"""
CRM enrichment task for initial sync.

This task runs enrich_all() after all CRM entity syncs complete during initial sync.
It's dispatched by the webhook service when a CRM integration connects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.enrichment import CRMEnrichmentOrchestrator
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


def _trigger_stakeholder_sync(organisation_id: str) -> bool:
    """Trigger org-level stakeholder sync after CRM initial sync.

    After CRM initial sync completes, we need to sync stakeholders for all deals
    so that calendar/email matching can work (matching depends on stakeholder lookup).

    Args:
        organisation_id: The organization UUID string

    Returns:
        True if task was dispatched successfully, False otherwise
    """
    try:
        app.send_task(
            "stakeholder.sync_all_deals_stakeholders_org",
            args=[organisation_id],
            queue="deal",
        )
        logger.info(
            "Triggered org-level stakeholder sync after CRM initial sync: org=%s",
            organisation_id,
        )
        return True
    except Exception as e:
        logger.warning(
            "Failed to trigger stakeholder sync: org=%s error=%s",
            organisation_id,
            str(e),
        )
        return False


@app.task(
    name="integrations.enrich_crm_after_initial_sync",
    bind=True,
    max_retries=3,
    default_retry_delay=300,  # 5 minutes - give sync tasks time to complete
    acks_late=True,
    soft_time_limit=1800,  # 30 minutes
    time_limit=1860,  # 31 minutes
)
def enrich_crm_after_initial_sync_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """
    Run full CRM enrichment after initial sync completes.

    This task is dispatched by the webhook service after triggering all CRM
    entity sync tasks with skip_enrichment=True. It runs enrich_all() to
    handle all enrichments in the correct order.

    Args:
        organisation_id: The organization UUID string
        integration_id: The integration UUID string

    Returns:
        Dictionary with enrichment results
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _enrich() -> dict[str, Any]:
        logger.info(
            "Starting CRM enrichment after initial sync: org=%s integration=%s",
            org_id,
            int_id,
        )

        async for session in acquire_org_async_session(org_id):
            # Fetch the integration
            stmt = select(KnowledgeIntegration).where(KnowledgeIntegration.integration_id == int_id)
            result = await session.execute(stmt)
            integration = result.scalars().first()

            if not integration:
                logger.error("Integration not found: %s", int_id)
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration not found",
                }

            if not integration.is_active:
                logger.warning("Integration is inactive, skipping enrichment: %s", int_id)
                return {
                    "status": "skipped",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "reason": "Integration inactive",
                }

            # Run full enrichment
            orchestrator = CRMEnrichmentOrchestrator(
                db=session,
                organisation_id=org_id,
                integration=integration,
            )

            enrichment_result = await orchestrator.enrich_all()

            # Commit enrichment changes
            await session.commit()

            logger.info(
                "CRM enrichment complete: org=%s integration=%s results=%s",
                org_id,
                int_id,
                enrichment_result,
            )

            # Trigger org-level stakeholder sync after CRM initial sync.
            # This ensures all deals have their stakeholders synced, which is required
            # for calendar/email matching to work (matching uses stakeholder lookup).
            _trigger_stakeholder_sync(organisation_id)

            return {
                "status": "completed",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "enrichment_results": enrichment_result,
            }

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_enrich())
    except Exception as e:
        logger.error(
            "CRM enrichment failed: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying CRM enrichment: org=%s integration=%s attempt=%d/%d",
                organisation_id,
                integration_id,
                self.request.retries + 1,
                self.max_retries,
            )
            raise self.retry(exc=e) from e

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
