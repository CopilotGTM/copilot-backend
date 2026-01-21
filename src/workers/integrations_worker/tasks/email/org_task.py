"""
Tier 2: Organization-level email sync task.

Syncs all email integrations for a specific organization.

This task:
1. Queries org DB for all active email integrations
2. For each integration, calls EmailDeltaSyncService
3. Returns org-level aggregated results

Architecture:
    sync_email_all_orgs_task [Tier 1]
        ↓
    sync_email_org_task [Tier 2 - this file] (one per org)
        ↓
    EmailDeltaSyncService._sync_integration() (for each integration, inline)

NOTE: This task uses EmailDeltaSyncService directly (NOT sync_integration_data_task)
because email sync requires:
- Combined folders + messages sync per integration
- Per-folder delta link management
- Different orchestration than calendar

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections unnecessarily:
    1. Session 1: Query integration IDs, release session
    2. For each integration: call EmailDeltaSyncService (which manages its own sessions)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.email_delta_sync_service import (
    EMAIL_INTEGRATION_TYPES,
    EmailDeltaSyncService,
)
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@dataclass
class IntegrationInfo:
    """Minimal integration data needed for orchestration."""

    integration_id: UUID
    integration_type: str


@app.task(
    name="integrations.sync_email_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,  # 2 minutes between retries
    acks_late=True,
    soft_time_limit=1800,  # 30 minutes soft limit (may have multiple integrations with full body)
    time_limit=1860,  # 31 minutes hard limit
)
def sync_email_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync all email integrations for one organization.

    Triggered by: sync_email_all_orgs_task (Tier 1)

    This task loops through all active email integrations for the org
    and calls EmailDeltaSyncService for each one.

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
                "total_folders_synced": 10,
                "total_messages_synced": 250
            },
            "integration_results": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync all email integrations for the organization.

        Uses fresh session pattern:
        1. Session 1: Query integration IDs, release session
        2. For each integration: call EmailDeltaSyncService (manages own sessions)
        """
        logger.info("Starting email sync for organization (Tier 2): %s", org_id)

        # Phase 1: Query integration IDs only, then release session
        # EmailDeltaSyncService manages its own sessions for the actual sync
        integration_list: list[IntegrationInfo] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(
                col(KnowledgeIntegration.integration_id),
                col(KnowledgeIntegration.integration_type),
            ).where(
                col(KnowledgeIntegration.integration_type).in_(EMAIL_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(stmt)
            rows = result.all()

            for row in rows:
                int_type = row.integration_type
                integration_list.append(
                    IntegrationInfo(
                        integration_id=row.integration_id,
                        integration_type=(
                            int_type.value if hasattr(int_type, "value") else str(int_type)
                        ),
                    )
                )
        # Session is now released

        if not integration_list:
            logger.info("No active email integrations for org: %s", org_id)
            return {
                "status": "completed",
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_integrations": 0,
                    "successful": 0,
                    "failed": 0,
                    "total_folders_synced": 0,
                    "total_messages_synced": 0,
                },
                "integration_results": [],
            }

        logger.info(
            "Found %d active email integration(s) for org: %s",
            len(integration_list),
            org_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: Sync each integration (service manages its own sessions)
        # ─────────────────────────────────────────────────────────────────────
        service = EmailDeltaSyncService()
        integration_results = []
        successful = 0
        failed = 0
        total_folders = 0
        total_messages = 0

        for info in integration_list:
            logger.info(
                "Syncing email integration: org=%s integration=%s type=%s",
                org_id,
                info.integration_id,
                info.integration_type,
            )

            try:
                # Call service to sync this integration
                # Service manages its own database sessions
                sync_result = await service.sync_integration(
                    organisation_id=org_id,
                    integration_id=info.integration_id,
                )

                # Track results
                if sync_result.get("status") == "success":
                    successful += 1
                    total_folders += sync_result.get("folders_synced", 0)
                    total_messages += sync_result.get("messages_synced", 0)
                else:
                    failed += 1

                integration_results.append(
                    {
                        "integration_id": str(info.integration_id),
                        "integration_type": info.integration_type,
                        "status": sync_result.get("status", "unknown"),
                        "folders_synced": sync_result.get("folders_synced", 0),
                        "messages_synced": sync_result.get("messages_synced", 0),
                        "error": sync_result.get("error"),
                    }
                )

            except Exception as e:
                logger.error(
                    "Failed to sync email integration: org=%s integration=%s error=%s",
                    org_id,
                    info.integration_id,
                    str(e),
                    exc_info=True,
                )
                failed += 1
                integration_results.append(
                    {
                        "integration_id": str(info.integration_id),
                        "integration_type": info.integration_type,
                        "status": "failed",
                        "error": str(e),
                    }
                )

        completed_at = datetime.now(UTC).isoformat()

        # Determine overall status
        if failed == 0:
            status = "completed"
        elif successful == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Email sync complete for org %s: successful=%d failed=%d folders=%d messages=%d",
            org_id,
            successful,
            failed,
            total_folders,
            total_messages,
        )

        # Dispatch Task 3: Match emails to deals
        if total_messages > 0:
            app.send_task(
                "deal.match_email_to_deals_org",
                args=[organisation_id],
                queue="deal",
            )
            logger.info("Dispatched email matching task for org %s", org_id)

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "total_integrations": len(integration_list),
                "successful": successful,
                "failed": failed,
                "total_folders_synced": total_folders,
                "total_messages_synced": total_messages,
            },
            "integration_results": integration_results,
        }

    try:
        return run_async(_sync_org())
    except Exception as e:
        logger.error(
            "Email org sync failed: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying email org sync: org=%s attempt=%d/%d",
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
