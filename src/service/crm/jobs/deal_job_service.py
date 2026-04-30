"""Service for managing deal-level jobs (deal summary, stakeholder analysis, etc.).

This service handles deal-level async operations that are NOT tied to a specific meeting/event.
For meeting-level jobs (briefing, meeting summary, followup), use DealMeetingJobService.

Both services share the same DealJobs table for job storage and tracking.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger(__name__)

# Task name for deal summary processing (dedicated task for deal-level jobs)
DEAL_SUMMARY_TASK_NAME = "deal_summary_task.process"

# Active jobs are the only jobs clients should reconnect to.
ACTIVE_JOB_STATUSES = {JobStatus.PENDING, JobStatus.IN_PROGRESS}

# Deterministic post-stakeholder-create operations surfaced as deal-level activity.
STAKEHOLDER_PROCESSING_JOB_TYPES = {
    DealJobType.DEAL_EMAIL_REMATCH,
    DealJobType.DEAL_CALENDAR_REMATCH,
    DealJobType.STAKEHOLDER_SYNC,
}

# Deal-level job types (not tied to a specific event)
DEAL_LEVEL_JOB_TYPES = {
    DealJobType.DEAL_SUMMARY,
    DealJobType.DEAL_RISK_ANALYSIS,
    DealJobType.DEAL_NEXT_STEPS,
    DealJobType.STAKEHOLDER_ANALYSIS,
}


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _job_response(job: DealJobs) -> dict:
    return {
        "job_id": job.job_id,
        "job_type": _enum_value(job.job_type),
        "status": _enum_value(job.status),
    }


class DealJobServiceError(Exception):
    """Custom exception for deal job service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealJobService:
    """
    Service for creating and managing deal-level jobs.

    Deal-level jobs are operations on the entire deal, not tied to a specific meeting.
    Examples: Deal Summary, Stakeholder Analysis, Deal Risk Analysis.

    Uses the same DealJobs table but with event_id=None.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        """Clean up resources (placeholder for future async cleanup)."""
        # Session lifecycle is managed by the caller/dependency
        pass

    async def create_job(
        self,
        job_type: DealJobType,
        deal_id: UUID,
        *,
        metadata: dict | None = None,
    ) -> DealJobs:
        """
        Create a new deal-level job and enqueue the task.

        Args:
            job_type: Type of deal-level job to create
            deal_id: The deal this job is for
            metadata: Optional job-specific metadata

        Returns:
            The created DealJobs for SSE subscription

        Raises:
            DealJobServiceError: If job type is not a deal-level job or task fails to enqueue
        """
        # Validate operation scope: only top-level deal jobs are allowed here;
        # meeting and event-level tasks must be created through the meeting job service.
        if job_type not in DEAL_LEVEL_JOB_TYPES:
            raise DealJobServiceError(
                f"{job_type.value} is not a deal-level job type. Use DealMeetingJobService for meeting jobs.",
                status_code=400,
            )

        # event_id=None marks this as a deal-scoped job, which keeps replay and
        # SSE reconnect semantics aligned with deal-level workflows.
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            job_type=job_type,
            deal_id=deal_id,
            event_id=None,  # Deal-level jobs have no event
            stakeholder_id=None,
            status=JobStatus.PENDING,
            job_metadata=metadata or {},
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)

        # Enqueue Celery task (use async client to avoid blocking event loop)
        try:
            await send_deal_task_async(
                DEAL_SUMMARY_TASK_NAME,
                args=[str(self.organisation_id), str(job.job_id)],
                queue="deal_jobs",
                routing_key="deal_jobs",
            )
            logger.info(
                "Enqueued deal-level job org=%s job=%s type=%s deal=%s",
                self.organisation_id,
                job.job_id,
                job_type.value,
                deal_id,
            )
        except Exception as e:
            logger.error("Failed to enqueue deal job: %s", e, exc_info=True)
            await self.session.rollback()
            raise DealJobServiceError("Failed to schedule job.") from e

        await self.session.commit()
        return job

    async def create_deal_summary_job(self, deal_id: UUID) -> DealJobs:
        """
        Create a deal summary job.

        Analyzes all meeting transcripts for the deal to generate a comprehensive summary.

        Idempotent - returns existing job if one is already running.

        Args:
            deal_id: The deal to generate a summary for

        Returns:
            The created or existing DealJobs
        """
        job_type = DealJobType.DEAL_SUMMARY

        # Return the active job if present so callers reconnect instead of enqueuing
        # duplicate work.
        active_job = await self.get_active_job(deal_id, job_type)
        if active_job:
            logger.info(
                "Deal summary job already in progress for deal %s, returning existing job",
                deal_id,
            )
            return active_job

        return await self.create_job(
            job_type=job_type,
            deal_id=deal_id,
            metadata={"deal_id": str(deal_id)},
        )

    async def get_active_job(
        self,
        deal_id: UUID,
        job_type: DealJobType,
    ) -> DealJobs | None:
        """
        Get the currently running deal-level job for a specific type.

        Used for idempotency and job recovery when user reconnects.

        Args:
            deal_id: The deal to check
            job_type: The type of job to look for

        Returns:
            Active job if found, None otherwise
        """
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.job_type) == job_type,
            col(DealJobs.event_id).is_(None),  # Deal-level jobs have no event
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )

        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_active_stakeholder_processing_jobs(self, deal_id: UUID) -> list[dict]:
        """Return active deterministic stakeholder post-create jobs for a deal."""
        stmt = (
            select(DealJobs)
            .where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.deal_id) == deal_id,
                col(DealJobs.job_type).in_(list(STAKEHOLDER_PROCESSING_JOB_TYPES)),
                col(DealJobs.event_id).is_(None),
                col(DealJobs.status).in_(list(ACTIVE_JOB_STATUSES)),
            )
            .order_by(col(DealJobs.created_at).asc())
        )
        result = await self.session.execute(stmt)
        jobs = result.scalars().all()

        return [
            _job_response(job)
            for job in jobs
            if job.job_type in STAKEHOLDER_PROCESSING_JOB_TYPES
            and job.status in ACTIVE_JOB_STATUSES
        ]

    async def get_active_stakeholder_research_job(
        self,
        deal_id: UUID,
        stakeholder_id: UUID,
    ) -> dict | None:
        """Return the active manual stakeholder research job for one stakeholder."""
        stmt = (
            select(DealJobs)
            .where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.user_id) == self.user_id,
                col(DealJobs.deal_id) == deal_id,
                col(DealJobs.stakeholder_id) == stakeholder_id,
                col(DealJobs.job_type) == DealJobType.STAKEHOLDER_RESEARCH,
                col(DealJobs.status).in_(list(ACTIVE_JOB_STATUSES)),
            )
            .order_by(col(DealJobs.created_at).desc())
        )
        result = await self.session.execute(stmt)
        job = result.scalars().first()
        if not job:
            return None
        return _job_response(job)

    async def get_active_deal_jobs_status(self, deal_id: UUID) -> dict | None:
        """
        Get active deal-level jobs status for API response.

        Returns a dict suitable for DealSummaryJobsStatus schema, or None if no active jobs.
        Used by deals_handler to include active job info for SSE reconnection.

        Args:
            deal_id: The deal to check for active jobs

        Returns:
            Dict with job info (job_id, job_type, status) keyed by job type, or None
        """
        active_summary_job = await self.get_active_job(deal_id, DealJobType.DEAL_SUMMARY)
        stakeholder_processing = await self.get_active_stakeholder_processing_jobs(deal_id)

        if not active_summary_job and not stakeholder_processing:
            logger.debug("No active deal summary job found for deal %s", deal_id)
            return None

        response: dict = {}

        # Ensure job_id exists before returning
        if active_summary_job and not active_summary_job.job_id:
            logger.warning(
                "Active deal summary job found but job_id is None for deal %s",
                deal_id,
            )
        elif active_summary_job:
            logger.debug(
                "Active deal summary job found: deal=%s job_id=%s status=%s",
                deal_id,
                active_summary_job.job_id,
                active_summary_job.status,
            )
            response["deal_summary"] = _job_response(active_summary_job)

        if stakeholder_processing:
            response["stakeholder_processing"] = stakeholder_processing

        return response or None

    async def get_job(self, job_id: UUID) -> DealJobs | None:
        """Get a job by ID."""
        return await self.session.get(DealJobs, job_id)
