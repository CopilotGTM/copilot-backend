"""
Tier 3: Integration-level CRM activities sync task.

For manual/on-demand sync of a single CRM integration's activities.

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Validate integration, extract needed data, release session
    2. Fetch activities from API (NO session held)
    3. Session 2: Write activities to DB, update integration, release session
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
from src.service.integrations.crm.entities import DealTaskSyncService
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.registry import get_fetcher

from ..utils import get_provider_from_integration_type, update_last_sync_timestamp

logger = get_logger(__name__)


@dataclass
class IntegrationFetchData:
    """Data extracted from integration needed for fetching activities."""

    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str


@app.task(
    name="integrations.sync_crm_activities_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def sync_crm_activities_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Tier 3: Sync CRM activities for a specific integration.

    Triggered by: Frontend refresh button, API, manual intervention
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync CRM activities for the specific integration.

        Uses fresh session pattern:
        1. Session 1: Validate integration, extract data, release session
        2. Fetch activities from API (NO session held)
        3. Session 2: Write activities to DB, update integration, release session
        """
        logger.info(
            "Starting CRM activities sync for integration (Tier 3): org=%s integration=%s",
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

            fetch_data = IntegrationFetchData(
                integration_id=integration.integration_id,
                connection_id=integration.connection_id,
                integration_type=int_type,
                integration_type_value=int_type.value,
                provider=provider,
                provider_config_key=provider_config_key,
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

        fetcher = get_fetcher("crm", fetch_data.provider, "deal_tasks")
        if not fetcher:
            raise ValueError(f"No fetcher found for crm/{fetch_data.provider}/deal_tasks")

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: Fetch activities from API (NO session held)
        # ─────────────────────────────────────────────────────────────────────
        nango = NangoClient()
        try:
            sync_start = datetime.utcnow()  # Use naive UTC for TIMESTAMP WITHOUT TIME ZONE column
            records = await fetcher(
                nango=nango,
                connection_id=fetch_data.connection_id,
                is_initial=True,
                last_synced_at=None,
                data_type="deal_tasks",
                provider_config_key=fetch_data.provider_config_key,
            )

            logger.info(
                "Fetched %d activities for integration %s",
                len(records),
                fetch_data.integration_id,
            )

            # ─────────────────────────────────────────────────────────────────
            # Phase 3: Write activities to DB and update integration (fresh session)
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

                activity_service = DealTaskSyncService(db=session, organisation_id=org_id)
                metrics = await activity_service.sync(
                    integration=integration, records=records, provider=fetch_data.provider
                )

                integration.last_synced_at = sync_start
                integration.last_sync_status = SyncStatus.SUCCESS.value
                integration.last_sync_error = None
                update_last_sync_timestamp(integration, "activities", sync_start)
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
                "records_synced": metrics.get("activities", len(records)),
                "metrics": metrics,
            }
        finally:
            await nango.close()

    try:
        return run_async(_sync_integration())
    except Exception as e:
        logger.error(
            "CRM activities integration sync failed: org=%s integration=%s error=%s",
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
