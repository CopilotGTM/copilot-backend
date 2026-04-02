"""
Tier 3: Integration-level CRM contacts sync task.

For manual/on-demand sync of a single CRM integration's contacts.

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Validate integration, extract needed data, release session
    2. Fetch contacts from API (NO session held)
    3. Session 2: Write contacts to DB, update integration, release session
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
    SyncStrategy,
    get_provider_from_integration_type,
    get_sync_strategy,
    update_last_sync_timestamp,
)

logger = get_logger(__name__)


@dataclass
class IntegrationFetchData:
    """Data extracted from integration needed for fetching contacts."""

    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str
    is_initial: bool
    last_sync_at: datetime | None


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
    """Tier 3: Sync CRM contacts for a specific integration.

    Triggered by: Frontend refresh button, API, manual intervention
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync CRM contacts for the specific integration.

        Uses fresh session pattern:
        1. Session 1: Validate integration, extract data, release session
        2. Fetch contacts from API (NO session held)
        3. Session 2: Write contacts to DB, update integration, release session
        """
        logger.info(
            "Starting CRM contacts sync for integration (Tier 3): org=%s integration=%s",
            org_id,
            int_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Validate integration and extract data needed for fetching
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        fetch_data: IntegrationFetchData | None = None

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_id == int_id,
                KnowledgeIntegration.is_active.is_(True),
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

            # Extract data for fetching
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)

            provider = get_provider_from_integration_type(int_type)
            provider_config_key = to_nango_provider_key(int_type)

            if force_full_sync:
                sync_strategy = SyncStrategy(is_initial=True, last_sync_at=None)
            else:
                sync_strategy = await get_sync_strategy(
                    session=session,
                    integration_id=integration.integration_id,
                    entity_type="contacts",
                )

            fetch_data = IntegrationFetchData(
                integration_id=integration.integration_id,
                connection_id=integration.connection_id,
                integration_type=int_type,
                integration_type_value=int_type.value,
                provider=provider,
                provider_config_key=provider_config_key,
                is_initial=sync_strategy.is_initial,
                last_sync_at=sync_strategy.last_sync_at,
            )
        # Session is now released

        if not fetch_data:
            return {
                "status": "failed",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": "Failed to acquire database session",
            }

        fetcher = get_fetcher("crm", fetch_data.provider, "contacts")
        if not fetcher:
            raise ValueError(f"No fetcher found for crm/{fetch_data.provider}/contacts")

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: Fetch contacts from API (NO session held)
        # ─────────────────────────────────────────────────────────────────────
        nango = NangoClient()
        try:
            sync_start = datetime.utcnow()  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
            records = await fetcher(
                nango=nango,
                connection_id=fetch_data.connection_id,
                is_initial=fetch_data.is_initial,
                last_synced_at=fetch_data.last_sync_at,
                data_type="contacts",
                provider_config_key=fetch_data.provider_config_key,
            )

            logger.info(
                "Fetched %d contacts for integration %s", len(records), fetch_data.integration_id
            )

            # ─────────────────────────────────────────────────────────────────
            # Phase 3: Write contacts to DB and update integration (fresh session)
            # ─────────────────────────────────────────────────────────────────
            metrics: dict[str, Any] = {}

            async for session in acquire_org_async_session(org_id):
                # Reload integration for this session
                stmt = select(KnowledgeIntegration).where(
                    KnowledgeIntegration.integration_id == fetch_data.integration_id,
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()

                if not integration:
                    raise ValueError(f"Integration {fetch_data.integration_id} not found")

                contact_service = ContactSyncService(db=session, organisation_id=org_id)

                if fetch_data.is_initial:
                    metrics = await contact_service.sync(
                        integration=integration, records=records, provider=fetch_data.provider
                    )
                else:
                    metrics = await contact_service.sync_incremental(
                        integration=integration, records=records, provider=fetch_data.provider
                    )

                integration.last_synced_at = sync_start
                integration.last_sync_status = SyncStatus.SUCCESS.value
                integration.last_sync_error = None
                update_last_sync_timestamp(integration, "contacts", sync_start)
                attributes.flag_modified(integration, "connection_metadata")
                session.add(integration)
                await session.commit()
            # Session is now released

            return {
                "status": "success",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "integration_type": fetch_data.integration_type_value,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "records_fetched": len(records),
                "records_synced": metrics.get("contacts", len(records)),
                "is_initial": fetch_data.is_initial,
                "metrics": metrics,
            }
        finally:
            await nango.close()

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
