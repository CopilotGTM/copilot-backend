"""
Tier 2: Organization-level channels/chats sync task.

Syncs all messaging integrations (Teams channels and chats) for a specific organization.

This task:
1. Queries org DB for all active messaging integrations
2. For each integration, dispatches sync_integration_data_task for channels AND chats
3. Returns org-level aggregated results

Architecture:
    sync_messaging_channels_all_orgs_task [Tier 1]
        |
    sync_messaging_channels_org_task [Tier 2 - this file] (one per org)
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
    and dispatches sync_integration_data_task for both channels and chats.

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
                "syncs_triggered": 4,
                "syncs_failed": 0
            },
            "integration_results": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync all messaging channels for the organization."""
        logger.info("Starting messaging channels sync for organization (Tier 2): %s", org_id)

        async for session in acquire_org_async_session(org_id):
            # Query all active messaging integrations
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
                        "syncs_triggered": 0,
                        "syncs_failed": 0,
                    },
                    "integration_results": [],
                }

            logger.info(
                "Found %d active messaging integration(s) for org (channels sync): %s",
                len(integrations),
                org_id,
            )

            # Dispatch sync tasks for each integration
            integration_results = []
            syncs_triggered = 0
            syncs_failed = 0

            for integration in integrations:
                integration_id = integration.integration_id
                integration_syncs = []

                # Get integration type
                int_type = integration.integration_type
                if isinstance(int_type, str):
                    int_type = IntegrationType(int_type)

                # Determine provider based on integration type
                if int_type == IntegrationType.TEAMS:
                    provider = "teams"
                    provider_config_key = "microsoft-teams"
                else:
                    # Future: Add Slack support
                    provider = str(int_type.value)
                    provider_config_key = str(int_type.value)

                logger.info(
                    "Dispatching messaging channels sync: org=%s integration=%s type=%s",
                    org_id,
                    integration_id,
                    int_type.value,
                )

                # 1. Sync channels
                try:
                    channels_task = sync_integration_data_task.apply_async(
                        kwargs={
                            "organisation_id": str(org_id),
                            "integration_id": str(integration_id),
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
                    integration_syncs.append(
                        {
                            "sync_type": "channels",
                            "task_id": channels_task.id,
                            "status": "triggered",
                        }
                    )
                    syncs_triggered += 1
                    logger.debug(
                        "Channels sync dispatched: org=%s integration=%s task=%s",
                        org_id,
                        integration_id,
                        channels_task.id,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to dispatch channels sync: org=%s integration=%s error=%s",
                        org_id,
                        integration_id,
                        str(e),
                        exc_info=True,
                    )
                    integration_syncs.append(
                        {
                            "sync_type": "channels",
                            "status": "failed",
                            "error": str(e),
                        }
                    )
                    syncs_failed += 1

                # 2. Sync chats (group and meeting chats, excludes 1:1)
                try:
                    chats_task = sync_integration_data_task.apply_async(
                        kwargs={
                            "organisation_id": str(org_id),
                            "integration_id": str(integration_id),
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
                    integration_syncs.append(
                        {
                            "sync_type": "chats",
                            "task_id": chats_task.id,
                            "status": "triggered",
                        }
                    )
                    syncs_triggered += 1
                    logger.debug(
                        "Chats sync dispatched: org=%s integration=%s task=%s",
                        org_id,
                        integration_id,
                        chats_task.id,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to dispatch chats sync: org=%s integration=%s error=%s",
                        org_id,
                        integration_id,
                        str(e),
                        exc_info=True,
                    )
                    integration_syncs.append(
                        {
                            "sync_type": "chats",
                            "status": "failed",
                            "error": str(e),
                        }
                    )
                    syncs_failed += 1

                integration_results.append(
                    {
                        "integration_id": str(integration_id),
                        "integration_type": int_type.value,
                        "user_id": str(integration.created_by_user_id)
                        if integration.created_by_user_id
                        else None,
                        "syncs": integration_syncs,
                    }
                )

            completed_at = datetime.now(UTC).isoformat()

            # Determine overall status
            if syncs_failed == 0:
                status = "completed"
            elif syncs_triggered == 0:
                status = "failed"
            else:
                status = "partial"

            logger.info(
                "Messaging channels sync complete for org %s: triggered=%d failed=%d",
                org_id,
                syncs_triggered,
                syncs_failed,
            )

            return {
                "status": status,
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": completed_at,
                "summary": {
                    "total_integrations": len(integrations),
                    "syncs_triggered": syncs_triggered,
                    "syncs_failed": syncs_failed,
                },
                "integration_results": integration_results,
            }

        # If async for didn't yield any sessions
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
