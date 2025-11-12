"""
Teams-specific synchronization tasks.

Handles automatic and on-demand Teams synchronization:
- Team channels sync (standard/private channels within Teams)
- Chats sync (group and meeting chats - excludes 1:1)
- Scheduled sync for all organizations (via Celery Beat every 3 hours)
- Manual sync for specific integration (triggered from frontend)

Note: 1:1 direct chats are excluded from sync - only group and meeting chats are synced.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_teams_channels",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def sync_teams_channels_task(
    organisation_id: str | None = None,
    integration_id: str | None = None,
) -> dict[str, Any]:
    """
    Sync Teams channels AND chats for all organizations or specific org/integration.

    This task syncs BOTH:
    - Team channels (standard/private channels within Teams)
    - Chats (group and meeting chats only - excludes 1:1)

    The task can be:
    1. Scheduled via Celery Beat to run every 3 hours for all orgs (no args)
    2. Manually triggered from frontend (refresh button) for specific integration
    3. Triggered via API endpoint for specific org

    Args:
        organisation_id: Optional - sync only this organization
        integration_id: Optional - sync only this specific integration (requires organisation_id)

    Returns:
        Dictionary with sync summary and results including both channels and chats
    """

    async def _sync_all_orgs() -> dict[str, Any]:
        """Sync Teams channels and chats for all approved organizations."""
        logger.info("🚀 Starting scheduled Teams channels+chats sync for all organizations")

        # Get all approved organizations
        async for master_session in acquire_master_async_session():
            stmt = select(Organisation.organisation_id).where(
                Organisation.status == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning("No approved organizations found for Teams channels+chats sync")
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
        """Sync Teams channels and chats for a specific organization."""
        logger.info("Processing Teams channels+chats for organization: %s", org_id)

        async for org_session in acquire_org_async_session(org_id):
            # Build query for active Teams integrations
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_type == IntegrationType.TEAMS,
                KnowledgeIntegration.is_active == True,  # noqa: E712
                KnowledgeIntegration.connection_id.isnot(None),
            )

            # Filter to specific integration if provided
            if specific_integration_id:
                stmt = stmt.where(KnowledgeIntegration.integration_id == specific_integration_id)

            result = await org_session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info("No active Teams integrations found for org: %s", org_id)
                return {
                    "org_id": str(org_id),
                    "integrations_count": 0,
                    "syncs_triggered": 0,
                }

            logger.info(
                "Found %d active Teams integration(s) for org: %s",
                len(integrations),
                org_id,
            )

            # Trigger both channels and chats sync for each integration
            sync_results = []
            for integration in integrations:
                integration_syncs = []

                # 1. Sync Team channels
                try:
                    channels_task = sync_integration_data_task.apply_async(
                        kwargs={
                            "organisation_id": str(org_id),
                            "integration_id": str(integration.integration_id),
                            "category": "messaging",
                            "provider": "teams",
                            "provider_config_key": "microsoft-teams",
                            "sync_name": "channels",
                            "sync_type": "INCREMENTAL",
                            "sync_variant": "base",
                            "incremental": True,
                        }
                    )

                    integration_syncs.append(
                        {
                            "type": "channels",
                            "task_id": channels_task.id,
                            "status": "submitted",
                        }
                    )

                    logger.info(
                        "✅ Channels sync triggered: org=%s integration=%s task=%s",
                        org_id,
                        integration.integration_id,
                        channels_task.id,
                    )
                except Exception as e:
                    logger.error(
                        "❌ Failed to trigger channels sync: org=%s integration=%s error=%s",
                        org_id,
                        integration.integration_id,
                        str(e),
                        exc_info=True,
                    )
                    integration_syncs.append(
                        {
                            "type": "channels",
                            "status": "failed",
                            "error": str(e),
                        }
                    )

                # 2. Sync chats (1:1, group, meeting)
                try:
                    chats_task = sync_integration_data_task.apply_async(
                        kwargs={
                            "organisation_id": str(org_id),
                            "integration_id": str(integration.integration_id),
                            "category": "messaging",
                            "provider": "teams",
                            "provider_config_key": "microsoft-teams",
                            "sync_name": "chats",
                            "sync_type": "INCREMENTAL",
                            "sync_variant": "base",
                            "incremental": True,
                        }
                    )

                    integration_syncs.append(
                        {
                            "type": "chats",
                            "task_id": chats_task.id,
                            "status": "submitted",
                        }
                    )

                    logger.info(
                        "✅ Chats sync triggered: org=%s integration=%s task=%s",
                        org_id,
                        integration.integration_id,
                        chats_task.id,
                    )
                except Exception as e:
                    logger.error(
                        "❌ Failed to trigger chats sync: org=%s integration=%s error=%s",
                        org_id,
                        integration.integration_id,
                        str(e),
                        exc_info=True,
                    )
                    integration_syncs.append(
                        {
                            "type": "chats",
                            "status": "failed",
                            "error": str(e),
                        }
                    )

                sync_results.append(
                    {
                        "integration_id": str(integration.integration_id),
                        "user_id": str(integration.created_by_user_id)
                        if integration.created_by_user_id
                        else None,
                        "syncs": integration_syncs,
                        "total_submitted": len(
                            [s for s in integration_syncs if s["status"] == "submitted"]
                        ),
                    }
                )

            return {
                "org_id": str(org_id),
                "integrations_count": len(integrations),
                "syncs_triggered": sum(r["total_submitted"] for r in sync_results),
                "results": sync_results,
            }

    async def _execute() -> dict[str, Any]:
        """Execute the appropriate sync based on parameters."""
        if organisation_id:
            org_uuid = UUID(organisation_id)
            integration_uuid = UUID(integration_id) if integration_id else None

            logger.info(
                "Syncing Teams channels+chats: org=%s integration=%s",
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
            "Teams channels+chats sync failed: org=%s integration=%s error=%s",
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
sync_teams_channels = sync_teams_channels_task
