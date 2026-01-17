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
    DealChannelMessagesService.sync_all_deal_channels()
        |
    Teams Graph API (via Nango proxy) -> messaging_messages table
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task

from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.deal_channel_messages_service import (
    DealChannelMessagesService,
)
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_deal_messages_org",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute between retries
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=600,  # 10 minutes soft limit
    time_limit=660,  # 11 minutes hard limit
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
        """Sync deal-linked channel messages for the organization."""
        logger.info("Starting deal messages sync for organization (Tier 2): %s", org_id)

        async for org_session in acquire_org_async_session(org_id):
            # Perform the sync via service layer
            service = DealChannelMessagesService(org_session, org_id)
            result = await service.sync_all_deal_channels()

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
