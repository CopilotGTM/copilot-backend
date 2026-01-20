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

from celery import Task, chain, signature
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
)

# System user UUID for automated pipeline jobs
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

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
            "Task 3 (org): Skipping - another task already running for org %s",
            organisation_id,
        )
        return {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    logger.info(
        "Task 3 (org): Matching emails to deals for org %s",
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        # DealEmailMatcher now manages its own sessions per batch
        # No need to acquire a session here - it uses fresh sessions internally
        matcher = DealEmailMatcher(org_uuid)
        stats = await matcher.process_organisation()

        # Get deals that got new email matches
        deals_matched_ids: list[UUID] = stats.get("deals_matched_ids", [])

        # Create jobs and trigger pipelines for deals with new email matches
        pipelines_triggered = 0
        jobs_created = 0
        jobs_skipped = 0

        for deal_uuid in deals_matched_ids:
            try:
                # Create jobs for deal summary (with deduplication)
                deal_summary_job_id: UUID | None = None
                async for session in acquire_org_async_session(org_uuid):
                    # Check for existing active deal summary job
                    stmt = select(DealJobs).where(
                        col(DealJobs.organisation_id) == org_uuid,
                        col(DealJobs.deal_id) == deal_uuid,
                        col(DealJobs.event_id).is_(None),
                        col(DealJobs.job_type) == DealJobType.DEAL_SUMMARY,
                        col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
                    )
                    result = await session.execute(stmt)
                    existing_job = result.scalars().first()

                    if existing_job:
                        deal_summary_job_id = existing_job.job_id
                        jobs_skipped += 1
                        logger.debug(
                            "Using existing deal summary job for deal %s: %s",
                            deal_uuid,
                            deal_summary_job_id,
                        )
                    else:
                        # Create new job
                        job = DealJobs(
                            organisation_id=org_uuid,
                            user_id=AUTOMATED_PIPELINE_USER_ID,
                            job_type=DealJobType.DEAL_SUMMARY,
                            deal_id=deal_uuid,
                            event_id=None,
                            status=JobStatus.PENDING,
                            job_metadata={
                                "source": "email_matching",
                                "trigger": "new_emails_matched",
                            },
                        )
                        session.add(job)
                        await session.flush()
                        await session.refresh(job)
                        deal_summary_job_id = job.job_id
                        jobs_created += 1
                        logger.debug(
                            "Created deal summary job for deal %s: %s",
                            deal_uuid,
                            deal_summary_job_id,
                        )

                    await session.commit()

                # Trigger the pipeline chain: risk_blocker → stakeholder → deal_summary (with job)
                if deal_summary_job_id:
                    risk_blocker_sig = signature(
                        "risk_blocker.extract_deal_risks_blockers_task",
                        args=[organisation_id, str(deal_uuid)],
                        queue="deal_jobs",
                        immutable=True,
                    )
                    stakeholder_sig = signature(
                        "stakeholder.analyze_deal_stakeholders_task",
                        args=[organisation_id, str(deal_uuid)],
                        queue="deal_jobs",
                        immutable=True,
                    )
                    deal_summary_sig = signature(
                        "deal_summary_task.process",
                        args=[organisation_id, str(deal_summary_job_id)],
                        queue="deal_jobs",
                        immutable=True,
                    )
                    task_chain = chain(risk_blocker_sig, stakeholder_sig, deal_summary_sig)
                    task_chain.apply_async()
                    pipelines_triggered += 1

                    logger.debug(
                        "Triggered risk blocker + stakeholder + deal summary chain for deal %s "
                        "(job=%s) after email match",
                        deal_uuid,
                        deal_summary_job_id,
                    )

            except Exception as e:
                logger.warning(
                    "Failed to trigger pipeline for deal %s: %s",
                    deal_uuid,
                    str(e),
                )

        logger.info(
            "Task 3 (org): Triggered %d deal pipelines for deals with new email matches "
            "(jobs_created=%d, jobs_skipped=%d)",
            pipelines_triggered,
            jobs_created,
            jobs_skipped,
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
            "Task 3 (org) failed for org %s: %s",
            organisation_id,
            str(exc),
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
        "Task 3 (deal): Re-matching emails for deal %s in org %s",
        deal_id,
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        async for session in acquire_org_async_session(org_uuid):
            # process_deal requires session passed in constructor
            matcher = DealEmailMatcher(org_uuid, session=session)
            stats = await matcher.process_deal(deal_uuid)

            return {
                "status": "success",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "emails_processed": stats.get("emails_processed", 0),
                "emails_matched": stats.get("emails_matched", 0),
                "associations_created": stats.get("associations_created", 0),
            }

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Task 3 (deal) failed for deal %s: %s",
            deal_id,
            str(exc),
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc

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
        logger.info("Tier 1: Starting email matching for all organizations")

        # Query all approved organizations from master DB
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            logger.warning("No approved organizations found for email matching")
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

        logger.info("Found %d approved organization(s) for email matching", len(org_ids))

        # Dispatch Tier 2 task for each organization
        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                task = match_email_to_deals_org_task.apply_async(
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
                    "Dispatched email matching for org: %s (task_id=%s)",
                    org_id,
                    task.id,
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch email matching for org %s: %s",
                    org_id,
                    str(e),
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
            "Email matching dispatch complete: triggered=%d skipped=%d total=%d",
            orgs_triggered,
            orgs_skipped,
            len(org_ids),
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
            "Email matching all-orgs failed: error=%s",
            str(e),
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
