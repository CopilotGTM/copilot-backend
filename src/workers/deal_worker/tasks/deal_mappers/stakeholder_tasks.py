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
                        └─→ Loops through all deals, syncs stakeholders
                        └─→ Triggers calendar/email matching at end

    Tier 3: sync_deal_stakeholders_task (per-deal)
                        │
                        └─→ Syncs stakeholders for a single deal
                        └─→ Triggers calendar/email matching for that deal

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
from src.infra.database.sessions import (
    acquire_master_async_session,
)
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service import (
    StakeholderSyncService,
)
from src.workers.deal_worker.tasks.deal_mappers.calendar_matching_tasks import (
    match_calendar_to_deals_task,
)
from src.workers.deal_worker.tasks.deal_mappers.email_matching_tasks import (
    match_email_to_deals_task,
)

logger = get_logger(__name__)


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
) -> dict[str, Any]:
    """Task 1: Sync stakeholders for a deal based on CRM participant changes.

    Args:
        organisation_id: UUID of the organization (string)
        deal_id: UUID of the deal (string)
        change_type: Type of change (NEW_DEAL, PARTICIPANT_ADDED, etc.)
        integration_id: UUID of the CRM integration (optional)
        integration_type: Type of CRM (HUBSPOT, SALESFORCE, etc.)
        trigger_rematching: Whether to trigger Task 2/3 (default True)

    Returns:
        Dict with sync results
    """
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

        # Deal-level: service manages its own fresh session lifecycle.
        service = StakeholderSyncService(org_uuid)
        result = await service.sync_for_deal(deal_uuid)

        rematching_triggered = False

        # Trigger re-matching if new stakeholders were created
        if trigger_rematching and result.requires_rematching:
            match_calendar_to_deals_task.delay(organisation_id, deal_id)
            match_email_to_deals_task.delay(organisation_id, deal_id)
            rematching_triggered = True

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

        return {
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

        # Org-level: StakeholderSyncService manages its own sessions (fresh session pattern)
        service = StakeholderSyncService(org_uuid)
        result = await service.sync_for_organisation()

        # Trigger calendar and email matching for the entire org
        # ALWAYS trigger when trigger_deal_matching=True, not just when new CRM stakeholders
        # This ensures discovered stakeholders (CALENDAR_DISCOVERY, EMAIL_DISCOVERY)
        # from previous runs are used to find more meetings
        deal_matching_triggered = False
        if trigger_deal_matching:
            try:
                from src.workers.deal_worker.tasks.deal_mappers.calendar_matching_tasks import (
                    match_calendar_to_deals_org_task,
                )
                from src.workers.deal_worker.tasks.deal_mappers.email_matching_tasks import (
                    match_email_to_deals_org_task,
                )

                match_calendar_to_deals_org_task.delay(organisation_id)
                match_email_to_deals_org_task.delay(organisation_id)
                deal_matching_triggered = True
                logger.info(
                    "Triggered calendar/email matching for organisation",
                    extra={
                        "event": "stakeholder_sync_org_matching_triggered",
                        "organisation_id": organisation_id,
                        "deal_matching_triggered": True,
                    },
                )
            except Exception as e:
                result.errors.append(f"Failed to trigger deal matching: {str(e)}")
                logger.warning(
                    "Failed to trigger deal matching for organisation",
                    extra={
                        "event": "stakeholder_sync_org_matching_failed",
                        "organisation_id": organisation_id,
                        "failure_reason": str(e),
                    },
                )

        # Determine overall status
        if result.failed_deals == 0:
            status = "completed"
        elif result.successful_deals == 0:
            status = "failed"
        else:
            status = "partial"

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "total_deals": result.total_deals,
            "successful_deals": result.successful_deals,
            "failed_deals": result.failed_deals,
            "stakeholders_created": result.stakeholders_created,
            "stakeholders_updated": result.stakeholders_updated,
            "stakeholders_deactivated": result.stakeholders_deactivated,
            "deal_matching_triggered": deal_matching_triggered,
            "errors": result.errors[:10] if result.errors else [],
        }

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
                task = sync_all_deals_stakeholders_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
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
