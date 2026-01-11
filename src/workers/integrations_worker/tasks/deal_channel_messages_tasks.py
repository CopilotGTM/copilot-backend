"""
DEPRECATED: This module is deprecated. Use tasks/messaging/ instead.

The three-tier architecture in tasks/messaging/ provides:
- sync_deal_messages_all_orgs_task (Tier 1) - Global coordinator
- sync_deal_messages_org_task (Tier 2) - Organization-level processor

Benefits of three-tier architecture:
- Each organization gets its own Celery task (trackable, retryable)
- Better org-level parallelism and monitoring
- Consolidated with messaging sync in a single module

This file will be removed in a future release.

---

Original description:
Deal channel messages synchronization tasks.

This module provides Celery tasks for syncing messages from Teams channels
that users have linked to deals (via `messaging_deal_links`).

Key features:
- Scheduled sync (hourly via Celery Beat) for all organizations
- Deduplication by external_channel_id (same Teams channel, multiple integrations)
- Random integration selection for rate limit distribution
- Supports both Team channels and group/meeting chats

Architecture:
- Task Layer (this file): Thin orchestrators that handle Celery mechanics
- Service Layer: Business logic in DealChannelMessagesService
- Provider Layer: API calls in providers/messaging/teams.py

Tasks:
- trigger_deal_channel_messages_sync_all_orgs: Coordinator task (Beat)
- sync_deal_channel_messages_task: Per-organization worker task
"""

# mypy: disable-error-code="arg-type,call-overload,union-attr"

from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.deal_channel_messages_service import (
    DealChannelMessagesService,
)
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    name="integrations.trigger_deal_channel_messages_sync_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def trigger_deal_channel_messages_sync_all_orgs() -> dict[str, Any]:
    """
    Trigger deal channel message sync for all approved organisations.

    This is the Celery Beat entry point that runs hourly.
    It enqueues one sync_deal_channel_messages_task per approved organisation.

    Returns:
        Dictionary with count of organisations queued
    """
    logger.info("🚀 Starting deal channel messages sync for all organisations")

    async def _run() -> int:
        org_ids: list[UUID] = []

        # Get all approved organizations
        async for master_session in acquire_master_async_session():
            # Query approved organisations
            stmt = select(Organisation.organisation_id).where(
                Organisation.status == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        # Enqueue a sync task for each organisation
        if not org_ids:
            logger.warning("No approved organisations found for deal channel messages sync")
            return 0

        logger.info("Found %d approved organisation(s)", len(org_ids))

        for org_id in org_ids:
            sync_deal_channel_messages_task.apply_async(
                kwargs={"organisation_id": str(org_id)},
                headers=correlation_headers(),
            )

        return len(org_ids)

    count = run_async(_run())
    logger.info("✅ Enqueued deal channel messages sync for %d organisations", count)
    return {"organisations_queued": count}


@app.task(
    name="integrations.sync_deal_channel_messages",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_deal_channel_messages_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Sync messages for deal-linked channels in an organisation.

    This task delegates all business logic to DealChannelMessagesService:
    1. Queries MessagingDealLink for user-selected channels
    2. Groups by external_channel_id to avoid duplicate fetches
    3. Randomly selects integration for load distribution
    4. Fetches messages via Teams Graph API (Nango proxy)
    5. Stores in messaging_messages table

    Args:
        organisation_id: Organisation UUID string

    Returns:
        Dictionary with sync results summary
    """
    logger.info("📨 Starting deal channel messages sync for org: %s", organisation_id)

    org_uuid = UUID(organisation_id)

    # Internal async function to perform the sync
    async def _sync() -> dict[str, Any]:
        # Acquire organisation session
        async for org_session in acquire_org_async_session(org_uuid):
            # Perform the sync via service layer
            service = DealChannelMessagesService(org_session, org_uuid)
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

            return {
                "status": result.status,
                "channels_synced": result.channels_synced,
                "channels_failed": result.channels_failed,
                "messages_synced": result.messages_synced,
                "errors": errors if errors else None,
            }

        # Fallback if no session yielded
        return {
            "status": "failed",
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_sync())
    except Exception as e:
        logger.error(
            "Deal channel messages sync failed for org %s: %s",
            organisation_id,
            str(e),
            exc_info=True,
        )
        # Retry with exponential backoff
        try:
            raise self.retry(exc=e, countdown=60 * (2**self.request.retries))
        except self.MaxRetriesExceededError:
            return {
                "status": "failed",
                "error": str(e),
                "error_type": type(e).__name__,
            }


# Convenience alias
sync_deal_channel_messages = sync_deal_channel_messages_task
