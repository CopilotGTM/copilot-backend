"""Celery tasks for deal timeline batch operations.

This module provides Celery tasks for batch timeline sync operations:
- sync_deal_timeline_task: Sync all timeline events for a single deal
- sync_org_timeline_task: Sync timeline events for all deals in an org
- exclude_timeline_event_task: Mark a timeline event as excluded

These tasks are designed for:
- Backfilling timeline events for existing associations
- Repairing/reconciling timeline data
- Manual triggers from admin interfaces

NOTE: Real-time timeline creation happens INLINE in the matcher services
(DealEmailMatcher, DealMeetingMatcher) - NOT via these tasks. This avoids
the overhead of creating separate tasks for each event.

See docs/Development Documentation/deal-timeline-feature.md for design details.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.infra.database.factory import get_async_provider
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.deal_timeline_service import DealTimelineService
from src.service.crm.deal_timeline_sync_service import DealTimelineSyncService
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


# =============================================================================
# Batch Sync Tasks (Recommended for backfill/repair)
# =============================================================================


@app.task(name="deal.trigger_timeline_sync_all_orgs")
def trigger_timeline_sync_all_orgs() -> dict[str, int]:
    """Trigger org-wide timeline sync for all active organisations.

    This is intended for:
    - One-time backfill after deploying the timeline feature
    - Manual reconciliation runs

    It enqueues one `deal.sync_org_timeline` task per approved organisation.
    """

    logger.info("Triggering org-wide timeline sync for all organisations")

    async def _run() -> int:
        provider = get_async_provider()
        async with provider.master_session() as session:
            stmt = select(Organisation).where(Organisation.status == OrganisationStatus.APPROVED)
            result = await session.execute(stmt)
            orgs = result.scalars().all()

        logger.info("Found %d active organisations for timeline sync", len(orgs))

        for org in orgs:
            sync_org_timeline_task.apply_async(
                args=[str(org.organisation_id)],
                headers=correlation_headers(),
            )

        return len(orgs)

    count = run_async(_run())
    logger.info("Enqueued org timeline sync for %d organisations", count)
    return {"organisations_queued": count}


@app.task(bind=True, name="deal.sync_deal_timeline", acks_late=True, max_retries=3)
def sync_deal_timeline_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    event_types: list[str] | None = None,
) -> dict[str, int]:
    """
    Sync all timeline events for a single deal.

    Creates timeline events for all associations that don't already have them.
    Uses the unique constraint to avoid duplicates (idempotent).

    This task is for:
    - Backfilling timeline for a deal that was created before timeline feature
    - Repairing timeline data after issues
    - Manual sync from admin interface

    NOT for real-time creation - that happens inline in matchers.

    Args:
        organisation_id: Organisation UUID (string for JSON serialization)
        deal_id: Deal UUID
        event_types: Optional filter - ["EMAIL", "MEETING"] to sync specific types

    Returns:
        Stats dict: {emails_synced, meetings_synced, total_created, total_skipped}
    """
    logger.info(f"Starting timeline sync for deal {deal_id} in org {organisation_id}")

    async def _run():
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        # Convert string event types to enum if provided
        parsed_types = None
        if event_types:
            parsed_types = [TimelineEventType(t) for t in event_types]

        async for session in acquire_org_async_session(org_uuid):
            service = DealTimelineSyncService(session, org_uuid)
            result = await service.sync_deal_timeline(deal_uuid, parsed_types)
            await session.commit()
            return result

    try:
        result = run_async(_run())
        logger.info(f"Timeline sync completed for deal {deal_id}: {result}")
        return result
    except Exception as e:
        logger.error(f"Timeline sync failed for deal {deal_id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e


@app.task(bind=True, name="deal.sync_org_timeline", acks_late=True, max_retries=2)
def sync_org_timeline_task(
    self: Task,
    organisation_id: str,
    deal_ids: list[str] | None = None,
    batch_size: int = 50,
) -> dict[str, int]:
    """
    Sync timeline events for all deals in an organisation (or specific deals).

    Processes in batches to manage memory and transaction size.
    Commits after each batch for progress durability.

    This task is for:
    - Initial backfill when timeline feature is deployed
    - Periodic reconciliation (if needed)
    - Manual org-wide sync from admin

    Args:
        organisation_id: Organisation UUID
        deal_ids: Optional list of specific deal IDs to sync
        batch_size: Number of deals per batch (default 50)

    Returns:
        Aggregate stats dict
    """
    logger.info(
        f"Starting org-wide timeline sync for org {organisation_id} "
        f"(deals={len(deal_ids) if deal_ids else 'all'}, batch_size={batch_size})"
    )

    async def _run():
        org_uuid = UUID(organisation_id)
        parsed_deal_ids = [UUID(d) for d in deal_ids] if deal_ids else None

        async for session in acquire_org_async_session(org_uuid):
            service = DealTimelineSyncService(session, org_uuid)
            return await service.sync_organisation_timeline(
                deal_ids=parsed_deal_ids,
                batch_size=batch_size,
            )

    try:
        result = run_async(_run())
        logger.info(f"Org timeline sync completed for {organisation_id}: {result}")
        return result
    except Exception as e:
        logger.error(f"Org timeline sync failed for {organisation_id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=300) from e


# =============================================================================
# Status Update Tasks
# =============================================================================


@app.task(bind=True, name="deal.exclude_timeline_event", acks_late=True, max_retries=2)
def exclude_timeline_event_task(
    self: Task,
    organisation_id: str,
    timeline_event_id: str,
) -> dict[str, bool]:
    """
    Mark a timeline event as excluded (soft-hide from timeline).

    Called when an association is rejected or cancelled.

    Args:
        organisation_id: Organisation UUID
        timeline_event_id: Timeline event UUID to exclude

    Returns:
        Dictionary with success status
    """
    logger.info(f"Excluding timeline event {timeline_event_id}")

    async def _run():
        org_uuid = UUID(organisation_id)
        event_uuid = UUID(timeline_event_id)

        async for session in acquire_org_async_session(org_uuid):
            service = DealTimelineService(session, org_uuid)
            updated = await service.exclude_timeline_event(event_uuid, datetime.now(UTC))
            await session.commit()
            return {"success": updated}

    try:
        result = run_async(_run())
        logger.info(f"Timeline event exclusion result: {result}")
        return result
    except Exception as e:
        logger.error(
            f"Failed to exclude timeline event {timeline_event_id}: {e}",
            exc_info=True,
        )
        raise self.retry(exc=e, countdown=30) from e
