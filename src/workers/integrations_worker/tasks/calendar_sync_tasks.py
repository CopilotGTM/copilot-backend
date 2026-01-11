"""
DEPRECATED: This module is deprecated. Use tasks/calendar/ instead.

The three-tier architecture in tasks/calendar/ provides:
- sync_calendar_all_orgs_task (Tier 1) - Global coordinator
- sync_calendar_org_task (Tier 2) - Organization-level processor
- sync_calendar_integration_task (Tier 3) - Manual trigger

Benefits of three-tier architecture:
- Each organization gets its own Celery task (trackable, retryable)
- Better org-level parallelism and monitoring
- Follows the pattern established by email sync

This file will be removed in a future release.

---

Original description:
Calendar-specific synchronization tasks.

Handles automatic and on-demand calendar event synchronization:
- Google Calendar events sync
- Outlook Calendar events sync
- Scheduled sync for all organizations (via Celery Beat every 3 hours)
- Manual sync for specific integration (triggered from frontend)

This provides a fallback mechanism for webhook-driven syncs, ensuring
events are captured even if webhook processing fails.
"""

# mypy: disable-error-code="arg-type,call-overload,union-attr"

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables import Organisation, OrganisationStatus
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
    name="integrations.sync_calendar_events",
    bind=False,
    max_retries=0,  # Coordinator doesn't retry; workers retry independently
    acks_late=True,
)
def sync_calendar_events_task(
    organisation_id: str | None = None,
    integration_id: str | None = None,
) -> dict[str, Any]:
    """
    Sync calendar events for all organizations or specific org/integration.

    This task syncs events from:
    - Google Calendar
    - Outlook Calendar

    The task can be:
    1. Scheduled via Celery Beat to run every 3 hours for all orgs (no args)
    2. Manually triggered from frontend (refresh button) for specific integration
    3. Triggered via API endpoint for specific org

    Args:
        organisation_id: Optional - sync only this organization
        integration_id: Optional - sync only this specific integration (requires organisation_id)

    Returns:
        Dictionary with sync summary and results
    """

    async def _sync_all_orgs() -> dict[str, Any]:
        """Sync calendar events for all approved organizations."""
        logger.info("Starting scheduled calendar events sync for all organizations")

        # Get all approved organizations
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(Organisation.organisation_id).where(
                Organisation.status == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning("No approved organizations found for calendar events sync")
            return {
                "status": "completed",
                "organizations_processed": 0,
                "total_syncs_triggered": 0,
            }

        logger.info("Found %d approved organization(s)", len(org_ids))

        results = []
        total_syncs = 0

        for org_id in org_ids:
            try:
                org_result = await _sync_organization(org_id, None)
                results.append(org_result)
                total_syncs += org_result.get("syncs_triggered", 0)
            except Exception as e:
                logger.error(
                    "Failed to process organization %s: %s",
                    org_id,
                    str(e),
                    exc_info=True,
                )
                results.append(
                    {
                        "org_id": str(org_id),
                        "status": "failed",
                        "error": str(e),
                    }
                )

        return {
            "status": "completed",
            "organizations_processed": len(org_ids),
            "organizations_with_integrations": len(
                [r for r in results if r.get("integrations_count", 0) > 0]
            ),
            "total_syncs_triggered": total_syncs,
            "results": results,
        }

    async def _sync_organization(
        org_id: UUID, specific_integration_id: UUID | None
    ) -> dict[str, Any]:
        """Sync calendar events for a specific organization."""
        logger.info("Processing calendar events for organization: %s", org_id)

        async for org_session in acquire_org_async_session(org_id):
            # Build query for active calendar integrations
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
                KnowledgeIntegration.is_active == True,  # noqa: E712
                KnowledgeIntegration.connection_id.isnot(None),
            )

            # Filter to specific integration if provided
            if specific_integration_id:
                stmt = stmt.where(KnowledgeIntegration.integration_id == specific_integration_id)

            result = await org_session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info("No active calendar integrations found for org: %s", org_id)
                return {
                    "org_id": str(org_id),
                    "integrations_count": 0,
                    "syncs_triggered": 0,
                }

            logger.info(
                "Found %d active calendar integration(s) for org: %s",
                len(integrations),
                org_id,
            )

            # Trigger events sync for each integration
            sync_results = []
            for integration in integrations:
                # Get provider details - convert string to enum if needed
                int_type_str = integration.integration_type
                if isinstance(int_type_str, str):
                    int_type = IntegrationType(int_type_str)
                else:
                    int_type = int_type_str

                category, provider = categorize_integration_type(int_type)
                provider_config_key = to_nango_provider_key(int_type)

                try:
                    events_task = sync_integration_data_task.apply_async(
                        kwargs={
                            "organisation_id": str(org_id),
                            "integration_id": str(integration.integration_id),
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

                    sync_results.append(
                        {
                            "integration_id": str(integration.integration_id),
                            "integration_type": int_type.value,
                            "user_id": str(integration.created_by_user_id)
                            if integration.created_by_user_id
                            else None,
                            "sync": {
                                "type": "events",
                                "task_id": events_task.id,
                                "status": "submitted",
                                "provider": provider,
                            },
                        }
                    )

                    logger.info(
                        "Calendar sync triggered: org=%s integration=%s provider=%s task=%s",
                        org_id,
                        integration.integration_id,
                        provider,
                        events_task.id,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to trigger calendar sync: org=%s integration=%s error=%s",
                        org_id,
                        integration.integration_id,
                        str(e),
                        exc_info=True,
                    )
                    sync_results.append(
                        {
                            "integration_id": str(integration.integration_id),
                            "integration_type": int_type.value,
                            "sync": {
                                "type": "events",
                                "status": "failed",
                                "provider": provider,
                                "error": str(e),
                            },
                        }
                    )

            return {
                "org_id": str(org_id),
                "integrations_count": len(integrations),
                "syncs_triggered": len(
                    [r for r in sync_results if r.get("sync", {}).get("status") == "submitted"]
                ),
                "results": sync_results,
            }

        # If async for didn't yield any sessions
        return {
            "org_id": str(org_id),
            "integrations_count": 0,
            "syncs_triggered": 0,
            "error": "Failed to acquire database session",
        }

    async def _execute() -> dict[str, Any]:
        """Execute the appropriate sync based on parameters."""
        if organisation_id:
            org_uuid = UUID(organisation_id)
            integration_uuid = UUID(integration_id) if integration_id else None

            logger.info(
                "Syncing calendar events: org=%s integration=%s",
                org_uuid,
                integration_uuid or "all",
            )

            return await _sync_organization(org_uuid, integration_uuid)
        else:
            # Sync all organizations (scheduled job)
            return await _sync_all_orgs()

    try:
        return run_async(_execute())
    except Exception as e:
        logger.error(
            "Calendar events sync failed: org=%s integration=%s error=%s",
            organisation_id or "all",
            integration_id or "all",
            str(e),
            exc_info=True,
        )
        return {
            "status": "failed",
            "error": str(e),
            "error_type": type(e).__name__,
        }


# Convenience alias for calling from other parts of the codebase
sync_calendar_events = sync_calendar_events_task
