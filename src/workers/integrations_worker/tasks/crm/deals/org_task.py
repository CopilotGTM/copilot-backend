"""
Tier 2: Organization-level CRM deals sync task.

Syncs all CRM deal integrations for a specific organization.

This task:
1. Queries org DB for all active CRM integrations
2. For each integration, fetches deals and syncs using DealSyncService
3. Updates integration status after sync
4. Returns org-level aggregated results

Architecture:
    sync_crm_deals_all_orgs_task [Tier 1]
        |
    sync_crm_deals_org_task [Tier 2 - this file] (one per org)
        |
    DealSyncService (for each integration, inline)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.workers.integrations_worker.celery_app import app

from ..utils import CRM_INTEGRATION_TYPES
from .sync_helper import sync_deals_for_integration

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_crm_deals_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,  # 2 minutes between retries
    acks_late=True,
    soft_time_limit=1200,  # 20 minutes soft limit
    time_limit=1260,  # 21 minutes hard limit
)
def sync_crm_deals_org_task(
    self: Task,
    organisation_id: str,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """
    Tier 2: Sync all CRM deal integrations for one organization.

    Triggered by: sync_crm_deals_all_orgs_task (Tier 1)

    This task loops through all active CRM integrations for the org
    and syncs deals using DealSyncService.

    Args:
        organisation_id: The organization UUID string
        skip_enrichment: If True, skip enrichment after sync (used during initial sync)

    Returns:
        Dictionary with org-level sync results:
        {
            "status": "completed",
            "organisation_id": "uuid",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "total_integrations": 2,
                "successful": 2,
                "failed": 0,
                "total_records_synced": 150
            },
            "integration_results": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync all CRM deal integrations for the organization."""
        logger.info("Starting CRM deals sync for organization (Tier 2): %s", org_id)

        nango = NangoClient()
        try:
            async for session in acquire_org_async_session(org_id):
                # Query all active CRM integrations
                stmt = select(KnowledgeIntegration).where(
                    KnowledgeIntegration.integration_type.in_(CRM_INTEGRATION_TYPES),
                    KnowledgeIntegration.is_active == True,  # noqa: E712
                    KnowledgeIntegration.connection_id.isnot(None),
                )
                result = await session.execute(stmt)
                integrations = result.scalars().all()

                if not integrations:
                    logger.info("No active CRM integrations for org: %s", org_id)
                    return {
                        "status": "completed",
                        "organisation_id": organisation_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "summary": {
                            "total_integrations": 0,
                            "successful": 0,
                            "failed": 0,
                            "total_records_synced": 0,
                        },
                        "integration_results": [],
                    }

                logger.info(
                    "Found %d active CRM integration(s) for org: %s",
                    len(integrations),
                    org_id,
                )

                # Sync each integration
                integration_results = []
                successful = 0
                failed = 0
                total_records = 0

                for integration in integrations:
                    try:
                        sync_result = await sync_deals_for_integration(
                            session=session,
                            nango=nango,
                            organisation_id=org_id,
                            integration=integration,
                            skip_enrichment=skip_enrichment,
                        )

                        if sync_result.get("status") == "success":
                            successful += 1
                            total_records += sync_result.get("records_synced", 0)
                        else:
                            failed += 1

                        integration_results.append(sync_result)

                    except Exception as e:
                        logger.error(
                            "Failed to sync deals for integration %s: %s",
                            integration.integration_id,
                            str(e),
                            exc_info=True,
                        )
                        failed += 1

                        # Update integration status to failed
                        integration.last_sync_status = SyncStatus.FAILED.value
                        integration.last_sync_error = str(e)[:500]
                        session.add(integration)
                        await session.commit()

                        integration_results.append(
                            {
                                "integration_id": str(integration.integration_id),
                                "integration_type": (
                                    integration.integration_type.value
                                    if hasattr(integration.integration_type, "value")
                                    else str(integration.integration_type)
                                ),
                                "status": "failed",
                                "error": str(e),
                            }
                        )

                completed_at = datetime.now(UTC).isoformat()

                # Determine overall status
                if failed == 0:
                    status = "completed"
                elif successful == 0:
                    status = "failed"
                else:
                    status = "partial"

                logger.info(
                    "CRM deals sync complete for org %s: successful=%d failed=%d records=%d",
                    org_id,
                    successful,
                    failed,
                    total_records,
                )

                return {
                    "status": status,
                    "organisation_id": organisation_id,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "summary": {
                        "total_integrations": len(integrations),
                        "successful": successful,
                        "failed": failed,
                        "total_records_synced": total_records,
                    },
                    "integration_results": integration_results,
                }
        finally:
            await nango.close()

        # If async for didn't yield any sessions
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_sync_org())
    except Exception as e:
        logger.error(
            "CRM deals org sync failed: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying CRM deals org sync: org=%s attempt=%d/%d",
                organisation_id,
                self.request.retries + 1,
                self.max_retries,
            )
            raise self.retry(exc=e) from e

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
