"""
Tier 2: Organization-level CRM contacts sync task.

Syncs all CRM contact integrations for a specific organization.
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
from src.service.integrations.crm.enrichment import CRMEnrichmentOrchestrator
from src.service.integrations.crm.entities import ContactSyncService
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.registry import get_fetcher

from ..utils import (
    CRM_INTEGRATION_TYPES,
    get_last_sync_timestamp,
    get_provider_from_integration_type,
    update_last_sync_timestamp,
)

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_crm_contacts_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
    soft_time_limit=1200,
    time_limit=1260,
)
def sync_crm_contacts_org_task(
    self: Task,
    organisation_id: str,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """Tier 2: Sync all CRM contact integrations for one organization.

    Args:
        organisation_id: The organization UUID string
        skip_enrichment: If True, skip enrichment after sync (used during initial sync)
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        logger.info("Starting CRM contacts sync for organization (Tier 2): %s", org_id)

        nango = NangoClient()
        try:
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

                integration_results = []
                successful = 0
                failed = 0
                total_records = 0

                for integration in integrations:
                    try:
                        sync_result = await _sync_contacts_for_integration(
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
                            "Failed to sync contacts for integration %s: %s",
                            integration.integration_id,
                            str(e),
                            exc_info=True,
                        )
                        failed += 1
                        integration.last_sync_status = SyncStatus.FAILED.value
                        integration.last_sync_error = str(e)[:500]
                        session.add(integration)
                        await session.commit()
                        integration_results.append(
                            {
                                "integration_id": str(integration.integration_id),
                                "integration_type": integration.integration_type.value
                                if hasattr(integration.integration_type, "value")
                                else str(integration.integration_type),
                                "status": "failed",
                                "error": str(e),
                            }
                        )

                status = (
                    "completed" if failed == 0 else ("failed" if successful == 0 else "partial")
                )

                return {
                    "status": status,
                    "organisation_id": organisation_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
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
            "CRM contacts org sync failed: org=%s error=%s", organisation_id, str(e), exc_info=True
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


async def _sync_contacts_for_integration(
    *,
    session,
    nango: NangoClient,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """Sync contacts for a single CRM integration.

    Args:
        session: Database session
        nango: NangoClient instance
        organisation_id: Organization UUID
        integration: KnowledgeIntegration instance
        skip_enrichment: If True, skip enrichment after sync
    """
    integration_id = integration.integration_id
    connection_id = integration.connection_id
    integration_type = integration.integration_type

    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)

    provider = get_provider_from_integration_type(integration_type)
    provider_config_key = to_nango_provider_key(integration_type)

    last_sync_at = get_last_sync_timestamp(integration, "contacts")
    is_initial = last_sync_at is None

    logger.info(
        "Syncing contacts: org=%s integration=%s provider=%s is_initial=%s",
        organisation_id,
        integration_id,
        provider,
        is_initial,
    )

    fetcher = get_fetcher("crm", provider, "contacts")
    if not fetcher:
        raise ValueError(f"No fetcher found for crm/{provider}/contacts")

    sync_start = datetime.utcnow()  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
    records = await fetcher(
        nango=nango,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_sync_at,
        data_type="contacts",
        provider_config_key=provider_config_key,
    )

    logger.info("Fetched %d contacts for integration %s", len(records), integration_id)

    contact_service = ContactSyncService(db=session, organisation_id=organisation_id)

    if is_initial:
        metrics = await contact_service.sync(
            integration=integration, records=records, provider=provider
        )
    else:
        metrics = await contact_service.sync_incremental(
            integration=integration, records=records, provider=provider
        )

    integration.last_synced_at = sync_start
    integration.last_sync_status = SyncStatus.SUCCESS.value
    integration.last_sync_error = None
    update_last_sync_timestamp(integration, "contacts", sync_start)
    attributes.flag_modified(integration, "connection_metadata")
    session.add(integration)
    await session.commit()

    # Run enrichment after sync (unless skipped for initial sync)
    enrichment_metrics = {}
    if not skip_enrichment:
        orchestrator = CRMEnrichmentOrchestrator(
            db=session,
            organisation_id=organisation_id,
            integration=integration,
        )
        enrichment_metrics = await orchestrator.enrich_after_contacts_sync()
        # Commit enrichment changes
        await session.commit()
        logger.info(
            "Contact enrichment complete: org=%s integration=%s metrics=%s",
            organisation_id,
            integration_id,
            enrichment_metrics,
        )

    return {
        "integration_id": str(integration_id),
        "integration_type": integration_type.value,
        "status": "success",
        "records_fetched": len(records),
        "records_synced": metrics.get("contacts", len(records)),
        "is_initial": is_initial,
        "metrics": metrics,
        "enrichment_metrics": enrichment_metrics,
    }
