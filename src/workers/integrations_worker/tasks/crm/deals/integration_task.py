"""
Tier 3: Integration-level CRM deals sync task.

For manual/on-demand sync of a single CRM integration's deals.
NOT used by scheduled sync (Celery Beat).

Use cases:
- User clicks "Refresh" button on frontend
- Admin manually triggers sync for debugging
- API endpoint for on-demand sync
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlalchemy.orm import attributes

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm.entities import DealSyncService
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.registry import get_fetcher

from ..utils import (
    get_last_sync_timestamp,
    get_provider_from_integration_type,
    get_sync_data_type,
    update_last_sync_timestamp,
)

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
) -> dict[str, Any]:
    """
    Tier 3: Sync CRM deals for a specific integration.

    Triggered by: Frontend refresh button, API, manual intervention
    NOT triggered by: Scheduled sync (Tier 1/2)

    Args:
        organisation_id: The organization UUID string
        integration_id: The integration UUID string
        force_full_sync: If True, ignores last_sync_at and does full sync

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
            "metrics": {...}
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync deals for the specific integration."""
        logger.info(
            "Starting CRM deals sync for integration (Tier 3): org=%s integration=%s force_full=%s",
            org_id,
            int_id,
            force_full_sync,
        )

        nango = NangoClient()
        try:
            async for session in acquire_org_async_session(org_id):
                # Query the specific integration
                stmt = select(KnowledgeIntegration).where(
                    KnowledgeIntegration.integration_id == int_id,
                    KnowledgeIntegration.is_active == True,  # noqa: E712
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

                integration_type = integration.integration_type
                if isinstance(integration_type, str):
                    integration_type = IntegrationType(integration_type)

                provider = get_provider_from_integration_type(integration_type)
                provider_config_key = to_nango_provider_key(integration_type)
                data_type = get_sync_data_type("deals", provider)

                # Get last sync timestamp (skip if force_full_sync)
                last_sync_at = (
                    None if force_full_sync else get_last_sync_timestamp(integration, "deals")
                )
                is_initial = last_sync_at is None

                logger.info(
                    "Syncing deals: org=%s integration=%s provider=%s is_initial=%s",
                    org_id,
                    int_id,
                    provider,
                    is_initial,
                )

                # Get fetcher from registry
                fetcher = get_fetcher("crm", provider, data_type)
                if not fetcher:
                    raise ValueError(f"No fetcher found for crm/{provider}/{data_type}")

                # Fetch records
                sync_start = (
                    datetime.utcnow()
                )  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
                records = await fetcher(
                    nango=nango,
                    connection_id=integration.connection_id,
                    is_initial=is_initial,
                    last_synced_at=last_sync_at,
                    data_type=data_type,
                    provider_config_key=provider_config_key,
                )

                logger.info(
                    "Fetched %d deals for integration %s",
                    len(records),
                    int_id,
                )

                # Sync to database using DealSyncService
                deal_service = DealSyncService(db=session, organisation_id=org_id)

                if is_initial:
                    metrics = await deal_service.sync(
                        integration=integration,
                        records=records,
                        provider=provider,
                    )
                else:
                    metrics = await deal_service.sync_incremental(
                        integration=integration,
                        records=records,
                        provider=provider,
                    )

                # Update integration status (SUCCESS)
                integration.last_synced_at = sync_start
                integration.last_sync_status = SyncStatus.SUCCESS.value
                integration.last_sync_error = None

                # Update entity-specific timestamp in connection_metadata
                update_last_sync_timestamp(integration, "deals", sync_start)

                # Flag connection_metadata as modified for SQLAlchemy to persist
                attributes.flag_modified(integration, "connection_metadata")

                session.add(integration)
                await session.commit()

                completed_at = datetime.now(UTC).isoformat()

                return {
                    "status": "success",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "integration_type": integration_type.value,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "records_fetched": len(records),
                    "records_synced": metrics.get("deals", len(records)),
                    "is_initial": is_initial,
                    "metrics": metrics,
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
