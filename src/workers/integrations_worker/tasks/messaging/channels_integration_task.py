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
    sync_integration_data_task (for channels)
    sync_integration_data_task (for chats)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

logger = get_logger(__name__)

# Messaging integration types
MESSAGING_INTEGRATION_TYPES = (IntegrationType.TEAMS,)


@app.task(
    name="integrations.sync_messaging_channels_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute between retries
    acks_late=True,
    soft_time_limit=300,  # 5 minutes soft limit for single integration
    time_limit=360,  # 6 minutes hard limit
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
    It dispatches sync tasks for both channels and chats.

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
            "syncs": [
                {"sync_type": "channels", "task_id": "...", "status": "triggered"},
                {"sync_type": "chats", "task_id": "...", "status": "triggered"}
            ]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync the specific messaging integration channels."""
        logger.info(
            "Starting manual messaging channels sync (Tier 3): org=%s integration=%s",
            org_id,
            int_id,
        )

        async for session in acquire_org_async_session(org_id):
            # Query the specific integration
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_id == int_id,
                KnowledgeIntegration.integration_type.in_(MESSAGING_INTEGRATION_TYPES),
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

            # Get provider details
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)

            integration_type = int_type.value

            # Determine provider based on integration type
            if int_type == IntegrationType.TEAMS:
                provider = "teams"
                provider_config_key = "microsoft-teams"
            else:
                # Future: Add Slack support
                provider = str(int_type.value)
                provider_config_key = str(int_type.value)

            syncs = []
            syncs_triggered = 0
            syncs_failed = 0

            # 1. Dispatch channels sync
            try:
                channels_task = sync_integration_data_task.apply_async(
                    kwargs={
                        "organisation_id": str(org_id),
                        "integration_id": str(int_id),
                        "category": "messaging",
                        "provider": provider,
                        "provider_config_key": provider_config_key,
                        "sync_name": "channels",
                        "sync_type": "INCREMENTAL",
                        "sync_variant": "base",
                        "incremental": True,
                    },
                    headers=correlation_headers(),
                )
                syncs.append(
                    {
                        "sync_type": "channels",
                        "task_id": channels_task.id,
                        "status": "triggered",
                    }
                )
                syncs_triggered += 1
                logger.debug(
                    "Manual messaging channels sync dispatched: org=%s integration=%s task=%s",
                    org_id,
                    int_id,
                    channels_task.id,
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch manual messaging channels sync: org=%s integration=%s error=%s",
                    org_id,
                    int_id,
                    str(e),
                    exc_info=True,
                )
                syncs.append(
                    {
                        "sync_type": "channels",
                        "status": "failed",
                        "error": str(e),
                    }
                )
                syncs_failed += 1

            # 2. Dispatch chats sync
            try:
                chats_task = sync_integration_data_task.apply_async(
                    kwargs={
                        "organisation_id": str(org_id),
                        "integration_id": str(int_id),
                        "category": "messaging",
                        "provider": provider,
                        "provider_config_key": provider_config_key,
                        "sync_name": "chats",
                        "sync_type": "INCREMENTAL",
                        "sync_variant": "base",
                        "incremental": True,
                    },
                    headers=correlation_headers(),
                )
                syncs.append(
                    {
                        "sync_type": "chats",
                        "task_id": chats_task.id,
                        "status": "triggered",
                    }
                )
                syncs_triggered += 1
                logger.debug(
                    "Manual messaging chats sync dispatched: org=%s integration=%s task=%s",
                    org_id,
                    int_id,
                    chats_task.id,
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch manual messaging chats sync: org=%s integration=%s error=%s",
                    org_id,
                    int_id,
                    str(e),
                    exc_info=True,
                )
                syncs.append(
                    {
                        "sync_type": "chats",
                        "status": "failed",
                        "error": str(e),
                    }
                )
                syncs_failed += 1

            # Determine overall status
            if syncs_failed == 0:
                status = "success"
            elif syncs_triggered == 0:
                status = "failed"
            else:
                status = "partial"

            logger.info(
                "Manual messaging channels sync dispatched: org=%s integration=%s triggered=%d failed=%d",
                org_id,
                int_id,
                syncs_triggered,
                syncs_failed,
            )

            return {
                "status": status,
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "integration_type": integration_type,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "syncs": syncs,
            }

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
        result = run_async(_sync_integration())

        # Log based on status
        if result.get("status") == "success":
            logger.info(
                "Manual messaging channels sync complete: org=%s integration=%s",
                organisation_id,
                integration_id,
            )
        else:
            logger.warning(
                "Manual messaging channels sync failed: org=%s integration=%s error=%s",
                organisation_id,
                integration_id,
                result.get("error"),
            )

        return result

    except Exception as e:
        logger.error(
            "Manual messaging channels sync exception: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying manual messaging channels sync: org=%s integration=%s attempt=%d/%d",
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


# Convenience alias
sync_messaging_channels_integration = sync_messaging_channels_integration_task
