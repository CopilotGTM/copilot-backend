"""
Tier 3: Integration-level deal messages sync task.

Syncs deal-linked channel messages for a specific integration (manual trigger).

This task is used for:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

NOTE: This task is NOT used by the scheduled sync (Celery Beat).
The scheduler uses Tier 1 -> Tier 2.

Architecture:
    Frontend/API (manual trigger)
        |
    sync_deal_messages_integration_task [Tier 3 - this file]
        |
    DealChannelMessagesService.sync_all_deal_channels_fresh(integration_id=...)

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Validate integration exists and is active, release session
    2. Service manages its own sessions internally for sync operations
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
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.deal_channel_messages_service import (
    DealChannelMessagesService,
)
from src.service.integrations.sync_health import record_sync_failure
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)


@dataclass
class IntegrationValidationData:
    """Data extracted from integration validation."""

    integration_type: str
    is_valid: bool
    error: str | None = None


# Messaging integration types
MESSAGING_INTEGRATION_TYPES = (IntegrationType.TEAMS,)

_TASK_NAME = "integrations.sync_deal_messages_integration"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_deal_messages_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """
    Tier 3: Sync deal-linked messages for a specific integration.

    Triggered by: Frontend refresh button, API, manual intervention
    NOT triggered by: Scheduled sync (Tier 1/2)

    This task syncs messages from deal-linked channels that are accessible
    via the specified integration.

    Args:
        organisation_id: The organization UUID string
        integration_id: The specific integration UUID string

    Returns:
        Dictionary with sync results:
        {
            "status": "success",
            "organisation_id": "uuid",
            "integration_id": "uuid",
            "integration_type": "teams",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "channels_synced": 5,
                "channels_failed": 0,
                "messages_synced": 150
            }
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync deal messages for the specific integration.

        Uses fresh session pattern:
        1. Quick session to validate integration, release
        2. Service manages its own sessions for sync operations
        """
        logger.info(
            "Starting manual deal messages sync (Tier 3): org=%s integration=%s",
            org_id,
            int_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Validate integration (quick query, then release session)
        # ─────────────────────────────────────────────────────────────────────
        validation: IntegrationValidationData | None = None

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == int_id,
                col(KnowledgeIntegration.integration_type).in_(MESSAGING_INTEGRATION_TYPES),
            )
            result = await session.execute(stmt)
            integration = result.scalar_one_or_none()

            if not integration:
                validation = IntegrationValidationData(
                    integration_type="",
                    is_valid=False,
                    error="Integration not found or not a messaging integration",
                )
            elif not integration.is_active:
                validation = IntegrationValidationData(
                    integration_type="",
                    is_valid=False,
                    error="Integration is not active",
                )
            elif not integration.connection_id:
                validation = IntegrationValidationData(
                    integration_type="",
                    is_valid=False,
                    error="Integration is not connected (no connection_id)",
                )
            else:
                # Extract integration type
                int_type = integration.integration_type
                if isinstance(int_type, str):
                    int_type = IntegrationType(int_type)
                validation = IntegrationValidationData(
                    integration_type=int_type.value,
                    is_valid=True,
                )
        # Session is now released

        if not validation:
            return {
                "status": "failed",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": "Failed to acquire database session",
            }

        if not validation.is_valid:
            return {
                "status": "failed",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": validation.error,
            }

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: Sync deal messages (service manages its own sessions)
        # ─────────────────────────────────────────────────────────────────────
        service = DealChannelMessagesService(None, org_id)
        sync_result = await service.sync_all_deal_channels_fresh(integration_id=int_id)

        # Extract errors from failed channel results
        errors = [
            {
                "channel_id": str(cr.channel_id),
                "channel_name": cr.channel_name,
                "error": cr.error,
            }
            for cr in sync_result.channel_results
            if cr.status == "failed" and cr.error
        ]

        completed_at = datetime.now(UTC).isoformat()

        logger.info(
            "Manual deal messages sync complete: org=%s integration=%s channels=%d messages=%d",
            org_id,
            int_id,
            sync_result.channels_synced,
            sync_result.messages_synced,
        )

        return {
            "status": sync_result.status,
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "integration_type": validation.integration_type,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "channels_synced": sync_result.channels_synced,
                "channels_failed": sync_result.channels_failed,
                "messages_synced": sync_result.messages_synced,
            },
            "errors": errors if errors else None,
        }

    try:
        result = run_async(_sync_integration())

        if result.get("status") in ("success", "completed"):
            logger.info(
                "Manual deal messages sync complete: org=%s integration=%s",
                organisation_id,
                integration_id,
            )
        else:
            logger.warning(
                "Manual deal messages sync failed: org=%s integration=%s error=%s",
                organisation_id,
                integration_id,
                result.get("error"),
            )

        return result

    except SoftTimeLimitExceeded:
        logger.error(
            "Manual deal messages sync timed out: org=%s integration=%s after %ds",
            organisation_id,
            integration_id,
            _L["soft"],
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
            logger.warning("Failed to record timeout failure for %s", integration_id)
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
            "Manual deal messages sync exception: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying manual deal messages sync: org=%s integration=%s attempt=%d/%d",
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
sync_deal_messages_integration = sync_deal_messages_integration_task
