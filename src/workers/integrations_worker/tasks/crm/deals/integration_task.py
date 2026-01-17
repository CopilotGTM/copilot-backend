"""
Tier 3: Integration-level CRM deals sync task.

For manual/on-demand sync of a single CRM integration's deals.
NOT used by scheduled sync (Celery Beat).

Use cases:
- User clicks "Refresh" button on frontend
- Admin manually triggers sync for debugging
- API endpoint for on-demand sync
- Initial sync triggered on auth webhook

This task uses CRMDealSyncService.sync_integration() - same as org_task.py
to ensure consistent behavior with scheduled sync (Tier 2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm_deal_sync_service import CRMDealSyncService
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_crm_deals_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute between retries
    acks_late=True,
    soft_time_limit=600,  # 10 minutes soft limit
    time_limit=660,  # 11 minutes hard limit
)
def sync_crm_deals_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
    force_full_sync: bool = False,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """
    Tier 3: Sync CRM deals for a specific integration.

    Triggered by: Frontend refresh button, API, manual intervention, initial sync on auth
    NOT triggered by: Scheduled sync (Tier 1/2)

    This task uses CRMDealSyncService.sync_integration() - same as org_task.py
    to ensure consistent behavior between scheduled and manual syncs.

    Args:
        organisation_id: The organization UUID string
        integration_id: The integration UUID string
        force_full_sync: If True, ignores last_sync_at and does full sync
        skip_enrichment: If True, skip enrichment after sync (used during initial sync)

    Returns:
        Dictionary with integration sync result:
        {
            "status": "success",
            "organisation_id": "uuid",
            "integration_id": "uuid",
            "integration_type": "hubspot",
            "started_at": "...",
            "completed_at": "...",
            "records_fetched": 150,
            "records_synced": 150,
            "is_initial": false,
            "metrics": {...},
            "enrichment_metrics": {...},
            "triggered_syncs": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync deals for the specific integration."""
        logger.info(
            "Starting CRM deals sync for integration (Tier 3): org=%s integration=%s "
            "force_full=%s skip_enrichment=%s",
            org_id,
            int_id,
            force_full_sync,
            skip_enrichment,
        )

        nango = NangoClient()
        try:
            async for session in acquire_org_async_session(org_id):
                # Query the specific integration
                stmt = select(KnowledgeIntegration).where(
                    KnowledgeIntegration.integration_id == int_id,  # type: ignore
                    KnowledgeIntegration.is_active,  # type: ignore
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()

                if not integration:
                    logger.error(
                        "Integration not found or inactive: org=%s integration=%s",
                        org_id,
                        int_id,
                    )
                    return {
                        "status": "failed",
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "error": "Integration not found or inactive",
                    }

                if not integration.connection_id:
                    logger.error(
                        "Integration has no connection_id: org=%s integration=%s",
                        org_id,
                        int_id,
                    )
                    return {
                        "status": "failed",
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "error": "Integration not connected",
                    }

                # Use the service directly
                try:
                    deal_sync_service = CRMDealSyncService(
                        session=session,
                        organisation_id=org_id,
                    )

                    sync_result = await deal_sync_service.sync_integration(
                        integration=integration,
                        nango=nango,
                        skip_enrichment=skip_enrichment,
                        force_full_sync=force_full_sync,
                    )

                    # Add task-level metadata to result
                    sync_result["started_at"] = started_at
                    sync_result["completed_at"] = datetime.now(UTC).isoformat()
                    sync_result["organisation_id"] = organisation_id

                    return sync_result

                except Exception as sync_error:
                    logger.error(
                        "Sync service failed: org=%s integration=%s error=%s",
                        org_id,
                        int_id,
                        str(sync_error),
                        exc_info=True,
                    )
                    # Update integration status to failed
                    integration.last_sync_status = SyncStatus.FAILED.value
                    integration.last_sync_error = str(sync_error)[:500]
                    session.add(integration)
                    await session.commit()

                    return {
                        "status": "failed",
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "error": str(sync_error),
                    }

        finally:
            await nango.close()

        # If async for didn't yield any sessions
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_sync_integration())
    except Exception as e:
        logger.error(
            "CRM deals integration sync failed: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying CRM deals integration sync: org=%s integration=%s attempt=%d/%d",
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
