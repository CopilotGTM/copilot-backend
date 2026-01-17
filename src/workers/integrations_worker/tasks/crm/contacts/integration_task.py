"""
Tier 3: Integration-level CRM contacts sync task.

For manual/on-demand sync of a single CRM integration's contacts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlalchemy.orm import attributes

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm.entities import ContactSyncService
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.registry import get_fetcher

from ..utils import (
    get_last_sync_timestamp,
    get_provider_from_integration_type,
    update_last_sync_timestamp,
)

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_crm_contacts_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
)
def sync_crm_contacts_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
    force_full_sync: bool = False,
) -> dict[str, Any]:
    """Tier 3: Sync CRM contacts for a specific integration."""
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        logger.info(
            "Starting CRM contacts sync for integration (Tier 3): org=%s integration=%s",
            org_id,
            int_id,
        )

        nango = NangoClient()
        try:
            async for session in acquire_org_async_session(org_id):
                stmt = select(KnowledgeIntegration).where(
                    KnowledgeIntegration.integration_id == int_id,
                    KnowledgeIntegration.is_active == True,  # noqa: E712
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()

                if not integration:
                    return {
                        "status": "failed",
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "error": "Integration not found or inactive",
                    }

                if not integration.connection_id:
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

                last_sync_at = (
                    None if force_full_sync else get_last_sync_timestamp(integration, "contacts")
                )
                is_initial = last_sync_at is None

                fetcher = get_fetcher("crm", provider, "contacts")
                if not fetcher:
                    raise ValueError(f"No fetcher found for crm/{provider}/contacts")

                sync_start = (
                    datetime.utcnow()
                )  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
                records = await fetcher(
                    nango=nango,
                    connection_id=integration.connection_id,
                    is_initial=is_initial,
                    last_synced_at=last_sync_at,
                    data_type="contacts",
                    provider_config_key=provider_config_key,
                )

                contact_service = ContactSyncService(db=session, organisation_id=org_id)

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

                return {
                    "status": "success",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "integration_type": integration_type.value,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "records_fetched": len(records),
                    "records_synced": metrics.get("contacts", len(records)),
                    "is_initial": is_initial,
                    "metrics": metrics,
                }
        finally:
            await nango.close()

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
            "CRM contacts integration sync failed: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )
        if self.request.retries < self.max_retries:
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
