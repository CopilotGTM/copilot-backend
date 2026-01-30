"""
Tier 2: Organization-level CRM users sync task.

Syncs all CRM user integrations for a specific organization.

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query integrations, extract needed data, release session
    2. For each integration:
       a. Fetch users from API (NO session held)
       b. Session N: Write users to DB, update integration, release session
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlalchemy.orm import attributes

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm.enrichment import CRMEnrichmentOrchestrator
from src.service.integrations.crm.entities import UserSyncService
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.registry import get_fetcher

from ..utils import (
    CRM_INTEGRATION_TYPES,
    get_provider_from_integration_type,
    update_last_sync_timestamp,
)

logger = get_logger(__name__)


@dataclass
class IntegrationFetchData:
    """Data extracted from integration needed for fetching users."""

    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str


@app.task(
    name="integrations.sync_crm_users_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
)
def sync_crm_users_org_task(
    self: Task,
    organisation_id: str,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """Tier 2: Sync all CRM user integrations for one organization.

    Args:
        organisation_id: The organization UUID string
        skip_enrichment: If True, skip enrichment after sync (used during initial sync)
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync CRM users for the organization.

        Uses fresh session pattern:
        1. Session 1: Query integrations, extract data, release session
        2. For each integration:
           a. Fetch users from API (NO session held)
           b. Session N: Write users to DB, update integration, release session
        """
        logger.info("Starting CRM users sync for organization (Tier 2): %s", org_id)

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query integrations and extract data needed for fetching
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        integration_fetch_list: list[IntegrationFetchData] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_type.in_(CRM_INTEGRATION_TYPES),
                KnowledgeIntegration.is_active == True,  # noqa: E712
                KnowledgeIntegration.connection_id.isnot(None),
            )
            result = await session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
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

            # Extract data needed for fetching
            for integration in integrations:
                int_type = integration.integration_type
                if isinstance(int_type, str):
                    int_type = IntegrationType(int_type)

                provider = get_provider_from_integration_type(int_type)
                provider_config_key = to_nango_provider_key(int_type)

                integration_fetch_list.append(
                    IntegrationFetchData(
                        integration_id=integration.integration_id,
                        connection_id=integration.connection_id,
                        integration_type=int_type,
                        integration_type_value=int_type.value,
                        provider=provider,
                        provider_config_key=provider_config_key,
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
        # Phase 2 & 3: For each integration, fetch users then persist
        # External API calls happen WITHOUT holding a database session
        # ─────────────────────────────────────────────────────────────────────
        integration_results = []
        successful = 0
        failed = 0
        total_records = 0

        nango = NangoClient()
        try:
            for fetch_data in integration_fetch_list:
                try:
                    sync_result = await _sync_users_for_integration_fresh(
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
                        "Failed to sync users for integration %s: %s",
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

        status = "completed" if failed == 0 else ("failed" if successful == 0 else "partial")

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
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
            "CRM users org sync failed: org=%s error=%s", organisation_id, str(e), exc_info=True
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e) from e
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


async def _sync_users_for_integration_fresh(
    *,
    nango: NangoClient,
    organisation_id: UUID,
    fetch_data: IntegrationFetchData,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """Sync users for a single CRM integration using fresh session pattern.

    Users are reference data that rarely changes, so we always do a full sync.

    Flow:
    1. Fetch users from API (NO session held)
    2. Acquire fresh session, write users to DB
    3. Update integration metadata
    4. Run enrichment if needed

    Args:
        nango: NangoClient instance
        organisation_id: Organization UUID
        fetch_data: Pre-extracted integration data
        skip_enrichment: If True, skip enrichment after sync
    """
    logger.info(
        "Syncing users: org=%s integration=%s provider=%s",
        organisation_id,
        fetch_data.integration_id,
        fetch_data.provider,
    )

    fetcher = get_fetcher("crm", fetch_data.provider, "users")
    if not fetcher:
        raise ValueError(f"No fetcher found for crm/{fetch_data.provider}/users")

    # ─────────────────────────────────────────────────────────────────────
    # Phase 2: Fetch users from API (NO session held)
    # ─────────────────────────────────────────────────────────────────────
    sync_start = datetime.utcnow()  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
    records = await fetcher(
        nango=nango,
        connection_id=fetch_data.connection_id,
        is_initial=True,
        last_synced_at=None,
        data_type="users",
        provider_config_key=fetch_data.provider_config_key,
    )

    logger.info("Fetched %d users for integration %s", len(records), fetch_data.integration_id)

    # ─────────────────────────────────────────────────────────────────────
    # Phase 3: Write users to DB and update integration (fresh session)
    # ─────────────────────────────────────────────────────────────────────
    metrics: dict[str, Any] = {}
    enrichment_metrics: dict[str, Any] = {}

    async for session in acquire_org_async_session(organisation_id):
        # Reload integration for this session
        stmt = select(KnowledgeIntegration).where(
            KnowledgeIntegration.integration_id == fetch_data.integration_id,
        )
        result = await session.execute(stmt)
        integration = result.scalar_one_or_none()

        if not integration:
            raise ValueError(f"Integration {fetch_data.integration_id} not found")

        # Create sync history record
        sync_history = IntegrationSyncHistory(
            integration_id=fetch_data.integration_id,
            started_at=sync_start,
            status=SyncStatus.IN_PROGRESS,
        )
        session.add(sync_history)
        await session.commit()
        await session.refresh(sync_history)

        records_processed = 0
        records_failed = 0

        try:
            # Sync users to DB
            user_service = UserSyncService(db=session, organisation_id=organisation_id)
            metrics = await user_service.sync(
                integration=integration,
                records=records,
                provider=fetch_data.provider,
            )

            records_processed = metrics.get("users", len(records))
            records_failed = metrics.get("failed", 0)

            # Update integration metadata
            integration.last_synced_at = sync_start
            integration.last_sync_status = SyncStatus.SUCCESS.value
            integration.last_sync_error = None
            update_last_sync_timestamp(integration, "users", sync_start)
            attributes.flag_modified(integration, "connection_metadata")
            session.add(integration)

            # Complete sync history - success
            sync_history.completed_at = datetime.utcnow()
            sync_history.status = SyncStatus.SUCCESS
            sync_history.records_processed = records_processed
            sync_history.records_failed = records_failed
            sync_history.sync_metadata = {
                "entity": "users",
                "provider": fetch_data.provider,
                "is_initial": True,
                "records_fetched": len(records),
            }
            session.add(sync_history)
            await session.commit()

        except Exception as e:
            # Complete sync history - failed
            sync_history.completed_at = datetime.utcnow()
            sync_history.status = SyncStatus.FAILED
            sync_history.records_processed = records_processed
            sync_history.records_failed = records_failed
            sync_history.error_details = {"error": str(e)[:500]}
            sync_history.sync_metadata = {
                "entity": "users",
                "provider": fetch_data.provider,
                "is_initial": True,
                "records_fetched": len(records),
            }
            session.add(sync_history)
            await session.commit()
            raise

        # Run enrichment after sync (unless skipped for initial sync)
        if not skip_enrichment:
            orchestrator = CRMEnrichmentOrchestrator(
                db=session,
                organisation_id=organisation_id,
                integration=integration,
            )
            enrichment_metrics = await orchestrator.enrich_after_users_sync()
            # Commit enrichment changes
            await session.commit()
            logger.info(
                "User enrichment complete: org=%s integration=%s metrics=%s",
                organisation_id,
                fetch_data.integration_id,
                enrichment_metrics,
            )
    # Session is now released

    return {
        "integration_id": str(fetch_data.integration_id),
        "integration_type": fetch_data.integration_type_value,
        "status": "success",
        "records_fetched": len(records),
        "records_synced": metrics.get("users", len(records)),
        "metrics": metrics,
        "enrichment_metrics": enrichment_metrics,
    }
