"""Normalize participant-backed deal stakeholders and schedule targeted hydration.

These Celery tasks are the worker boundary around ``StakeholderSyncService``.
The service links existing CRM deal participants to contacts, rejects internal
people, and normalizes stakeholder state; it does not search email or calendar
history. When normalization requests replay or the change type can invalidate
old matches, the per-deal task hands the deal to contact communication
hydration, which owns history loading, deal association, and
communication-based stakeholder discovery.

The all-org coordinator dispatches org-level batch claimers, and each claimer
dispatches bounded per-deal tasks. This keeps retries scoped to one deal instead
of repeating an organisation-wide scan.
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
from src.service.crm.deals.mapping_eligibility_service import (
    DealMappingEligibilityService,
)
from src.service.crm.participants.contracts import StakeholderChangeType
from src.service.crm.participants.stakeholder_sync_service import (
    StakeholderSyncService,
)
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_mapping_backfill_claim_service import (
    DealMappingBackfillClaimService,
)
from src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking import (
    VisibleDealJobTracker,
)
from src.workers.deal_worker.utils.contact_communication_hydration_dispatch import (
    dispatch_contact_communication_hydration_async,
)
from src.workers.deal_worker.utils.contact_research_dispatch import (
    dispatch_contact_research_for_contact,
)

logger = get_logger(__name__)
# copilotgtm: claim at most 100 deals per org task so dispatch remains bounded;
# add continuation scheduling only if measured backfill throughput requires it.
INITIAL_BACKFILL_CLAIM_LIMIT = 100
# These change types can invalidate communication matches even when normalization
# produces no row delta, so they must still replay the deal through hydration.
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
    """Normalize one deal's participant-backed stakeholders, then request replay.

    ``StakeholderSyncService`` owns only normalization. If the approved
    stakeholder set may have changed, this task dispatches targeted deal
    hydration; that downstream transaction loads communication history, creates
    qualifying email/meeting associations, discovers additional stakeholders,
    and reconciles stale links.

    The task is safe to retry because normalization is idempotent and hydration
    has its own uniqueness boundaries. A visible job remains open across the
    queue handoff so hydration, not this producer, emits the terminal event.

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
    # Celery acknowledges only after completion. A retry repeats normalization and
    # may republish hydration, so both sides must retain their idempotency guards.
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
        # Reject ineligible deals before normalization creates participant state or
        # dispatches any downstream history work.
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

        # Normalize only participant-backed stakeholders. Email/calendar searches
        # happen later in ContactCommunicationHydrationService.
        service = StakeholderSyncService(
            org_uuid,
        )
        result = await service.sync_for_deal(deal_uuid)

        # Research enriches normalized contacts but is not part of the stakeholder
        # transaction. A publish failure is logged and does not roll back valid links.
        research_task_ids: list[str] = []
        for contact_id in sorted(result.research_contact_ids, key=str):
            try:
                research_task_id = await dispatch_contact_research_for_contact(
                    organisation_id=org_uuid,
                    contact_id=contact_id,
                    trigger="stakeholder_sync",
                )
                if research_task_id:
                    research_task_ids.append(research_task_id)
            except Exception:
                logger.warning(
                    "Failed to dispatch stakeholder contact research",
                    exc_info=True,
                    extra={
                        "event": "stakeholder_sync_contact_research_dispatch_failed",
                        "organisation_id": organisation_id,
                        "deal_id": deal_id,
                        "contact_id": str(contact_id),
                    },
                )

        rematching_triggered = False

        # Some explicit participant changes require replay even if normalization was
        # already current: removing a participant can make an old association stale.
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
            "research_task_ids": research_task_ids,
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
    """Claim and dispatch one bounded batch of deal stakeholder normalization.

    CRM initial sync and recovery flows use this task to claim pending deal jobs.
    Each claimed deal gets its own normalization task and, when requested, its
    own targeted hydration replay. This task never scans communications or runs
    one organisation-wide matcher.

    Claim state prevents concurrent org tasks from dispatching the same unit of
    work. If publication fails, the claim is marked failed so recovery can pick
    it up rather than leaving it silently reserved.

    Args:
        organisation_id: UUID of the organization (string)
        trigger_deal_matching: Whether to trigger calendar/email matching after
                              stakeholder sync completes (default True)

    Returns:
        Dict with org-level sync results
    """
    # Retrying this coordinator is safe because the claim service, not in-memory
    # iteration state, decides which per-deal jobs remain dispatchable.
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

        # Claim before publishing so overlapping org-level workers cannot fan out
        # duplicate initial-backfill work for the same deal.
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
                # Return the durable claim to a recoverable failed state when the
                # broker handoff never happened.
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

    This coordinator:
    1. Queries master DB for all approved organizations
    2. Dispatches an org-level bounded claimer for each organization
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
    # This task owns no stakeholder writes. Re-running it may enqueue another org
    # claimer, but the downstream durable claim boundary prevents duplicate deal work.
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        """Query orgs and dispatch Tier 2 tasks."""
        logger.info(
            "Tier 1: Starting stakeholder sync for all organisations",
            extra={"event": "stakeholder_sync_all_orgs_started"},
        )

        # Read organisation identities from the master database; tenant schemas
        # are opened only by the downstream org/per-deal workers.
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

        # Fan out one lightweight claimer per organisation instead of holding a
        # master session while tenant work runs.
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
