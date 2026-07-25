"""Celery tasks for stakeholder synchronization.

Task 1a: sync_deal_stakeholders_task - Sync stakeholders for a single deal
Task 1b: sync_all_deals_stakeholders_org_task - Sync stakeholders for ALL deals in an org
Task 1c: sync_all_deals_stakeholders_all_orgs_task - Sync stakeholders for ALL orgs (Tier 1)

Architecture (Three-Tier System):
---------------------------------
    Tier 1: sync_all_deals_stakeholders_all_orgs_task (all orgs - coordinator)
                        │
                        └─→ Dispatches Tier 2 for each approved org

    Tier 2: sync_all_deals_stakeholders_org_task (org-level)
                        │
                        └─→ Claims bounded per-deal stakeholder work

    Tier 3: sync_deal_stakeholders_task (per-deal)
                        │
                        └─→ Syncs stakeholders for a single deal
                        └─→ Replays that deal through communication hydration

For stakeholder analysis tasks, see stakeholder_analysis_tasks.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.job_models import DealJobType
from src.infra.database.sessions import (
    acquire_master_async_session,
)
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_change_detector import StakeholderChangeType
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_backfill_claim_service import (
    DealMappingBackfillClaimService,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_eligibility import (
    DealMappingEligibilityService,
)
from src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service import (
    StakeholderSyncService,
)
from src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking import (
    VisibleDealJobTracker,
)
from src.workers.deal_worker.utils.contact_communication_hydration_dispatch import (
    dispatch_contact_communication_hydration_async,
)

logger = get_logger(__name__)
INITIAL_BACKFILL_CLAIM_LIMIT = 100
ALWAYS_REMATCH_CHANGE_TYPES = {
    "INITIAL_BACKFILL",
    "COMMUNICATION_SYNC_REMAP",
    StakeholderChangeType.PARTICIPANT_ADDED.value,
    StakeholderChangeType.PARTICIPANT_REMOVED.value,
    StakeholderChangeType.PARTICIPANT_UPDATED.value,
    StakeholderChangeType.MANUAL_REFRESH.value,
}


@app.task(
    bind=True,
    name="stakeholder.sync_deal_stakeholders",
    acks_late=True,
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=300,
    time_limit=360,
)
def sync_deal_stakeholders_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    change_type: str,
    integration_id: str | None = None,
    integration_type: str | None = None,
    trigger_rematching: bool = True,
    job_id: str | None = None,
    emit_events: bool = False,
) -> dict[str, Any]:
    """Task 1: Sync stakeholders for a deal based on CRM participant changes.

    Args:
        organisation_id: UUID of the organization (string)
        deal_id: UUID of the deal (string)
        change_type: Type of change (NEW_DEAL, PARTICIPANT_ADDED, etc.)
        integration_id: UUID of the CRM integration (optional)
        integration_type: Type of CRM (HUBSPOT, SALESFORCE, etc.)
        trigger_rematching: Whether to trigger Task 2/3 (default True)
        emit_events: Publish client SSE progress only for an explicitly requested job.

    Returns:
        Dict with sync results
    """
    if emit_events and not job_id:
        raise ValueError("job_id is required when emit_events=True")

    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Task 1: Syncing stakeholders for deal",
        extra={
            "event": "stakeholder_sync_deal_started",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "change_type": change_type,
            "integration_id": integration_id,
            "integration_type": integration_type,
        },
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        tracker = VisibleDealJobTracker(
            org_uuid,
            job_id,
            DealJobType.STAKEHOLDER_SYNC,
            emit_events=emit_events,
        )
        eligibility = await DealMappingEligibilityService(org_uuid).get_deal_eligibility(deal_uuid)
        if not eligibility.is_eligible:
            response: dict[str, Any] = {
                "status": "skipped",
                "reason": eligibility.reason,
                "deal_id": deal_id,
                "change_type": change_type,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "rematching_triggered": False,
                "integration_id": integration_id,
                "integration_type": integration_type,
            }
            await tracker.complete(
                response,
                "skipping_ineligible_deal",
                "Skipping ineligible deal",
            )
            return response

        await tracker.start(
            "searching_related_stakeholders",
            "Searching related stakeholders",
            0.25,
        )

        # Deal-level: service manages its own fresh session lifecycle.
        service = StakeholderSyncService(
            org_uuid,
        )
        result = await service.sync_for_deal(deal_uuid)

        rematching_triggered = False

        should_dispatch_rematching = result.requires_rematching or (
            change_type in ALWAYS_REMATCH_CHANGE_TYPES
        )
        if trigger_rematching and should_dispatch_rematching:
            # Participant changes replay this deal through the same hydration service used by
            # live sync. This replaces separate calendar/email rematch workers and their scans.
            visibility = (
                {
                    "job_id": UUID(job_id),
                    "job_type": DealJobType.STAKEHOLDER_SYNC,
                    "emit_events": True,
                }
                if emit_events and job_id
                else {}
            )
            rematching_triggered = bool(
                await dispatch_contact_communication_hydration_async(
                    organisation_id=org_uuid,
                    integration_id=UUID(integration_id) if integration_id else None,
                    deal_ids=[deal_uuid],
                    trigger="deal_participants_changed",
                    **visibility,
                )
            )
            if rematching_triggered and emit_events:
                await tracker.progress(
                    "hydrating_communications",
                    "Matching communication history",
                    0.55,
                )

        logger.info(
            "Task 1 complete: synced stakeholders for deal",
            extra={
                "event": "stakeholder_sync_deal_completed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "change_type": change_type,
                "created_stakeholders": result.created,
                "updated_stakeholders": result.updated,
                "deactivated_stakeholders": result.deactivated,
            },
        )

        response = {
            "status": "success" if result.success else "partial",
            "deal_id": deal_id,
            "change_type": change_type,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "stakeholders_created": result.created,
            "stakeholders_updated": result.updated,
            "stakeholders_deactivated": result.deactivated,
            "stakeholders_skipped": result.skipped,
            "rematching_triggered": rematching_triggered,
            "integration_id": integration_id,
            "integration_type": integration_type,
            "errors": result.errors,
        }
        # Requested jobs stay open across the queue handoff; hydration owns their terminal
        # state. Internal recovery jobs remain silent and retain the legacy sync boundary.
        if not (emit_events and rematching_triggered):
            await tracker.complete(response, "saving_stakeholders", "Saving stakeholders")
        return response

    try:
        return run_async(_run())

    except Exception as exc:
        logger.error(
            "Task 1 failed for deal",
            extra={
                "event": "stakeholder_sync_deal_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "change_type": change_type,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        if job_id:
            run_async(
                VisibleDealJobTracker(
                    UUID(organisation_id),
                    job_id,
                    DealJobType.STAKEHOLDER_SYNC,
                    emit_events=emit_events,
                ).fail(str(exc))
            )

        return {
            "status": "failed",
            "deal_id": deal_id,
            "change_type": change_type,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


@app.task(
    bind=True,
    name="stakeholder.sync_all_deals_stakeholders_org",
    acks_late=True,
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=1800,  # 30 minutes - may have many deals
    time_limit=1860,
)
def sync_all_deals_stakeholders_org_task(
    self: Task,
    organisation_id: str,
    trigger_deal_matching: bool = True,
) -> dict[str, Any]:
    """Org-level task: Sync stakeholders for ALL deals in an organization.

    This task is triggered after CRM initial sync to ensure all deals have their
    stakeholders synced. This is required for calendar/email matching to work,
    as matching depends on the stakeholder lookup tables.

    After syncing all stakeholders, this task triggers calendar and email matching
    for the entire org.

    Args:
        organisation_id: UUID of the organization (string)
        trigger_deal_matching: Whether to trigger calendar/email matching after
                              stakeholder sync completes (default True)

    Returns:
        Dict with org-level sync results
    """
    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Org-level stakeholder sync starting",
        extra={
            "event": "stakeholder_sync_org_started",
            "organisation_id": organisation_id,
            "trigger_deal_matching": trigger_deal_matching,
        },
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        service = DealMappingBackfillClaimService(org_uuid)
        result = await service.claim_stakeholder_backfill_batch(limit=INITIAL_BACKFILL_CLAIM_LIMIT)
        deal_tasks: list[dict[str, Any]] = []
        dispatch_headers = correlation_headers()
        for deal_task in result.get("deal_tasks", []):
            try:
                task = sync_deal_stakeholders_task.apply_async(
                    kwargs={
                        "organisation_id": deal_task["organisation_id"],
                        "deal_id": deal_task["deal_id"],
                        "change_type": "INITIAL_BACKFILL",
                        "trigger_rematching": bool(trigger_deal_matching),
                        "job_id": deal_task["job_id"],
                    },
                    headers=dispatch_headers,
                )
                deal_tasks.append(
                    {
                        **deal_task,
                        "task_id": task.id,
                        "status": "triggered",
                    }
                )
            except Exception as e:
                await service.mark_dispatch_failed(
                    job_id=UUID(deal_task["job_id"]),
                    error=str(e),
                )
                logger.warning(
                    "Failed to dispatch stakeholder sync for claimed deal",
                    extra={
                        "event": "stakeholder_sync_deal_dispatch_failed",
                        "organisation_id": organisation_id,
                        "deal_id": deal_task.get("deal_id"),
                        "failure_reason": str(e),
                    },
                )
                deal_tasks.append(
                    {
                        **deal_task,
                        "status": "failed",
                        "error": str(e),
                    }
                )

        result["deal_tasks_dispatched"] = sum(
            1 for deal_task in deal_tasks if deal_task.get("status") == "triggered"
        )
        result["deal_tasks"] = deal_tasks
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result

    try:
        return run_async(_run())

    except Exception as exc:
        logger.error(
            "Org-level stakeholder sync failed",
            extra={
                "event": "stakeholder_sync_org_failed",
                "organisation_id": organisation_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=120 * (2**self.request.retries)) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


@app.task(
    name="stakeholder.sync_all_deals_stakeholders_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,  # 5 minutes (coordinator task)
    time_limit=360,
)
def sync_all_deals_stakeholders_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Sync stakeholders for ALL deals in ALL approved organizations.

    This is a coordinator task that:
    1. Queries master DB for all approved organizations
    2. Dispatches Tier 2 tasks (sync_all_deals_stakeholders_org_task) for each org
    3. Returns summary of dispatched tasks

    Useful for:
    - Initial system setup
    - Manual re-sync across all organizations
    - Testing/debugging

    Returns:
        Dictionary with dispatch summary:
        {
            "status": "completed",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "total_orgs": N,
                "orgs_triggered": N,
                "orgs_skipped": N
            },
            "org_tasks": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        """Query orgs and dispatch Tier 2 tasks."""
        logger.info(
            "Tier 1: Starting stakeholder sync for all organisations",
            extra={"event": "stakeholder_sync_all_orgs_started"},
        )

        # Query all approved organizations from master DB
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning(
                "No approved organisations found for stakeholder sync",
                extra={
                    "event": "stakeholder_sync_all_orgs_no_organisations",
                    "status": "no_organisations",
                },
            )
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_orgs": 0,
                    "orgs_triggered": 0,
                    "orgs_skipped": 0,
                },
                "org_tasks": [],
            }

        logger.info(
            "Found approved organisations for stakeholder sync",
            extra={
                "event": "stakeholder_sync_all_orgs_found",
                "organisation_count": len(org_ids),
            },
        )

        # Dispatch Tier 2 task for each organization
        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                # Preserve coordinator context into each org-level stakeholder sync.
                task = sync_all_deals_stakeholders_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    headers=correlation_headers(),
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "task_id": task.id,
                        "status": "triggered",
                    }
                )
                orgs_triggered += 1
                logger.debug(
                    "Dispatched stakeholder sync for org: %s (task_id=%s)",
                    org_id,
                    task.id,
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch stakeholder sync for organisation",
                    extra={
                        "event": "stakeholder_sync_org_dispatch_failed",
                        "organisation_id": str(org_id),
                        "failure_reason": str(e),
                    },
                    exc_info=True,
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "status": "failed",
                        "error": str(e),
                    }
                )
                orgs_skipped += 1

        completed_at = datetime.now(UTC).isoformat()

        logger.info(
            "Stakeholder sync dispatch complete",
            extra={
                "event": "stakeholder_sync_all_orgs_completed",
                "triggered_orgs": orgs_triggered,
                "skipped_orgs": orgs_skipped,
                "total_orgs": len(org_ids),
            },
        )

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "total_orgs": len(org_ids),
                "orgs_triggered": orgs_triggered,
                "orgs_skipped": orgs_skipped,
            },
            "org_tasks": org_tasks,
        }

    try:
        return run_async(_dispatch_all_orgs())
    except Exception as e:
        logger.error(
            "Stakeholder all-orgs sync failed",
            extra={
                "event": "stakeholder_sync_all_orgs_failed",
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
