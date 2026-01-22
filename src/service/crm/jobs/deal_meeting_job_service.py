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
        # For meeting summary, verify transcript exists before triggering task
        if job_type == DealJobType.MEETING_SUMMARY:
            await self._verify_transcript_for_summary(event_id)

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

    async def _verify_transcript_for_summary(self, event_id: UUID) -> None:
        """Verify that a transcript exists for meeting summary generation.

        Checks if a Meeting record exists with READY status and has a transcript.
        This prevents triggering a task that will immediately fail.

        Args:
            event_id: The calendar event ID to check

        Raises:
            DealMeetingJobServiceError: If no transcript is available
        """
        # First check if the calendar event exists
        event = await self.session.get(CalendarEvent, event_id)
        if not event:
            raise DealMeetingJobServiceError("Calendar event not found.", status_code=404)

        # Check for Meeting record with transcript
        stmt = select(Meeting).where(
            col(Meeting.calendar_event_id) == event_id,
            col(Meeting.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        meeting = result.scalars().first()

        if not meeting:
            logger.info(
                "No meeting record for event %s - transcript not available",
                event_id,
            )
            raise DealMeetingJobServiceError(
                "No transcript available for this meeting. The meeting may not have been "
                "recorded or the transcript has not been fetched yet.",
                status_code=400,
            )

        if meeting.status == MeetingStatus.NOT_AVAILABLE:
            logger.info(
                "Meeting %s has status NOT_AVAILABLE - no transcript",
                event_id,
            )
            raise DealMeetingJobServiceError(
                "No transcript available for this meeting. The meeting was not recorded "
                "or transcription was not enabled.",
                status_code=400,
            )

        if meeting.status == MeetingStatus.PENDING:
            logger.info(
                "Meeting %s has status PENDING - transcript still being fetched",
                event_id,
            )
            raise DealMeetingJobServiceError(
                "Transcript is still being processed. Please try again later.",
                status_code=400,
            )

        if meeting.status == MeetingStatus.FETCHING:
            logger.info(
                "Meeting %s has status FETCHING - transcript fetch in progress",
                event_id,
            )
            raise DealMeetingJobServiceError(
                "Transcript is currently being fetched. Please try again shortly.",
                status_code=400,
            )

        if meeting.status == MeetingStatus.FAILED:
            logger.info(
                "Meeting %s has status FAILED - transcript fetch failed: %s",
                event_id,
                meeting.status_message,
            )
            raise DealMeetingJobServiceError(
                f"Transcript fetch failed: {meeting.status_message or 'Unknown error'}. "
                "The system will retry automatically.",
                status_code=400,
            )

        if meeting.status != MeetingStatus.READY or not meeting.full_transcript:
            logger.info(
                "Meeting %s status=%s has no transcript content",
                event_id,
                meeting.status,
            )
            raise DealMeetingJobServiceError(
                "Transcript is not ready yet. Please try again later.",
                status_code=400,
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
