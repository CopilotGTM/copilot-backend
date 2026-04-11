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
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.messaging_sync_service import MessagingSyncService
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.sync_health import (
    CIRCUIT_BREAKER_THRESHOLD,
    record_sync_failure,
    record_sync_success,
)
from src.service.integrations.sync_history_manager import SyncHistoryManager
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS
from src.workers.integrations_worker.providers.messaging.slack import (
    fetch_channels as slack_fetch_channels,
)
from src.workers.integrations_worker.providers.messaging.slack import (
    fetch_chats as slack_fetch_chats,
)
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
    connection_id: str | None
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str
    is_initial: bool
    last_synced_at: datetime | None
    credentials: dict[str, Any]
    access_token_expires_at: datetime | None


# Messaging integration types to sync
MESSAGING_INTEGRATION_TYPES = (IntegrationType.TEAMS, IntegrationType.SLACK)

_TASK_NAME = "integrations.sync_messaging_channels_org"
_L = TASK_LIMITS[_TASK_NAME]


def _resolve_messaging_provider(
    integration_type: IntegrationType,
) -> tuple[str, str]:
    """Map integration types to worker provider names/config keys."""
    if integration_type == IntegrationType.TEAMS:
        return "teams", "microsoft-teams"
    if integration_type == IntegrationType.SLACK:
        return "slack", "slack"
    return str(integration_type.value), str(integration_type.value)


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 120),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
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
    current_integration_id: list[UUID | None] = [None]

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
        logger.info(
            "Messaging channels sync starting for org (Tier 2)",
            extra={"organisation_id": organisation_id},
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query integrations and extract data needed for fetching
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        integration_fetch_list: list[IntegrationFetchData] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type).in_(MESSAGING_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                col(KnowledgeIntegration.connection_id).isnot(None),
                col(KnowledgeIntegration.consecutive_sync_failures) < CIRCUIT_BREAKER_THRESHOLD,
            )
            result = await session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info(
                    "No active messaging integrations for org (channels sync)",
                    extra={"organisation_id": organisation_id},
                )
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
                "Messaging channels sync found integrations for org",
                extra={
                    "organisation_id": organisation_id,
                    "total_integrations": len(integrations),
                },
            )

            # Extract data needed for fetching
            for integration in integrations:
                int_type = (
                    IntegrationType(integration.integration_type)
                    if isinstance(integration.integration_type, str)
                    else integration.integration_type
                )

                # Determine provider based on integration type
                provider, provider_config_key = _resolve_messaging_provider(int_type)

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
        partial = 0
        total_channels = 0
        total_chats = 0

        for fetch_data in integration_fetch_list:
            current_integration_id[0] = fetch_data.integration_id
            logger.info(
                "Syncing messaging integration",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": str(fetch_data.integration_id),
                    "integration_type": fetch_data.integration_type_value,
                },
            )

            integration_sync_result: dict[str, Any] = {
                "integration_id": str(fetch_data.integration_id),
                "integration_type": fetch_data.integration_type_value,
                "channels_synced": 0,
                "chats_synced": 0,
                "errors": [],
            }
            # Preserve the first real exception for proper failure classification
            first_exception: Exception | None = None

            # ─────────────────────────────────────────────────────────────
            # Create IntegrationSyncHistory record (fresh session)
            # ─────────────────────────────────────────────────────────────
            sync_history_id: UUID | None = None
            try:
                async for session in acquire_org_async_session(org_id):
                    sync_history_id = await SyncHistoryManager.start_fresh(
                        session,
                        fetch_data.integration_id,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to create sync history record",
                    extra={
                        "organisation_id": organisation_id,
                        "integration_id": str(fetch_data.integration_id),
                        "error": str(e),
                    },
                )

            token_info = IntegrationTokenInfo.from_integration(fetch_data, org_id)

            # ─────────────────────────────────────────────────────────────
            # Phase 2a: Fetch channels from API (NO session held)
            # ─────────────────────────────────────────────────────────────
            channels: list[dict[str, Any]] = []
            try:
                if fetch_data.provider == "slack":
                    channels = await slack_fetch_channels(
                        token_info=token_info,
                        connection_id=fetch_data.connection_id or "",
                        is_initial=fetch_data.is_initial,
                        last_synced_at=fetch_data.last_synced_at,
                        provider_config_key=fetch_data.provider_config_key,
                    )
                else:
                    channels = await teams_fetch_channels(
                        token_info=token_info,
                        connection_id=fetch_data.connection_id or "",
                        is_initial=fetch_data.is_initial,
                        last_synced_at=fetch_data.last_synced_at,
                        provider_config_key=fetch_data.provider_config_key,
                    )
                logger.info(
                    "Channels fetched for integration",
                    extra={
                        "integration_id": str(fetch_data.integration_id),
                        "channels_fetched": len(channels),
                    },
                )
            except Exception as e:
                logger.error(
                    "Failed to fetch channels",
                    exc_info=True,
                    extra={
                        "organisation_id": organisation_id,
                        "integration_id": str(fetch_data.integration_id),
                        "error": str(e),
                    },
                )
                integration_sync_result["errors"].append(f"channels_fetch: {str(e)}")
                if first_exception is None:
                    first_exception = e

            # ─────────────────────────────────────────────────────────────
            # Phase 2b: Write channels to DB (fresh session)
            # ─────────────────────────────────────────────────────────────
            if channels:
                try:
                    async for session in acquire_org_async_session(org_id):
                        # Reload integration for the service
                        stmt = select(KnowledgeIntegration).where(
                            col(KnowledgeIntegration.integration_id) == fetch_data.integration_id,
                        )
                        result = await session.execute(stmt)
                        integration_db = result.scalar_one_or_none()

                        if integration_db:
                            messaging_service = MessagingSyncService(session, org_id)
                            channels_metrics = await messaging_service.sync_channels_incremental(
                                integration=integration_db,
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
                        "Failed to sync channels",
                        exc_info=True,
                        extra={
                            "organisation_id": organisation_id,
                            "integration_id": str(fetch_data.integration_id),
                            "error": str(e),
                        },
                    )
                    integration_sync_result["errors"].append(f"channels_sync: {str(e)}")
                    if first_exception is None:
                        first_exception = e

            # ─────────────────────────────────────────────────────────────
            # Phase 3a: Fetch chats from API (NO session held)
            # ─────────────────────────────────────────────────────────────
            chats: list[dict[str, Any]] = []
            try:
                if fetch_data.provider == "slack":
                    chats_result = await slack_fetch_chats(
                        token_info=token_info,
                        connection_id=fetch_data.connection_id or "",
                        is_initial=fetch_data.is_initial,
                        last_synced_at=fetch_data.last_synced_at,
                        provider_config_key=fetch_data.provider_config_key,
                    )
                else:
                    chats_result = await teams_fetch_chats(
                        token_info=token_info,
                        connection_id=fetch_data.connection_id or "",
                        is_initial=fetch_data.is_initial,
                        last_synced_at=fetch_data.last_synced_at,
                        provider_config_key=fetch_data.provider_config_key,
                    )
                chats = chats_result.chats
                logger.info(
                    "Chats fetched for integration",
                    extra={
                        "integration_id": str(fetch_data.integration_id),
                        "chats_fetched": len(chats),
                    },
                )
            except Exception as e:
                logger.error(
                    "Failed to fetch chats",
                    exc_info=True,
                    extra={
                        "organisation_id": organisation_id,
                        "integration_id": str(fetch_data.integration_id),
                        "error": str(e),
                    },
                )
                integration_sync_result["errors"].append(f"chats_fetch: {str(e)}")
                if first_exception is None:
                    first_exception = e

            # ─────────────────────────────────────────────────────────────
            # Phase 3b: Write chats to DB + update integration (fresh session)
            # ─────────────────────────────────────────────────────────────
            try:
                async for session in acquire_org_async_session(org_id):
                    # Reload integration
                    stmt = select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_id) == fetch_data.integration_id,
                    )
                    result = await session.execute(stmt)
                    integration_db = result.scalar_one_or_none()

                    if integration_db:
                        # Write chats if we have any
                        if chats:
                            messaging_service = MessagingSyncService(session, org_id)
                            chats_metrics = await messaging_service.sync_channels_incremental(
                                integration=integration_db,
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
                        integration_db.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                        session.add(integration_db)
                        await session.commit()
                # Session released
            except Exception as e:
                logger.error(
                    "Failed to sync chats/update integration",
                    exc_info=True,
                    extra={
                        "organisation_id": organisation_id,
                        "integration_id": str(fetch_data.integration_id),
                        "error": str(e),
                    },
                )
                integration_sync_result["errors"].append(f"chats_sync: {str(e)}")
                if first_exception is None:
                    first_exception = e

            # Determine integration status and record health.
            # Partial failures still record a failure — the real exception
            # is preserved so classify_sync_error() can detect auth vs transient.
            if integration_sync_result["errors"]:
                has_partial = (
                    integration_sync_result["channels_synced"] > 0
                    or integration_sync_result["chats_synced"] > 0
                )
                integration_sync_result["status"] = "partial" if has_partial else "failed"
                if has_partial:
                    partial += 1
                else:
                    failed += 1
                await record_sync_failure(
                    organisation_id=org_id,
                    integration_id=fetch_data.integration_id,
                    error=first_exception or Exception(integration_sync_result["errors"][0]),
                )
            else:
                integration_sync_result["status"] = "success"
                successful += 1
                await record_sync_success(
                    organisation_id=org_id,
                    integration_id=fetch_data.integration_id,
                )

            # ─────────────────────────────────────────────────────────────
            # Update IntegrationSyncHistory record (fresh session)
            # ─────────────────────────────────────────────────────────────
            if sync_history_id:
                errors = integration_sync_result["errors"]
                try:
                    async for session in acquire_org_async_session(org_id):
                        await SyncHistoryManager.complete_fresh(
                            session,
                            sync_history_id,
                            status=(
                                SyncStatus.SUCCESS
                                if not errors
                                else (
                                    SyncStatus.PARTIAL
                                    if integration_sync_result["status"] == "partial"
                                    else SyncStatus.FAILED
                                )
                            ),
                            records_processed=(
                                integration_sync_result["channels_synced"]
                                + integration_sync_result["chats_synced"]
                            ),
                            metadata={
                                "entity": "messaging_channels",
                                "provider": fetch_data.provider,
                                "channels_synced": integration_sync_result["channels_synced"],
                                "chats_synced": integration_sync_result["chats_synced"],
                            },
                            error_details={"errors": errors[:5]} if errors else None,
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to update sync history",
                        extra={
                            "organisation_id": organisation_id,
                            "sync_history_id": str(sync_history_id),
                            "error": str(e),
                        },
                    )

            integration_results.append(integration_sync_result)

        completed_at = datetime.now(UTC).isoformat()

        # Determine overall status
        if failed == 0 and partial == 0:
            status = "completed"
        elif successful == 0 and partial == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Messaging channels sync complete for org",
            extra={
                "organisation_id": organisation_id,
                "successful": successful,
                "partial": partial,
                "failed": failed,
                "total_channels_synced": total_channels,
                "total_chats_synced": total_chats,
            },
        )

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "total_integrations": len(integration_fetch_list),
                "successful": successful,
                "partial": partial,
                "failed": failed,
                "total_channels_synced": total_channels,
                "total_chats_synced": total_chats,
            },
            "integration_results": integration_results,
        }

    try:
        return run_async(_sync_org())
    except SoftTimeLimitExceeded:
        logger.error(
            "Messaging channels org sync timed out",
            extra={"organisation_id": organisation_id, "soft_limit_seconds": _L["soft"]},
        )
        int_id = current_integration_id[0]
        if int_id:
            try:
                run_async(
                    record_sync_failure(
                        organisation_id=org_id,
                        integration_id=int_id,
                        error=TimeoutError(f"Soft time limit exceeded ({_L['soft']}s)"),
                    )
                )
            except Exception:
                logger.warning(
                    "Failed to record timeout failure",
                    extra={"integration_id": str(int_id)},
                )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_L['soft']}s",
        }
    except Exception as e:
        logger.error(
            "Messaging channels org sync failed",
            exc_info=True,
            extra={"organisation_id": organisation_id, "error": str(e)},
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying messaging channels org sync",
                extra={
                    "organisation_id": organisation_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
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
