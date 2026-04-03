"""
Tier 2: Organization-level deal messages sync task.

Syncs messages from deal-linked channels for a specific organization.

This task:
1. Uses DealChannelMessagesService to sync all deal-linked channel messages
2. Handles deduplication by external_channel_id
3. Returns org-level aggregated results

Architecture:
    sync_deal_messages_all_orgs_task [Tier 1]
        |
    sync_deal_messages_org_task [Tier 2 - this file] (one per org)
        |
    DealChannelMessagesService.sync_all_deal_channels_fresh()
        |
    Teams Graph API (via Nango proxy) -> messaging_messages table

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    - The service manages its own sessions internally
    - Sessions are acquired only when DB operations are needed
    - Sessions are released before external API calls
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.deal_channel_messages_service import (
    DealChannelMessagesService,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_deal_messages_org"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_deal_messages_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync deal-linked channel messages for one organization.

    Triggered by: sync_deal_messages_all_orgs_task (Tier 1)

    This task delegates all business logic to DealChannelMessagesService:
    1. Queries MessagingDealLink for user-selected channels
    2. Groups by external_channel_id to avoid duplicate fetches
    3. Randomly selects integration for load distribution
    4. Fetches messages via Teams Graph API (Nango proxy)
    5. Stores in messaging_messages table

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
                "channels_synced": 5,
                "channels_failed": 1,
                "messages_synced": 150
            },
            "errors": [...] (if any)
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync deal-linked channel messages for the organization.

        Uses fresh session pattern:
        - Service manages its own sessions internally
        - No session held during API calls
        """
        logger.info("Starting deal messages sync for organization (Tier 2): %s", org_id)

        # Service manages its own sessions - no external session needed
        # Pass None for session since we'll use sync_all_deal_channels_fresh
        service = DealChannelMessagesService(None, org_id)
        result = await service.sync_all_deal_channels_fresh()

        # Extract errors from failed channel results
        errors = [
            {
                "channel_id": str(cr.channel_id),
                "channel_name": cr.channel_name,
                "error": cr.error,
            }
            for cr in result.channel_results
            if cr.status == "failed" and cr.error
        ]

        completed_at = datetime.now(UTC).isoformat()

        logger.info(
            "Deal messages sync complete for org %s: channels_synced=%d channels_failed=%d messages=%d",
            org_id,
            result.channels_synced,
            result.channels_failed,
            result.messages_synced,
        )

        return {
            "status": result.status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "channels_synced": result.channels_synced,
                "channels_failed": result.channels_failed,
                "messages_synced": result.messages_synced,
            },
            "errors": errors if errors else None,
        }

    try:
        return run_async(_sync_org())
    except SoftTimeLimitExceeded:
        logger.error(
            "Deal messages org sync timed out: org=%s after %ds",
            organisation_id,
            _L["soft"],
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
            "Deal messages org sync failed: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry with exponential backoff
        if self.request.retries < self.max_retries:
            countdown = 60 * (2**self.request.retries)
            logger.info(
                "Retrying deal messages org sync: org=%s attempt=%d/%d countdown=%ds",
                organisation_id,
                self.request.retries + 1,
                self.max_retries,
                countdown,
            )
            raise self.retry(exc=e, countdown=countdown) from e

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


# Convenience alias
sync_deal_messages_org = sync_deal_messages_org_task
