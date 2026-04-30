"""Task 3: Email Matching Tasks.

This module provides Celery tasks for matching emails to CRM deals.

Task 3a: match_email_to_deals_task - Match emails to a single deal
Task 3b: match_email_to_deals_org_task - Match emails for an entire org
Task 3c: match_email_to_deals_all_orgs_task - Match emails for ALL orgs (Tier 1)

ARCHITECTURE (Three-Tier System):
---------------------------------
    Tier 1: match_email_to_deals_all_orgs_task (all orgs - coordinator)
                        │
                        └─→ Dispatches Tier 2 for each approved org

    Tier 2: match_email_to_deals_org_task (org-level)
                        │
                        └─→ Processes all emails for the org

    Tier 3: match_email_to_deals_task (per-deal)
                        │
                        └─→ Re-matches emails for a single deal
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
    acquire_org_async_session,
)
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
)
from src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking import (
    VisibleDealJobTracker,
)
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline,
    get_or_create_deal_memory_job,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="deal.match_email_to_deals_org",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,  # 10 minutes
    time_limit=660,  # 11 minutes
)
def match_email_to_deals_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Match emails to deals for an entire organisation.

    This task is typically triggered after email sync completes.
    It processes unmatched emails and matches them against active deals.

    Uses Redis lock to prevent concurrent execution for the same org.

    Args:
        organisation_id: UUID of the organization (string)

    Returns:
        Dictionary with matching results:
        {
            "status": "success" | "failed" | "skipped",
            "organisation_id": "...",
            "started_at": "...",
            "completed_at": "...",
            "emails_processed": N,
            "emails_matched": N,
            "associations_created": N,
            "deals_with_matches": N,
        }
    """
    started_at = datetime.now(UTC).isoformat()

    # Acquire distributed lock to prevent concurrent execution for same org
    lock_key = f"task_lock:match_email_to_deals_org:{organisation_id}"
    if not acquire_task_lock(lock_key, ttl=660):  # TTL matches time_limit
        logger.info(
            "Task 3 (org): Skipping - another task already running for organisation",
            extra={
                "event": "email_matching_org_already_running",
                "organisation_id": organisation_id,
                "failure_reason": "already_running",
            },
        )
        return {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    logger.info(
        "Task 3 (org): Matching emails to deals for organisation",
        extra={"event": "email_matching_org_started", "organisation_id": organisation_id},
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        # DealEmailMatcher now manages its own sessions per batch
        # No need to acquire a session here - it uses fresh sessions internally
        matcher = DealEmailMatcher(org_uuid)
        stats = await matcher.process_organisation()

        # Only advance stakeholders/deals that produced fresh match IDs.
        deals_matched_ids: list[UUID] = stats.get("deals_matched_ids", [])

        # Queue one pipeline fanout job per matched deal so a flaky per-deal
        # worker cannot block unrelated deals in the same scan.
        pipelines_triggered = 0
        jobs_created = 0
        jobs_skipped = 0

        for deal_uuid in deals_matched_ids:
            try:
                # Deduplicate at job creation so repeated scanners reuse the same
                # pending slot instead of flooding the queue.
                deal_memory_job_id: UUID | None = None
                async for session in acquire_org_async_session(org_uuid):
                    deal_memory_job_id, created = await get_or_create_deal_memory_job(
                        session,
                        org_uuid,
                        deal_uuid,
                        "email_matching",
                        "new_emails_matched",
                    )
                    if created:
                        jobs_created += 1
                        logger.debug(
                            "Created deal memory job for deal %s: %s",
                            deal_uuid,
                            deal_memory_job_id,
                        )
                    else:
                        jobs_skipped += 1
                        logger.debug(
                            "Using existing deal memory job for deal %s: %s",
                            deal_uuid,
                            deal_memory_job_id,
                        )

                    await session.commit()

                if deal_memory_job_id:
                    dispatch_deal_insight_pipeline(
                        organisation_id,
                        str(deal_uuid),
                        str(deal_memory_job_id),
                    )
                    pipelines_triggered += 1

                    logger.debug(
                        "Triggered parallel risk + stakeholder -> deal memory pipeline for deal %s "
                        "(job=%s) after email match",
                        deal_uuid,
                        deal_memory_job_id,
                    )

            except Exception as e:
                logger.warning(
                    "Failed to trigger pipeline for deal after email match",
                    extra={
                        "event": "email_matching_org_pipeline_trigger_failed",
                        "organisation_id": organisation_id,
                        "deal_id": str(deal_uuid),
                        "failure_reason": str(e),
                    },
                )

        logger.info(
            "Task 3 (org): Triggered email match pipelines",
            extra={
                "event": "email_matching_org_completed",
                "organisation_id": organisation_id,
                "pipelines_triggered": pipelines_triggered,
                "jobs_created": jobs_created,
                "jobs_skipped": jobs_skipped,
            },
        )

        return {
            "status": "success",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "emails_processed": stats.get("emails_processed", 0),
            "emails_matched": stats.get("emails_matched", 0),
            "associations_created": stats.get("associations_created", 0),
            "deals_with_matches": stats.get("deals_with_matches", 0),
            "pipelines_triggered": pipelines_triggered,
            "jobs_created": jobs_created,
            "jobs_skipped": jobs_skipped,
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Task 3 (org) failed for organisation",
            extra={
                "event": "email_matching_org_failed",
                "organisation_id": organisation_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    finally:
        release_task_lock(lock_key)


@app.task(
    bind=True,
    name="deal.match_email_to_deals",
    acks_late=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=120,  # 2 minutes
    time_limit=150,  # 2.5 minutes
)
def match_email_to_deals_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Match emails to a single deal.

    This task is triggered when:
    - A new deal is created (NEW_DEAL)
    - A stakeholder is added to an existing deal (PARTICIPANT_ADDED)
    - User requests a manual refresh

    It re-processes all emails against this specific deal's
    stakeholders to find historical email threads.

    Args:
        organisation_id: UUID of the organization (string)
        deal_id: UUID of the deal to re-match (string)

    Returns:
        Dictionary with matching results
    """
    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Task 3 (deal): Re-matching emails for deal",
        extra={
            "event": "email_matching_deal_started",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
        },
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        tracker = VisibleDealJobTracker(org_uuid, job_id, DealJobType.DEAL_EMAIL_REMATCH)
        await tracker.start("matching_emails", "Updating related emails", 0.25)

        async for session in acquire_org_async_session(org_uuid):
            # process_deal requires session passed in constructor
            matcher = DealEmailMatcher(
                org_uuid,
                session=session,
            )
            stats = await matcher.process_deal(deal_uuid)

            result = {
                "status": "success",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "emails_processed": stats.get("emails_processed", 0),
                "emails_matched": stats.get("emails_matched", 0),
                "associations_created": stats.get("associations_created", 0),
            }
            await tracker.complete(result, "saving_email_links", "Saving email links")
            return result

        result = {
            "status": "failed",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }
        await tracker.fail(result["error"])
        return result

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Task 3 (deal) failed for deal",
            extra={
                "event": "email_matching_deal_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc

        if job_id:
            run_async(
                VisibleDealJobTracker(
                    UUID(organisation_id), job_id, DealJobType.DEAL_EMAIL_REMATCH
                ).fail(str(exc))
            )

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


@app.task(
    name="deal.match_email_to_deals_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,  # 5 minutes (coordinator task)
    time_limit=360,
)
def match_email_to_deals_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Match emails to deals for ALL approved organizations.

    This is a coordinator task that:
    1. Queries master DB for all approved organizations
    2. Dispatches Tier 2 tasks (match_email_to_deals_org_task) for each org
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
            "Tier 1: Starting email matching for all organisations",
            extra={"event": "email_matching_all_orgs_started"},
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
                "No approved organisations found for email matching",
                extra={
                    "event": "email_matching_all_orgs_no_organisations",
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
            "Found approved organisations for email matching",
            extra={
                "event": "email_matching_all_orgs_found",
                "organisation_count": len(org_ids),
            },
        )

        # Dispatch Tier 2 task for each organization
        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                # Coordinator fan-out should preserve correlation so org-level matching is traceable.
                task = match_email_to_deals_org_task.apply_async(
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
                    "Dispatched email matching for org: %s (task_id=%s)",
                    org_id,
                    task.id,
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch email matching for organisation",
                    extra={
                        "event": "email_matching_org_dispatch_failed",
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
            "Email matching dispatch complete",
            extra={
                "event": "email_matching_all_orgs_completed",
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
            "Email matching all-orgs failed",
            extra={
                "event": "email_matching_all_orgs_failed",
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
