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

# Deal-level job types (not tied to a specific event)
DEAL_LEVEL_JOB_TYPES = {
    DealJobType.DEAL_SUMMARY,
    DealJobType.DEAL_RISK_ANALYSIS,
    DealJobType.DEAL_NEXT_STEPS,
    DealJobType.STAKEHOLDER_ANALYSIS,
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
        # Validate job type is a deal-level job
        if job_type not in DEAL_LEVEL_JOB_TYPES:
            raise DealJobServiceError(
                f"{job_type.value} is not a deal-level job type. Use DealMeetingJobService for meeting jobs.",
                status_code=400,
            )

        # Create job with event_id=None (deal-level)
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

        # Check if already generating (idempotent)
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

        if not active_summary_job:
            return None

        return {
            "deal_summary": {
                "job_id": active_summary_job.job_id,
                "job_type": str(active_summary_job.job_type),
                "status": str(active_summary_job.status),
            }
        }

    async def get_job(self, job_id: UUID) -> DealJobs | None:
        """Get a job by ID."""
        return await self.session.get(DealJobs, job_id)
