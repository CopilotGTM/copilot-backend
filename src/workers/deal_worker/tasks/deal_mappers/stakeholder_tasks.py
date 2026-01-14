"""Celery tasks for stakeholder synchronization.

Task 1: sync_deal_stakeholders_task - Sync stakeholders from CRM participant changes
Task 1b: sync_all_deals_stakeholders_org_task - Sync stakeholders for ALL deals in an org

Architecture (Three-Task System):
---------------------------------
    CRM Sync → sync_deal_stakeholders_task (per-deal)
                        │
                        └─→ Triggers Task 2/3 for re-matching

    CRM Initial Sync → sync_all_deals_stakeholders_org_task (org-level)
                        │
                        └─→ Loops through all deals, syncs stakeholders
                        └─→ Triggers Task 2/3 (calendar/email matching) at end

For stakeholder analysis tasks, see stakeholder_analysis_tasks.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.stakeholder_sync_service import StakeholderSyncService
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
        "Task 1: Syncing stakeholders for deal %s (change_type=%s)",
        deal_id,
        change_type,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        async for session in acquire_org_async_session(org_uuid):
            service = StakeholderSyncService(session, org_uuid)
            result = await service.sync_for_deal(deal_uuid)

            rematching_triggered = False

            # Trigger re-matching if new stakeholders were created
            if trigger_rematching and result.requires_rematching:
                match_calendar_to_deals_task.delay(organisation_id, deal_id)  # type: ignore
                match_email_to_deals_task.delay(organisation_id, deal_id)  # type: ignore
                rematching_triggered = True

            logger.info(
                "Task 1 complete: deal=%s created=%d updated=%d deactivated=%d",
                deal_id,
                result.created,
                result.updated,
                result.deactivated,
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

        # Session not acquired
        return {
            "status": "failed",
            "deal_id": deal_id,
            "change_type": change_type,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_run())

    except Exception as exc:
        logger.error("Task 1 failed for deal %s: %s", deal_id, str(exc), exc_info=True)

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
        "Org-level stakeholder sync starting: org=%s",
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        async for session in acquire_org_async_session(org_uuid):
            # Call service method to sync all deals
            service = StakeholderSyncService(session, org_uuid)
            result = await service.sync_for_organisation()

            # Trigger calendar and email matching for the entire org
            deal_matching_triggered = False
            if trigger_deal_matching and result.requires_rematching:
                try:
                    from src.workers.deal_worker.tasks.deal_mappers.calendar_matching_tasks import (
                        match_calendar_to_deals_org_task,
                    )
                    from src.workers.deal_worker.tasks.deal_mappers.email_matching_tasks import (
                        match_email_to_deals_org_task,
                    )

                    match_calendar_to_deals_org_task.delay(organisation_id)  # type: ignore
                    match_email_to_deals_org_task.delay(organisation_id)  # type: ignore
                    deal_matching_triggered = True
                    logger.info(
                        "Triggered calendar/email matching for org %s",
                        organisation_id,
                    )
                except Exception as e:
                    result.errors.append(f"Failed to trigger deal matching: {str(e)}")
                    logger.warning(
                        "Failed to trigger deal matching for org %s: %s",
                        organisation_id,
                        str(e),
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

        # Session not acquired
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_run())

    except Exception as exc:
        logger.error(
            "Org-level stakeholder sync failed for org %s: %s",
            organisation_id,
            str(exc),
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
