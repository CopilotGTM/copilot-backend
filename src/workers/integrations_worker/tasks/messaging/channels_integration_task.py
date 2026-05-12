"""
Tier 3: Integration-level channels/chats sync task.

Syncs a specific messaging integration for manual/user-level triggers.

This task is used for:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

NOTE: This task is NOT used by the scheduled sync (Celery Beat).
The scheduler uses Tier 1 -> Tier 2, which handles integrations inline.

Architecture:
    Frontend/API (manual trigger)
        |
    sync_messaging_channels_integration_task [Tier 3 - this file]
        |
    MessagingSyncService (direct call, same pattern as CRM/Email)

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query integration, validate, extract data, release session
    2. Fetch channels from API (NO session held)
    3. Session 2: Write channels to DB, release session
    4. Fetch chats from API (NO session held)
    5. Session 3: Write chats to DB + update integration, release session
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
from src.service.integrations.sync_health import record_sync_failure
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
    connection_id: str
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str
    is_initial: bool
    last_synced_at: datetime | None
    credentials: dict[str, Any]
    access_token_expires_at: datetime | None
    scopes: list[str] | None


# Messaging integration types
MESSAGING_INTEGRATION_TYPES = (IntegrationType.TEAMS, IntegrationType.SLACK)

_TASK_NAME = "integrations.sync_messaging_channels_integration"
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
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_messaging_channels_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """
    Tier 3: Sync specific messaging integration.

    Triggered by: Frontend refresh button, API, manual intervention
    NOT triggered by: Scheduled sync (Tier 1/2)

    This task is for on-demand/manual sync of a single messaging integration.
    It syncs both channels and chats using MessagingSyncService.

    Args:
        organisation_id: The organization UUID string
        integration_id: The specific integration UUID string

    Returns:
        Dictionary with integration sync results:
        {
            "status": "success",
            "organisation_id": "uuid",
            "integration_id": "uuid",
            "integration_type": "teams",
            "started_at": "...",
            "completed_at": "...",
            "channels_synced": 10,
            "chats_synced": 5
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync the specific messaging integration channels.

        Uses fresh session pattern:
        1. Session 1: Query integration, validate, extract data, release session
        2. Fetch channels from API (NO session held)
        3. Session 2: Write channels to DB, release session
        4. Fetch chats from API (NO session held)
        5. Session 3: Write chats to DB + update integration, release session
        """
        logger.info(
            "Manual messaging channels sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query integration and extract data needed for fetching
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        fetch_data: IntegrationFetchData | None = None

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == int_id,
                col(KnowledgeIntegration.integration_type).in_(MESSAGING_INTEGRATION_TYPES),
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
                    "error": "Integration not found or not a messaging integration",
                }

            if not integration.is_active:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration is not active",
                }

            if not integration.connection_id:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration is not connected (no connection_id)",
                }

            # Extract data for fetching
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)

            provider, provider_config_key = _resolve_messaging_provider(int_type)

            fetch_data = IntegrationFetchData(
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
                scopes=list(integration.scopes or []),
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

        logger.info(
            "Syncing messaging integration",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "integration_type": fetch_data.integration_type_value,
            },
        )

        # ─────────────────────────────────────────────────────────────────────
        # Create IntegrationSyncHistory record (fresh session)
        # ─────────────────────────────────────────────────────────────────────
        sync_history_id: UUID | None = None
        try:
            async for session in acquire_org_async_session(org_id):
                sync_history_id = await SyncHistoryManager.start_fresh(
                    session,
                    int_id,
                )
        except Exception as e:
            logger.warning(
                "Failed to create sync history record",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": str(e),
                },
            )
            # Continue with sync even if history record fails

        channels_synced = 0
        chats_synced = 0
        errors: list[str] = []

        token_info = IntegrationTokenInfo.from_integration(fetch_data, org_id)

        # ─────────────────────────────────────────────────────────────────
        # Phase 2: Fetch channels from API (NO session held)
        # ─────────────────────────────────────────────────────────────────
        channels: list[dict[str, Any]] = []
        try:
            if fetch_data.provider == "slack":
                channels = await slack_fetch_channels(
                    token_info=token_info,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
            else:
                channels = await teams_fetch_channels(
                    token_info=token_info,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
            logger.info(
                "Channels fetched for integration",
                extra={
                    "integration_id": integration_id,
                    "channels_fetched": len(channels),
                },
            )
        except Exception as e:
            logger.error(
                "Failed to fetch channels",
                exc_info=True,
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": str(e),
                },
            )
            errors.append(f"channels_fetch: {str(e)}")

        # ─────────────────────────────────────────────────────────────────
        # Phase 3: Write channels to DB (fresh session)
        # ─────────────────────────────────────────────────────────────────
        if channels:
            try:
                async for session in acquire_org_async_session(org_id):
                    stmt = select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_id) == int_id,
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
                # Session released
            except Exception as e:
                logger.error(
                    "Failed to sync channels",
                    exc_info=True,
                    extra={
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "error": str(e),
                    },
                )
                errors.append(f"channels_sync: {str(e)}")

        # ─────────────────────────────────────────────────────────────────
        # Phase 4: Fetch chats from API (NO session held)
        # ─────────────────────────────────────────────────────────────────
        chats: list[dict[str, Any]] = []
        try:
            if fetch_data.provider == "slack":
                chats_result = await slack_fetch_chats(
                    token_info=token_info,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
            else:
                chats_result = await teams_fetch_chats(
                    token_info=token_info,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
            chats = chats_result.chats
            logger.info(
                "Chats fetched for integration",
                extra={
                    "integration_id": integration_id,
                    "chats_fetched": len(chats),
                },
            )
        except Exception as e:
            logger.error(
                "Failed to fetch chats",
                exc_info=True,
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": str(e),
                },
            )
            errors.append(f"chats_fetch: {str(e)}")

        # ─────────────────────────────────────────────────────────────────
        # Phase 5: Write chats to DB + update integration (fresh session)
        # ─────────────────────────────────────────────────────────────────
        try:
            async for session in acquire_org_async_session(org_id):
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == int_id,
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

                    # Update last_synced_at
                    integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                    session.add(integration)
                    await session.commit()
            # Session released
        except Exception as e:
            logger.error(
                "Failed to sync chats/update integration",
                exc_info=True,
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": str(e),
                },
            )
            errors.append(f"chats_sync: {str(e)}")

        # Determine overall status
        if errors:
            status = "partial" if channels_synced > 0 or chats_synced > 0 else "failed"
        else:
            status = "success"

        logger.info(
            "Manual messaging channels sync complete",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "channels_synced": channels_synced,
                "chats_synced": chats_synced,
            },
        )

        # ─────────────────────────────────────────────────────────────────────
        # Update IntegrationSyncHistory record (fresh session)
        # ─────────────────────────────────────────────────────────────────────
        if sync_history_id:
            try:
                async for session in acquire_org_async_session(org_id):
                    await SyncHistoryManager.complete_fresh(
                        session,
                        sync_history_id,
                        status=SyncStatus.FAILED if errors else SyncStatus.SUCCESS,
                        records_processed=channels_synced + chats_synced,
                        metadata={
                            "entity": "messaging_channels",
                            "provider": fetch_data.provider,
                            "channels_synced": channels_synced,
                            "chats_synced": chats_synced,
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

        result_dict: dict[str, Any] = {
            "status": status,
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "integration_type": fetch_data.integration_type_value,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "channels_synced": channels_synced,
            "chats_synced": chats_synced,
        }

        if errors:
            result_dict["errors"] = errors

        return result_dict

    try:
        result = run_async(_sync_integration())

        # Log based on status
        if result.get("status") == "success":
            logger.info(
                "Manual messaging channels sync succeeded",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "channels_synced": result.get("channels_synced", 0),
                    "chats_synced": result.get("chats_synced", 0),
                },
            )
        else:
            logger.warning(
                "Manual messaging channels sync failed",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": result.get("error") or result.get("errors"),
                },
            )

        return result

    except SoftTimeLimitExceeded:
        logger.error(
            "Manual messaging channels sync timed out",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "soft_limit_seconds": _L["soft"],
            },
        )
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
                extra={"integration_id": integration_id},
            )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_L['soft']}s",
        }
    except Exception as e:
        logger.error(
            "Manual messaging channels sync exception",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "error": str(e),
            },
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying manual messaging channels sync",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
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


# Convenience alias
sync_messaging_channels_integration = sync_messaging_channels_integration_task
