"""Deprecated timeline sync tasks.

This module provides Celery tasks for syncing timeline events to deals.

ARCHITECTURE (Three-Tier System):
---------------------------------
    Tier 1: trigger_timeline_sync_all_orgs_task (all orgs - coordinator)
                        │
                        └─→ Dispatches Tier 2 for each approved org

    Tier 2: sync_org_timeline_task (org-level)
                        │
                        └─→ Processes all deals in the org

    Tier 3: sync_deal_timeline_task (per-deal)
                        │
                        └─→ Syncs all timeline events for a single deal

Supported Event Types:
- EMAIL: Email associations
- MEETING: Calendar event associations
- STAGE_CHANGE: Deal stage transitions (from CRMDealStageHistory)
- DEAL_CREATED: Deal creation events
- BLOCKER_ADDED: When a blocker is created (manual or AI-extracted)
- BLOCKER_RESOLVED: When a blocker is resolved
- BLOCKER_DISMISSED: When a blocker is dismissed
- RISK_FLAGGED: When a risk is identified (manual or AI-extracted)
- STAKEHOLDER_ADDED: When a stakeholder is added to a deal
- STAKEHOLDER_REMOVED: When a stakeholder is removed from a deal

NOTE: Real-time timeline creation happens INLINE in sync services
(DealSyncService, DealEmailMatcher, DealMeetingMatcher, RiskBlockerService,
StakeholdersService) - NOT via these tasks. These tasks are for backfilling
existing data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.timeline.deal_timeline_service import DealTimelineService
from src.service.crm.timeline.deal_timeline_sync_service import DealTimelineSyncService
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


# =============================================================================
# Tier 1: All Orgs Coordinator
# =============================================================================


@app.task(
    name="deal.trigger_timeline_sync_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def trigger_timeline_sync_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Trigger timeline sync for ALL approved organizations.

    This is a coordinator task that:
    1. Queries master DB for all approved organizations
    2. Dispatches Tier 2 tasks (sync_org_timeline_task) for each org
    3. Returns summary of dispatched tasks

    Useful for:
    - Initial backfill after deploying timeline feature
    - Periodic reconciliation runs
    - Manual sync from admin interface

    Returns:
        Dictionary with dispatch summary:
        {
            "status": "completed" | "failed",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "total_orgs": N,
                "orgs_triggered": N,
                "orgs_skipped": N
            }
        }
    """
    started_at = datetime.now(UTC).isoformat()
    logger.info(
        "Tier 1: Triggering timeline sync for all organisations",
        extra={"event": "timeline_sync_all_orgs_started"},
    )

    async def _dispatch_all_orgs() -> dict[str, Any]:
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning(
                "No approved organisations found for timeline sync",
                extra={
                    "event": "timeline_sync_all_orgs_no_organisations",
                    "status": "no_organisations",
                },
            )
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {"total_orgs": 0, "orgs_triggered": 0, "orgs_skipped": 0},
            }

        logger.info(
            "Found approved organisations for timeline sync",
            extra={
                "event": "timeline_sync_all_orgs_found",
                "organisation_count": len(org_ids),
            },
        )

        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                # Propagate coordinator lineage into each org-wide timeline reconciliation run.
                sync_org_timeline_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    headers=correlation_headers(),
                )
                orgs_triggered += 1
            except Exception as e:
                logger.error(
                    "Failed to dispatch timeline sync for organisation",
                    extra={
                        "event": "timeline_sync_org_dispatch_failed",
                        "organisation_id": str(org_id),
                        "failure_reason": str(e),
                    },
                    exc_info=True,
                )
                orgs_skipped += 1

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": len(org_ids),
                "orgs_triggered": orgs_triggered,
                "orgs_skipped": orgs_skipped,
            },
        }

    try:
        return run_async(_dispatch_all_orgs())
    except Exception as e:
        logger.error(
            "Timeline sync all-orgs dispatch failed",
            extra={
                "event": "timeline_sync_all_orgs_failed",
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
        }


# =============================================================================
# Tier 2: Per-Organisation
# =============================================================================


@app.task(
    bind=True,
    name="deal.sync_org_timeline",
    acks_late=True,
    max_retries=2,
    default_retry_delay=300,
    soft_time_limit=1800,  # 30 minutes
    time_limit=1860,
)
def sync_org_timeline_task(
    self: Task,
    organisation_id: str,
    deal_ids: list[str] | None = None,
    event_types: list[str] | None = None,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Tier 2: Sync timeline events for all deals in an organisation.

    Processes in batches to manage memory and transaction size.
    Commits after each batch for progress durability.

    Args:
        organisation_id: Organisation UUID
        deal_ids: Optional list of specific deal IDs to sync
        event_types: Optional filter - one or more of:
            ["EMAIL", "MEETING", "STAGE_CHANGE", "DEAL_CREATED", "BLOCKER_ADDED",
             "BLOCKER_RESOLVED", "BLOCKER_DISMISSED", "RISK_FLAGGED",
             "STAKEHOLDER_ADDED", "STAKEHOLDER_REMOVED"]
        batch_size: Number of deals per batch (default 50)

    Returns:
        Dictionary with sync results:
        {
            "status": "success" | "failed",
            "organisation_id": "...",
            "deals_processed": N,
            "emails_synced": N,
            "meetings_synced": N,
            "stage_changes_synced": N,
            "deal_created_synced": N,
            "total_created": N,
            "total_skipped": N,
        }
    """
    started_at = datetime.now(UTC).isoformat()
    logger.info(
        "Tier 2: Starting org-wide timeline sync",
        extra={
            "event": "timeline_sync_org_started",
            "organisation_id": organisation_id,
            "batch_size": batch_size,
            "requested_deal_count": len(deal_ids) if deal_ids else 0,
        },
    )

    org_uuid = UUID(organisation_id)

    async def _run() -> dict[str, Any]:
        parsed_deal_ids = [UUID(d) for d in deal_ids] if deal_ids else None
        parsed_types = [TimelineEventType(t) for t in event_types] if event_types else None

        # Org-level: DealTimelineSyncService manages its own sessions (fresh session pattern)
        service = DealTimelineSyncService(org_uuid)
        result = await service.sync_organisation_timeline(
            deal_ids=parsed_deal_ids,
            event_types=parsed_types,
            batch_size=batch_size,
        )
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **result,
        }

    try:
        return run_async(_run())
    except Exception as e:
        logger.error(
            "Org timeline sync failed",
            extra={
                "event": "timeline_sync_org_failed",
                "organisation_id": organisation_id,
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=300) from e
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
        }


# =============================================================================
# Tier 3: Per-Deal
# =============================================================================


@app.task(
    bind=True,
    name="deal.sync_deal_timeline",
    acks_late=True,
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=120,
    time_limit=150,
)
def sync_deal_timeline_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    event_types: list[str] | None = None,
) -> dict[str, Any]:
    """Tier 3: Sync all timeline events for a single deal.

    Creates timeline events for all associations that don't already have them.
    Uses the unique constraint to avoid duplicates (idempotent).

    This task is for:
    - Backfilling timeline for a deal created before timeline feature
    - Repairing timeline data after issues
    - Manual sync from admin interface

    NOT for real-time creation - that happens inline in sync services.

    Args:
        organisation_id: Organisation UUID
        deal_id: Deal UUID
        event_types: Optional filter - one or more of:
            ["EMAIL", "MEETING", "STAGE_CHANGE", "DEAL_CREATED", "BLOCKER_ADDED",
             "BLOCKER_RESOLVED", "BLOCKER_DISMISSED", "RISK_FLAGGED",
             "STAKEHOLDER_ADDED", "STAKEHOLDER_REMOVED"]

    Returns:
        Dictionary with sync results:
        {
            "status": "success" | "failed",
            "deal_id": "...",
            "emails_synced": N,
            "meetings_synced": N,
            "stage_changes_synced": N,
            "deal_created_synced": N,
            "total_created": N,
            "total_skipped": N,
        }
    """
    started_at = datetime.now(UTC).isoformat()
    logger.info(
        "Tier 3: Starting timeline sync for deal",
        extra={
            "event": "timeline_sync_deal_started",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
        },
    )

    org_uuid = UUID(organisation_id)
    deal_uuid = UUID(deal_id)

    async def _run() -> dict[str, Any]:
        parsed_types = [TimelineEventType(t) for t in event_types] if event_types else None

        service = DealTimelineSyncService(org_uuid)
        result = await service.sync_deal_timeline(deal_uuid, parsed_types)
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **result,
        }

    try:
        return run_async(_run())
    except Exception as e:
        logger.error(
            "Timeline sync failed for deal",
            extra={
                "event": "timeline_sync_deal_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
        }


# =============================================================================
# Status Update Tasks
# =============================================================================


@app.task(
    bind=True,
    name="deal.exclude_timeline_event",
    acks_late=True,
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=30,
    time_limit=60,
)
def exclude_timeline_event_task(
    self: Task,
    organisation_id: str,
    timeline_event_id: str,
) -> dict[str, Any]:
    """Mark a timeline event as excluded (soft-hide from timeline).

    Called when an association is rejected or cancelled.

    Args:
        organisation_id: Organisation UUID
        timeline_event_id: Timeline event UUID to exclude

    Returns:
        Dictionary with success status
    """
    logger.info(
        "Excluding timeline event",
        extra={
            "event": "timeline_event_exclusion_started",
            "organisation_id": organisation_id,
            "timeline_event_id": timeline_event_id,
        },
    )
    org_uuid = UUID(organisation_id)

    async def _run() -> dict[str, Any]:
        event_uuid = UUID(timeline_event_id)

        async for session in acquire_org_async_session(org_uuid):
            service = DealTimelineService(session, org_uuid)
            updated = await service.exclude_timeline_event(event_uuid, datetime.now(UTC))
            await session.commit()
            return {"success": updated, "timeline_event_id": timeline_event_id}

        return {"success": False, "timeline_event_id": timeline_event_id}

    try:
        return run_async(_run())
    except Exception as e:
        logger.error(
            "Failed to exclude timeline event",
            extra={
                "event": "timeline_event_exclusion_failed",
                "organisation_id": organisation_id,
                "timeline_event_id": timeline_event_id,
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30) from e
        return {"success": False, "timeline_event_id": timeline_event_id, "error": str(e)}
