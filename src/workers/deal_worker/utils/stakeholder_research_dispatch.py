"""Dispatch helpers for stakeholder research jobs.

Creates a DealJobs record and enqueues a Celery task for each stakeholder.
Async variant provided for different call sites.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
)
from src.infra.database.sessions import acquire_org_async_session

logger = logging.getLogger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
RESEARCH_COOLDOWN_DAYS = 30


async def dispatch_stakeholder_research_async(
    organisation_id: UUID,
    stakeholder_id: UUID,
    deal_id: UUID | None = None,
    user_id: UUID | None = None,
    force: bool = False,
) -> str | None:
    """Create a DealJobs record and dispatch research task (async variant).

    Args:
        organisation_id: Security boundary
        stakeholder_id: The stakeholder to research
        deal_id: Optional — stored on job for context
        user_id: Who triggered this (defaults to automated pipeline)
        force: Skip cooldown check (for manual user-triggered research)

    Returns:
        job_id string, or None if skipped (cooldown / not approved)
    """
    async for session in acquire_org_async_session(organisation_id):
        # Check stakeholder exists and is approved
        stakeholder = await session.get(DealStakeholder, stakeholder_id)
        if not stakeholder:
            logger.warning(
                "dispatch_stakeholder_research: stakeholder %s not found", stakeholder_id
            )
            return None

        if stakeholder.approval_status != ApprovalStatus.APPROVED:
            logger.debug(
                "dispatch_stakeholder_research: skipping non-approved stakeholder %s",
                stakeholder_id,
            )
            return None

        email = stakeholder.email

        # When force=True (manual trigger), skip cooldown — user explicitly wants fresh research
        if not force:
            # Email-based dedup: check if ANY stakeholder with this email was
            # recently researched (same person can appear on multiple deals)
            recent_research_stmt = (
                select(DealStakeholder)
                .where(
                    col(DealStakeholder.organisation_id) == organisation_id,
                    col(DealStakeholder.email) == email,
                    col(DealStakeholder.last_researched_at).isnot(None),
                )
                .order_by(col(DealStakeholder.last_researched_at).desc())
                .limit(1)
            )
            result = await session.execute(recent_research_stmt)
            already_researched = result.scalar_one_or_none()

            if already_researched and already_researched.last_researched_at:
                age = datetime.now(UTC) - already_researched.last_researched_at
                if age < timedelta(days=RESEARCH_COOLDOWN_DAYS):
                    # Copy research from the existing record to avoid re-researching
                    if already_researched.stakeholder_research:
                        from sqlalchemy import update

                        copy_stmt = (
                            update(DealStakeholder)
                            .where(
                                col(DealStakeholder.organisation_id) == organisation_id,
                                col(DealStakeholder.stakeholder_id) == stakeholder_id,
                            )
                            .values(
                                stakeholder_research=already_researched.stakeholder_research,
                                last_researched_at=already_researched.last_researched_at,
                            )
                        )
                        await session.execute(copy_stmt)
                        await session.commit()
                        logger.info(
                            "Stakeholder research copied from recent result",
                            extra={
                                "event": "stakeholder_research_reused",
                                "organisation_id": str(organisation_id),
                                "stakeholder_id": str(stakeholder_id),
                                "source_stakeholder_id": str(already_researched.stakeholder_id),
                            },
                        )
                    return None

        # Check for existing active job for this email to avoid duplicate work
        existing_job_stmt = (
            select(DealJobs)
            .join(
                DealStakeholder,
                col(DealJobs.stakeholder_id) == col(DealStakeholder.stakeholder_id),
            )
            .where(
                col(DealJobs.organisation_id) == organisation_id,
                col(DealStakeholder.email) == email,
                col(DealJobs.job_type) == DealJobType.STAKEHOLDER_RESEARCH,
                col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
            )
        )
        existing = (await session.execute(existing_job_stmt)).scalar_one_or_none()
        if existing:
            # Stale job guard: if PENDING for over 5 min, the Celery message was
            # likely lost (worker restart, etc.). Mark it FAILED and re-create.
            stale_threshold = timedelta(minutes=5)
            if (
                existing.status == JobStatus.PENDING
                and existing.created_at
                and (datetime.now(UTC) - existing.created_at) > stale_threshold
            ):
                existing.status = JobStatus.FAILED
                existing.error_message = "Stale PENDING job — re-dispatching"
                await session.commit()
                logger.info(
                    "Stakeholder research stale job marked failed",
                    extra={
                        "event": "stakeholder_research_job_recreated",
                        "organisation_id": str(organisation_id),
                        "job_id": str(existing.job_id),
                        "stakeholder_id": str(stakeholder_id),
                    },
                )
            else:
                logger.debug(
                    "dispatch_stakeholder_research: active job exists for email=%s",
                    email,
                )
                return str(existing.job_id)

        # Resolve deal_id from stakeholder if not provided
        resolved_deal_id = deal_id or stakeholder.deal_id

        # Create job
        job = DealJobs(
            organisation_id=organisation_id,
            user_id=user_id or AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.STAKEHOLDER_RESEARCH,
            deal_id=resolved_deal_id,
            stakeholder_id=stakeholder_id,
            status=JobStatus.PENDING,
            job_metadata={
                "source": "automated_pipeline",
                "trigger": "stakeholder_created",
            },
        )
        session.add(job)
        await session.commit()
        job_id_str = str(job.job_id)

    # Dispatch via client (by task name) to avoid importing the full worker
    # task module, which pulls in heavy dependencies not available in the API container
    from src.workers.deal_worker.client import send_deal_task_async

    await send_deal_task_async(
        "stakeholder_research.process_job",
        kwargs={"organisation_id": str(organisation_id), "job_id": job_id_str},
        queue="deal_jobs",
    )

    logger.info(
        "Stakeholder research dispatched",
        extra={
            "event": "stakeholder_research_dispatched",
            "organisation_id": str(organisation_id),
            "stakeholder_id": str(stakeholder_id),
            "deal_id": str(deal_id) if deal_id else None,
            "job_id": job_id_str,
            "user_id": str(user_id) if user_id else None,
        },
    )
    return job_id_str

