"""Service for managing deal jobs (briefing, summary, stakeholder analysis, etc.)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting, MeetingStatus
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger(__name__)

# Task names for different job types (must match task registrations)
JOB_TYPE_TO_TASK: dict[DealJobType, str] = {
    DealJobType.MEETING_BRIEFING: "meeting_prep_task.process",
    DealJobType.MEETING_SUMMARY: "meeting_summary_task.process",
    DealJobType.TRANSCRIPT_UPLOAD: "transcript_upload_task.process",
}


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

        # Get task name for this job type
        task_name = JOB_TYPE_TO_TASK.get(job_type)
        if not task_name:
            raise DealMeetingJobServiceError(
                f"No task configured for job type: {job_type.value}", status_code=400
            )

        # Enqueue Celery task (use async client to avoid blocking event loop)
        try:
            await send_deal_task_async(
                task_name,
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
        Create a meeting job (briefing or summary).

        Idempotent - returns existing job if one is already running.

        Args:
            deal_id: The deal this job is for
            event_id: The calendar event for this meeting
            job_type: Type of meeting job (must be in JOB_TYPE_TO_TASK)

        Returns:
            The created or existing DealJobs

        Raises:
            DealMeetingJobServiceError: If meeting summary requested but no transcript exists
        """
        # Return existing job if one is already running (idempotent) — check
        # this first so the user gets a job ID even if the meeting is FETCHING.
        active_job = await self.get_active_job(deal_id, job_type, event_id=event_id)
        if active_job:
            logger.info(
                "%s job already in progress for deal %s event %s, returning existing job",
                job_type.value,
                deal_id,
                event_id,
            )
            return active_job

        # Only gate new job creation — block if transcript fetch is in flight
        if job_type == DealJobType.MEETING_SUMMARY:
            await self._verify_transcript_for_summary(event_id)

        return await self.create_job(
            job_type=job_type,
            deal_id=deal_id,
            event_id=event_id,
            metadata={"event_id": str(event_id)},
        )

    async def _verify_transcript_for_summary(self, event_id: UUID) -> None:
        """Gate summary generation — only block when a backend job is already in flight.

        All other states are allowed through because the summary worker calls
        ensure_transcript() which will (re-)attempt the fetch if needed.

        Raises:
            DealMeetingJobServiceError: If the event doesn't exist or a
                fetch/processing job is already running (FETCHING/PROCESSING).
        """
        event = await self.session.get(CalendarEvent, event_id)
        if not event:
            raise DealMeetingJobServiceError("Calendar event not found.", status_code=404)

        stmt = select(Meeting).where(
            col(Meeting.calendar_event_id) == event_id,
            col(Meeting.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        meeting = result.scalars().first()

        if not meeting:
            return

        # Block only when a backend job is actively working on this meeting
        in_progress = (MeetingStatus.FETCHING, MeetingStatus.PROCESSING)
        if meeting.status in in_progress:
            logger.info(
                "Meeting %s has status %s — job already in progress",
                event_id,
                meeting.status,
            )
            raise DealMeetingJobServiceError(
                "Transcript is currently being fetched. Please try again shortly.",
                status_code=409,
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
        # Meeting jobs (in JOB_TYPE_TO_TASK) require event_id
        if job_type in JOB_TYPE_TO_TASK and not event_id:
            raise DealMeetingJobServiceError(
                f"{job_type.value} requires an event_id.", status_code=400
            )

        # Stakeholder-related jobs may require stakeholder_id
        if job_type == DealJobType.STAKEHOLDER_ENRICHMENT and not stakeholder_id:
            raise DealMeetingJobServiceError(
                f"{job_type.value} requires a stakeholder_id.", status_code=400
            )

    async def close(self):
        """Dispose the underlying session."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("DealMeetingJobService close exception", exc_info=True)
