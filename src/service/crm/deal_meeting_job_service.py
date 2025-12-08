"""Service for managing deal jobs (briefing, summary, stakeholder analysis, etc.)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger(__name__)

DEAL_JOB_TASK_NAME = "deal_meeting_task.process"


class DealMeetingJobServiceError(Exception):
    """Custom exception for deal meeting job service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealMeetingJobService:
    """
    Generic service for creating and managing deal jobs.

    Supports multiple job types that can run in parallel.
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
        event_id: UUID | None = None,
        stakeholder_id: UUID | None = None,
        metadata: dict | None = None,
    ) -> DealJobs:
        """
        Create a new deal job and enqueue the task.

        Args:
            job_type: Type of job to create
            deal_id: The deal this job is for
            event_id: Optional calendar event (for meeting-related jobs)
            stakeholder_id: Optional stakeholder (for stakeholder-related jobs)
            metadata: Optional job-specific metadata

        Returns:
            The created DealJobs for SSE subscription
        """
        # Validate based on job type
        self._validate_job_request(job_type, event_id, stakeholder_id)

        # Create job
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            job_type=job_type,
            deal_id=deal_id,
            event_id=event_id,
            stakeholder_id=stakeholder_id,
            status=JobStatus.PENDING,
            job_metadata=metadata or {},
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)

        # Enqueue Celery task (use async client to avoid blocking event loop)
        try:
            await send_deal_task_async(
                DEAL_JOB_TASK_NAME,
                args=[str(self.organisation_id), str(job.job_id)],
                queue="deal_jobs",
                routing_key="deal_jobs",
            )
            logger.info(
                "Enqueued deal job org=%s job=%s type=%s deal=%s",
                self.organisation_id,
                job.job_id,
                job_type.value,
                deal_id,
            )
        except Exception as e:
            logger.error("Failed to enqueue deal job: %s", e, exc_info=True)
            await self.session.rollback()
            raise DealMeetingJobServiceError("Failed to schedule job.") from e

        await self.session.commit()
        return job

    async def create_meeting_job(
        self, deal_id: UUID, event_id: UUID, job_type: DealJobType
    ) -> DealJobs:
        """
        Create a meeting job (briefing, summary, or followup).

        Idempotent - returns existing job if one is already running.

        Args:
            deal_id: The deal this job is for
            event_id: The calendar event for this meeting
            job_type: Type of meeting job (MEETING_BRIEFING, MEETING_SUMMARY, MEETING_FOLLOWUP)

        Returns:
            The created or existing DealJobs
        """
        # Validate job type is a meeting job
        meeting_jobs = {
            DealJobType.MEETING_BRIEFING,
            DealJobType.MEETING_SUMMARY,
            DealJobType.MEETING_FOLLOWUP,
        }
        if job_type not in meeting_jobs:
            raise DealMeetingJobServiceError(
                f"{job_type.value} is not a meeting job type.", status_code=400
            )

        # Check if already generating (idempotent)
        active_job = await self.get_active_job(deal_id, job_type, event_id=event_id)
        if active_job:
            logger.info(
                "%s job already in progress for deal %s event %s, returning existing job",
                job_type.value,
                deal_id,
                event_id,
            )
            return active_job

        return await self.create_job(
            job_type=job_type,
            deal_id=deal_id,
            event_id=event_id,
            metadata={"event_id": str(event_id)},
        )

    async def get_active_job(
        self,
        deal_id: UUID,
        job_type: DealJobType,
        event_id: UUID | None = None,
    ) -> DealJobs | None:
        """
        Get the currently running job for a specific type and context.

        Used for job recovery when user reconnects.
        """
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.job_type) == job_type,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        if event_id:
            stmt = stmt.where(col(DealJobs.event_id) == event_id)

        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_job(self, job_id: UUID) -> DealJobs | None:
        """Get a job by ID."""
        return await self.session.get(DealJobs, job_id)

    async def list_jobs_for_deal(
        self,
        deal_id: UUID,
        job_type: DealJobType | None = None,
        status: JobStatus | None = None,
    ) -> list[DealJobs]:
        """List all jobs for a deal, optionally filtered by type and status."""
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
        )
        if job_type:
            stmt = stmt.where(col(DealJobs.job_type) == job_type)
        if status:
            stmt = stmt.where(col(DealJobs.status) == status)
        stmt = stmt.order_by(col(DealJobs.created_at).desc())

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    def _validate_job_request(
        self,
        job_type: DealJobType,
        event_id: UUID | None,
        stakeholder_id: UUID | None,
    ) -> None:
        """Validate job request based on job type requirements."""
        # Meeting-related jobs require event_id
        meeting_jobs = {
            DealJobType.MEETING_BRIEFING,
            DealJobType.MEETING_SUMMARY,
            DealJobType.MEETING_FOLLOWUP,
        }
        if job_type in meeting_jobs and not event_id:
            raise DealMeetingJobServiceError(
                f"{job_type.value} requires an event_id.", status_code=400
            )

        # Stakeholder-related jobs may require stakeholder_id
        stakeholder_jobs = {
            DealJobType.STAKEHOLDER_ENRICHMENT,
        }
        if job_type in stakeholder_jobs and not stakeholder_id:
            raise DealMeetingJobServiceError(
                f"{job_type.value} requires a stakeholder_id.", status_code=400
            )

    async def close(self):
        """Dispose the underlying session."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("DealMeetingJobService close exception", exc_info=True)
