"""
Tier 2: Organization-level calendar sync task.

Syncs all calendar integrations for a specific organization.

This task:
1. Queries org DB for all active calendar integrations
2. For each integration, dispatches sync_integration_data_task
3. Returns org-level aggregated results

Architecture:
    sync_calendar_all_orgs_task [Tier 1]
        |
    sync_calendar_org_task [Tier 2 - this file] (one per org)
        |
    sync_integration_data_task (for each integration, via apply_async)

NOTE: This task dispatches sync_integration_data_task for actual sync work,
as calendar sync is simpler than email and can use the existing sync infrastructure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

logger = get_logger(__name__)

# Calendar integration types to sync
CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


@app.task(
    name="integrations.sync_calendar_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,  # 2 minutes between retries
    acks_late=True,
    soft_time_limit=600,  # 10 minutes soft limit (calendar is faster than email)
    time_limit=660,  # 11 minutes hard limit
)
def sync_calendar_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync all calendar integrations for one organization.

    Triggered by: sync_calendar_all_orgs_task (Tier 1)

    This task loops through all active calendar integrations for the org
    and dispatches sync_integration_data_task for each one.

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
                "syncs_triggered": 2,
                "syncs_failed": 0
            },
            "integration_results": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync all calendar integrations for the organization."""
        logger.info("Starting calendar sync for organization (Tier 2): %s", org_id)

        async for session in acquire_org_async_session(org_id):
            # Query all active calendar integrations
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
                KnowledgeIntegration.is_active == True,  # noqa: E712
                KnowledgeIntegration.connection_id.isnot(None),
            )
            result = await session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info("No active calendar integrations for org: %s", org_id)
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
                "Found %d active calendar integration(s) for org: %s",
                len(integrations),
                org_id,
            )

            # Dispatch sync task for each integration
            integration_results = []
            syncs_triggered = 0
            syncs_failed = 0

            for integration in integrations:
                integration_id = integration.integration_id

                # Get provider details - convert string to enum if needed
                int_type_str = integration.integration_type
                if isinstance(int_type_str, str):
                    int_type = IntegrationType(int_type_str)
                else:
                    int_type = int_type_str

                integration_type = int_type.value
                category, provider = categorize_integration_type(int_type)
                provider_config_key = to_nango_provider_key(int_type)

                logger.info(
                    "Dispatching calendar sync: org=%s integration=%s type=%s",
                    org_id,
                    integration_id,
                    integration_type,
                )

                try:
                    # Dispatch sync_integration_data_task for this integration
                    events_task = sync_integration_data_task.apply_async(
                        kwargs={
                            "organisation_id": str(org_id),
                            "integration_id": str(integration_id),
                            "category": category,
                            "provider": provider,
                            "provider_config_key": provider_config_key,
                            "sync_name": "events",
                            "sync_type": "INCREMENTAL",
                            "sync_variant": "base",
                            "incremental": True,
                        },
                        headers=correlation_headers(),
                    )

                    integration_results.append(
                        {
                            "integration_id": str(integration_id),
                            "integration_type": integration_type,
                            "user_id": str(integration.created_by_user_id)
                            if integration.created_by_user_id
                            else None,
                            "task_id": events_task.id,
                            "status": "triggered",
                        }
                    )
                    syncs_triggered += 1

                    logger.debug(
                        "Calendar sync dispatched: org=%s integration=%s task=%s",
                        org_id,
                        integration_id,
                        events_task.id,
                    )

                except Exception as e:
                    logger.error(
                        "Failed to dispatch calendar sync: org=%s integration=%s error=%s",
                        org_id,
                        integration_id,
                        str(e),
                        exc_info=True,
                    )
                    integration_results.append(
                        {
                            "integration_id": str(integration_id),
                            "integration_type": integration_type,
                            "status": "failed",
                            "error": str(e),
                        }
                    )
                    syncs_failed += 1

            completed_at = datetime.now(UTC).isoformat()

            # Determine overall status
            if syncs_failed == 0:
                status = "completed"
            elif syncs_triggered == 0:
                status = "failed"
            else:
                status = "partial"

            logger.info(
                "Calendar sync complete for org %s: triggered=%d failed=%d",
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
            "Calendar org sync failed: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying calendar org sync: org=%s attempt=%d/%d",
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
