"""
Tier 2: Organization-level CRM deals sync task.

Syncs all CRM deal integrations for a specific organization.

This task:
1. Queries org DB for all active CRM integrations
2. For each integration, calls CRMDealSyncService.sync_integration()
3. Returns org-level aggregated results

Architecture:
    sync_crm_deals_all_orgs_task [Tier 1]
        |
    sync_crm_deals_org_task [Tier 2 - this file] (one per org)
        |
    CRMDealSyncService.sync_integration() (for each integration)

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query integrations, extract needed data, release session
    2. For each integration:
       a. Session N: Acquire fresh session, sync via CRMDealSyncService, release session
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm_deal_sync_service import CRMDealSyncService
from src.workers.integrations_worker.celery_app import app

from ..utils import CRM_INTEGRATION_TYPES

logger = get_logger(__name__)


@dataclass
class IntegrationFetchData:
    """Data extracted from integration needed for syncing deals."""

    integration_id: UUID
    integration_type_value: str


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
    and syncs deals using CRMDealSyncService.

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
        """Sync all CRM deal integrations for the organization.

        Uses fresh session pattern:
        1. Session 1: Query integrations, extract data, release session
        2. For each integration:
           a. Session N: Acquire fresh session, sync via CRMDealSyncService, release session
        """
        logger.info("Starting CRM deals sync for organization (Tier 2): %s", org_id)

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query integrations and extract data needed for syncing
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        integration_fetch_list: list[IntegrationFetchData] = []

        async for session in acquire_org_async_session(org_id):
            # Query all active CRM integrations
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_type.in_(CRM_INTEGRATION_TYPES),
                KnowledgeIntegration.is_active == True,  # noqa: E712
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

            # Extract data needed for syncing
            for integration in integrations:
                int_type = integration.integration_type
                if isinstance(int_type, str):
                    int_type = IntegrationType(int_type)

                integration_fetch_list.append(
                    IntegrationFetchData(
                        integration_id=integration.integration_id,
                        integration_type_value=int_type.value,
                    )
                )
        # Session is now released

        if not integration_fetch_list:
            return {
                "status": "failed",
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": "Failed to acquire database session",
            }

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: For each integration, sync with fresh session
        # Each integration gets its own session to avoid holding connections
        # ─────────────────────────────────────────────────────────────────────
        integration_results = []
        successful = 0
        failed = 0
        total_records = 0

        nango = NangoClient()
        try:
            for fetch_data in integration_fetch_list:
                try:
                    sync_result = await _sync_deals_for_integration_fresh(
                        nango=nango,
                        organisation_id=org_id,
                        fetch_data=fetch_data,
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
                        fetch_data.integration_id,
                        str(e),
                        exc_info=True,
                    )
                    failed += 1

                    # Update integration status with fresh session
                    async for session in acquire_org_async_session(org_id):
                        stmt = select(KnowledgeIntegration).where(
                            KnowledgeIntegration.integration_id == fetch_data.integration_id,
                        )
                        result = await session.execute(stmt)
                        integration = result.scalar_one_or_none()
                        if integration:
                            integration.last_sync_status = SyncStatus.FAILED.value
                            integration.last_sync_error = str(e)[:500]
                            session.add(integration)
                            await session.commit()

                    integration_results.append(
                        {
                            "integration_id": str(fetch_data.integration_id),
                            "integration_type": fetch_data.integration_type_value,
                            "status": "failed",
                            "error": str(e),
                        }
                    )
        finally:
            await nango.close()

        completed_at = datetime.now(UTC).isoformat()

        # Determine overall status
        status = "completed" if failed == 0 else ("failed" if successful == 0 else "partial")

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
                "total_integrations": len(integration_fetch_list),
                "successful": successful,
                "failed": failed,
                "total_records_synced": total_records,
            },
            "integration_results": integration_results,
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


async def _sync_deals_for_integration_fresh(
    *,
    nango: NangoClient,
    organisation_id: UUID,
    fetch_data: IntegrationFetchData,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """Sync deals for a single CRM integration using fresh session pattern.

    Acquires a fresh session for each integration sync to avoid holding
    connections across multiple integrations.

    Args:
        nango: NangoClient instance
        organisation_id: Organization UUID
        fetch_data: Pre-extracted integration data
        skip_enrichment: If True, skip enrichment after sync
    """
    async for session in acquire_org_async_session(organisation_id):
        # Reload integration for this session
        stmt = select(KnowledgeIntegration).where(
            KnowledgeIntegration.integration_id == fetch_data.integration_id,
        )
        result = await session.execute(stmt)
        integration = result.scalar_one_or_none()

        if not integration:
            raise ValueError(f"Integration {fetch_data.integration_id} not found")

        # Use the service to sync
        deal_sync_service = CRMDealSyncService(
            session=session,
            organisation_id=organisation_id,
        )

        sync_result = await deal_sync_service.sync_integration(
            integration=integration,
            nango=nango,
            skip_enrichment=skip_enrichment,
        )

        return sync_result
    # Session is now released

    raise ValueError("Failed to acquire database session")
