"""
Tier 2: Organization-level channels/chats sync task.

Syncs all messaging integrations (Teams channels and chats) for a specific organization.

This task:
1. Queries org DB for all active messaging integrations
2. For each integration, fetches and syncs channels AND chats
3. Returns org-level aggregated results

Architecture:
    sync_messaging_channels_all_orgs_task [Tier 1]
        |
    sync_messaging_channels_org_task [Tier 2 - this file] (one per org)
        |
    MessagingSyncService (for each integration, inline)

NOTE: This task calls the sync service directly (similar to CRM/Email pattern),
not via sync_integration_data_task dispatch.

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query integrations, extract needed data, release session
    2. For each integration:
       a. Fetch channels from API (NO session held)
       b. Session 2: Write channels to DB, release session
       c. Fetch chats from API (NO session held)
       d. Session 3: Write chats to DB + update integration, release session
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.messaging_sync_service import MessagingSyncService
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.messaging.teams import (
    fetch_channels as teams_fetch_channels,
)
from src.workers.integrations_worker.providers.messaging.teams import (
    fetch_chats as teams_fetch_chats,
)

logger = get_logger(__name__)


@dataclass
class IntegrationFetchData:
    """Data extracted from integration needed for fetching channels/chats."""

    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str
    is_initial: bool
    last_synced_at: datetime | None
    credentials: dict[str, Any]
    access_token_expires_at: datetime | None


# Messaging integration types to sync
MESSAGING_INTEGRATION_TYPES = (IntegrationType.TEAMS,)


@app.task(
    name="integrations.sync_messaging_channels_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,  # 2 minutes between retries
    acks_late=True,
    soft_time_limit=900,  # 15 minutes soft limit (may have many channels)
    time_limit=960,  # 16 minutes hard limit
)
def sync_messaging_channels_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync all messaging channels/chats for one organization.

    Triggered by: sync_messaging_channels_all_orgs_task (Tier 1)

    This task loops through all active messaging integrations for the org
    and syncs channels and chats using MessagingSyncService.

    Args:
        organisation_id: The organization UUID string

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
                "total_channels_synced": 15,
                "total_chats_synced": 10
            },
            "integration_results": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync all messaging channels for the organization.

        Uses fresh session pattern:
        1. Session 1: Query integrations, extract data, release session
        2. For each integration:
           a. Fetch channels from API (NO session held)
           b. Session 2: Write channels to DB, release session
           c. Fetch chats from API (NO session held)
           d. Session 3: Write chats to DB + update integration, release session
        """
        logger.info("Starting messaging channels sync for organization (Tier 2): %s", org_id)

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query integrations and extract data needed for fetching
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        integration_fetch_list: list[IntegrationFetchData] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_type.in_(MESSAGING_INTEGRATION_TYPES),
                KnowledgeIntegration.is_active == True,  # noqa: E712
                KnowledgeIntegration.connection_id.isnot(None),
            )
            result = await session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info("No active messaging integrations for org (channels sync): %s", org_id)
                return {
                    "status": "completed",
                    "organisation_id": organisation_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "summary": {
                        "total_integrations": 0,
                        "successful": 0,
                        "failed": 0,
                        "total_channels_synced": 0,
                        "total_chats_synced": 0,
                    },
                    "integration_results": [],
                }

            logger.info(
                "Found %d active messaging integration(s) for org (channels sync): %s",
                len(integrations),
                org_id,
            )

            # Extract data needed for fetching
            for integration in integrations:
                int_type = (
                    IntegrationType(integration.integration_type)
                    if isinstance(integration.integration_type, str)
                    else integration.integration_type
                )

                # Determine provider based on integration type
                if int_type == IntegrationType.TEAMS:
                    provider = "teams"
                    provider_config_key = "microsoft-teams"
                else:
                    provider = str(int_type.value)
                    provider_config_key = str(int_type.value)

                integration_fetch_list.append(
                    IntegrationFetchData(
                        integration_id=integration.integration_id,
                        connection_id=integration.connection_id,
                        integration_type=int_type,
                        integration_type_value=int_type.value,
                        provider=provider,
                        provider_config_key=provider_config_key,
                        is_initial=integration.last_synced_at is None,
                        last_synced_at=integration.last_synced_at,
                        credentials=dict(integration.credentials or {}),
                        access_token_expires_at=integration.access_token_expires_at,
                    )
                )
        # Session is now released

        if not integration_fetch_list:
            return {
                "status": "completed",
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_integrations": 0,
                    "successful": 0,
                    "failed": 0,
                    "total_channels_synced": 0,
                    "total_chats_synced": 0,
                },
                "integration_results": [],
            }

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2 & 3: For each integration, fetch then persist
        # External API calls happen WITHOUT holding a database session
        # ─────────────────────────────────────────────────────────────────────
        integration_results = []
        successful = 0
        failed = 0
        total_channels = 0
        total_chats = 0

        for fetch_data in integration_fetch_list:
            logger.info(
                "Syncing messaging integration: org=%s integration=%s type=%s",
                org_id,
                fetch_data.integration_id,
                fetch_data.integration_type_value,
            )

            integration_sync_result: dict[str, Any] = {
                "integration_id": str(fetch_data.integration_id),
                "integration_type": fetch_data.integration_type_value,
                "channels_synced": 0,
                "chats_synced": 0,
                "errors": [],
            }

            # ─────────────────────────────────────────────────────────────
            # Create IntegrationSyncHistory record (fresh session)
            # ─────────────────────────────────────────────────────────────
            sync_history_id: UUID | None = None
            sync_start = datetime.now(UTC).replace(tzinfo=None)
            try:
                async for session in acquire_org_async_session(org_id):
                    sync_history = IntegrationSyncHistory(
                        integration_id=fetch_data.integration_id,
                        started_at=sync_start,
                        status=SyncStatus.IN_PROGRESS,
                    )
                    session.add(sync_history)
                    await session.commit()
                    await session.refresh(sync_history)
                    sync_history_id = sync_history.sync_id
            except Exception as e:
                logger.warning(
                    "Failed to create sync history record: org=%s integration=%s error=%s",
                    org_id,
                    fetch_data.integration_id,
                    str(e),
                )
                # Continue with sync even if history record fails

            token_info = IntegrationTokenInfo(
                access_token=fetch_data.credentials.get("access_token"),
                refresh_token=fetch_data.credentials.get("refresh_token"),
                access_token_expires_at=fetch_data.access_token_expires_at,
                integration_type=fetch_data.integration_type,
                organisation_id=org_id,
                integration_id=fetch_data.integration_id,
            )

            # ─────────────────────────────────────────────────────────────
            # Phase 2a: Fetch channels from API (NO session held)
            # ─────────────────────────────────────────────────────────────
            channels: list[dict[str, Any]] = []
            try:
                channels = await teams_fetch_channels(
                    token_info=token_info,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
                logger.info(
                    "Fetched %d channels for integration %s",
                    len(channels),
                    fetch_data.integration_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to fetch channels: org=%s integration=%s error=%s",
                    org_id,
                    fetch_data.integration_id,
                    str(e),
                    exc_info=True,
                )
                integration_sync_result["errors"].append(f"channels_fetch: {str(e)}")

            # ─────────────────────────────────────────────────────────────
            # Phase 2b: Write channels to DB (fresh session)
            # ─────────────────────────────────────────────────────────────
            if channels:
                try:
                    async for session in acquire_org_async_session(org_id):
                        # Reload integration for the service
                        stmt = select(KnowledgeIntegration).where(
                            KnowledgeIntegration.integration_id == fetch_data.integration_id,
                        )
                        result = await session.execute(stmt)
                        integration = result.scalar_one_or_none()

                        if integration:
                            messaging_service = MessagingSyncService(session, org_id)
                            channels_metrics = await messaging_service.sync_channels_incremental(
                                integration=integration,
                                channels=channels,
                                provider=fetch_data.provider,
                            )
                            channels_synced = channels_metrics.get(
                                "records_processed",
                                channels_metrics.get("channels_processed", 0),
                            )
                            integration_sync_result["channels_synced"] = channels_synced
                            total_channels += channels_synced
                    # Session released
                except Exception as e:
                    logger.error(
                        "Failed to sync channels: org=%s integration=%s error=%s",
                        org_id,
                        fetch_data.integration_id,
                        str(e),
                        exc_info=True,
                    )
                    integration_sync_result["errors"].append(f"channels_sync: {str(e)}")

            # ─────────────────────────────────────────────────────────────
            # Phase 3a: Fetch chats from API (NO session held)
            # ─────────────────────────────────────────────────────────────
            chats: list[dict[str, Any]] = []
            try:
                chats_result = await teams_fetch_chats(
                    token_info=token_info,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
                chats = chats_result.chats
                logger.info(
                    "Fetched %d chats for integration %s",
                    len(chats),
                    fetch_data.integration_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to fetch chats: org=%s integration=%s error=%s",
                    org_id,
                    fetch_data.integration_id,
                    str(e),
                    exc_info=True,
                )
                integration_sync_result["errors"].append(f"chats_fetch: {str(e)}")

            # ─────────────────────────────────────────────────────────────
            # Phase 3b: Write chats to DB + update integration (fresh session)
            # ─────────────────────────────────────────────────────────────
            try:
                async for session in acquire_org_async_session(org_id):
                    # Reload integration
                    stmt = select(KnowledgeIntegration).where(
                        KnowledgeIntegration.integration_id == fetch_data.integration_id,
                    )
                    result = await session.execute(stmt)
                    integration = result.scalar_one_or_none()

                    if integration:
                        # Write chats if we have any
                        if chats:
                            messaging_service = MessagingSyncService(session, org_id)
                            chats_metrics = await messaging_service.sync_channels_incremental(
                                integration=integration,
                                channels=chats,
                                provider=fetch_data.provider,
                            )
                            chats_synced = chats_metrics.get(
                                "records_processed",
                                chats_metrics.get("channels_processed", 0),
                            )
                            integration_sync_result["chats_synced"] = chats_synced
                            total_chats += chats_synced

                        # Update last_synced_at
                        integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                        session.add(integration)
                        await session.commit()
                # Session released
            except Exception as e:
                logger.error(
                    "Failed to sync chats/update integration: org=%s integration=%s error=%s",
                    org_id,
                    fetch_data.integration_id,
                    str(e),
                    exc_info=True,
                )
                integration_sync_result["errors"].append(f"chats_sync: {str(e)}")

            # Determine integration status
            if integration_sync_result["errors"]:
                if (
                    integration_sync_result["channels_synced"] > 0
                    or integration_sync_result["chats_synced"] > 0
                ):
                    integration_sync_result["status"] = "partial"
                else:
                    integration_sync_result["status"] = "failed"
                    failed += 1
            else:
                integration_sync_result["status"] = "success"
                successful += 1

            # ─────────────────────────────────────────────────────────────
            # Update IntegrationSyncHistory record (fresh session)
            # ─────────────────────────────────────────────────────────────
            if sync_history_id:
                try:
                    async for session in acquire_org_async_session(org_id):
                        history_stmt = select(IntegrationSyncHistory).where(
                            IntegrationSyncHistory.sync_id == sync_history_id,
                        )
                        history_result = await session.execute(history_stmt)
                        sync_history = history_result.scalar_one_or_none()

                        if sync_history:
                            sync_history.completed_at = datetime.now(UTC).replace(tzinfo=None)
                            sync_history.records_processed = (
                                integration_sync_result["channels_synced"]
                                + integration_sync_result["chats_synced"]
                            )

                            if integration_sync_result["errors"]:
                                sync_history.status = SyncStatus.FAILED
                                sync_history.error_details = {
                                    "errors": integration_sync_result["errors"][:5]
                                }
                            else:
                                sync_history.status = SyncStatus.SUCCESS

                            sync_history.sync_metadata = {
                                "entity": "messaging_channels",
                                "provider": fetch_data.provider,
                                "channels_synced": integration_sync_result["channels_synced"],
                                "chats_synced": integration_sync_result["chats_synced"],
                            }
                            session.add(sync_history)
                            await session.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to update sync history: org=%s sync_id=%s error=%s",
                        org_id,
                        sync_history_id,
                        str(e),
                    )

            integration_results.append(integration_sync_result)

        completed_at = datetime.now(UTC).isoformat()

        # Determine overall status
        if failed == 0:
            status = "completed"
        elif successful == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Messaging channels sync complete for org %s: successful=%d failed=%d channels=%d chats=%d",
            org_id,
            successful,
            failed,
            total_channels,
            total_chats,
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
                "total_channels_synced": total_channels,
                "total_chats_synced": total_chats,
            },
            "integration_results": integration_results,
        }

    try:
        return run_async(_sync_org())
    except Exception as e:
        logger.error(
            "Messaging channels org sync failed: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying messaging channels org sync: org=%s attempt=%d/%d",
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
