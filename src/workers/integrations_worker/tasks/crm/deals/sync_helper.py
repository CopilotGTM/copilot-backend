"""
Shared sync helper for CRM deals.

This module contains the core sync logic used by both:
- org_task.py (Tier 2): Called in a loop for each integration in the org
- integration_task.py (Tier 3): Called for a single integration (manual/on-demand)

CRITICAL: Both tiers MUST use this shared function to ensure consistent behavior.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.orm import attributes

from src.domain.models.organisation_tables.sync import IntegrationType
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.enrichment import CRMEnrichmentOrchestrator
from src.service.integrations.crm.entities import DealSyncService
from src.workers.integrations_worker.providers.registry import get_fetcher

from ..utils import (
    get_last_sync_timestamp,
    get_provider_from_integration_type,
    get_sync_data_type,
    update_last_sync_timestamp,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.domain.models.organisation_tables.sync import KnowledgeIntegration
    from src.service.integrations import NangoClient

logger = get_logger(__name__)


async def sync_deals_for_integration(
    *,
    session: AsyncSession,
    nango: NangoClient,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    skip_enrichment: bool = False,
    force_full_sync: bool = False,
) -> dict[str, Any]:
    """Sync deals for a single CRM integration.

    This is the shared sync function used by both Tier 2 (org_task) and Tier 3 (integration_task).
    Using a shared function ensures consistent behavior between scheduled and manual syncs.

    Args:
        session: Database session
        nango: NangoClient instance
        organisation_id: Organization UUID
        integration: KnowledgeIntegration instance
        skip_enrichment: If True, skip enrichment after sync (used during initial sync chord)
        force_full_sync: If True, ignores last_sync_at and does full sync

    Returns:
        Dictionary with sync result for this integration:
        {
            "integration_id": "uuid",
            "integration_type": "hubspot",
            "status": "success" | "failed",
            "records_fetched": 150,
            "records_synced": 150,
            "is_initial": true | false,
            "metrics": {...},
            "enrichment_metrics": {...},  # If enrichment ran
            "triggered_syncs": [...],     # If on-demand syncs were triggered
            "error": "...",               # If failed
        }
    """
    integration_id = integration.integration_id
    connection_id = integration.connection_id
    integration_type = integration.integration_type

    # Convert string to enum if needed
    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)

    provider = get_provider_from_integration_type(integration_type)
    provider_config_key = to_nango_provider_key(integration_type)
    data_type = get_sync_data_type("deals", provider)

    # Get last sync timestamp for incremental sync (skip if force_full_sync)
    last_sync_at = None if force_full_sync else get_last_sync_timestamp(integration, "deals")
    is_initial = last_sync_at is None

    logger.info(
        "Syncing deals: org=%s integration=%s provider=%s is_initial=%s last_sync_at=%s force_full=%s",
        organisation_id,
        integration_id,
        provider,
        is_initial,
        last_sync_at,
        force_full_sync,
    )

    # Get fetcher from registry
    fetcher = get_fetcher("crm", provider, data_type)
    if not fetcher:
        raise ValueError(f"No fetcher found for crm/{provider}/{data_type}")

    # Fetch records
    sync_start = datetime.utcnow()  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
    records = await fetcher(
        nango=nango,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_sync_at,
        data_type=data_type,
        provider_config_key=provider_config_key,
    )

    logger.info(
        "Fetched %d deals for integration %s",
        len(records),
        integration_id,
    )

    # Sync to database using DealSyncService
    deal_service = DealSyncService(db=session, organisation_id=organisation_id)

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

    # Run enrichment after sync (unless skipped for initial sync)
    enrichment_metrics: dict[str, Any] = {}
    triggered_syncs: list[str] = []

    if not skip_enrichment:
        orchestrator = CRMEnrichmentOrchestrator(
            db=session,
            organisation_id=organisation_id,
            integration=integration,
        )
        enrichment_metrics = await orchestrator.enrich_after_deals_sync(detect_missing=True)
        # Commit enrichment changes
        await session.commit()
        logger.info(
            "Deal enrichment complete: org=%s integration=%s metrics=%s",
            organisation_id,
            integration_id,
            enrichment_metrics,
        )

        # Trigger on-demand syncs for missing reference data
        missing_pipelines = enrichment_metrics.get("missing_pipelines", [])
        missing_users = enrichment_metrics.get("missing_users", [])

        if missing_pipelines:
            logger.info(
                "Triggering on-demand pipeline sync: org=%s integration=%s missing=%d",
                organisation_id,
                integration_id,
                len(missing_pipelines),
            )
            # Import here to avoid circular import
            from src.workers.integrations_worker.tasks.crm.pipelines.integration_task import (
                sync_crm_pipelines_integration_task,
            )

            sync_crm_pipelines_integration_task.delay(
                organisation_id=str(organisation_id),
                integration_id=str(integration_id),
            )
            triggered_syncs.append("pipelines")

        if missing_users:
            logger.info(
                "Triggering on-demand users sync: org=%s integration=%s missing=%d",
                organisation_id,
                integration_id,
                len(missing_users),
            )
            # Import here to avoid circular import
            from src.workers.integrations_worker.tasks.crm.users.integration_task import (
                sync_crm_users_integration_task,
            )

            sync_crm_users_integration_task.delay(
                organisation_id=str(organisation_id),
                integration_id=str(integration_id),
            )
            triggered_syncs.append("users")

    return {
        "integration_id": str(integration_id),
        "integration_type": integration_type.value,
        "status": "success",
        "records_fetched": len(records),
        "records_synced": metrics.get("deals", len(records)),
        "is_initial": is_initial,
        "metrics": metrics,
        "enrichment_metrics": enrichment_metrics,
        "triggered_syncs": triggered_syncs,
    }
