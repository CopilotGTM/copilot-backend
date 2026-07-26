"""Create API-requested event jobs and hand registered types to Celery.

The meetings router uses this service for its generic event-job endpoint.
``JOB_TYPE_TO_TASK`` is the actual publishable-type boundary; separate
``MeetingsService`` routes own transcript upload. Deal-wide jobs use
``DealJobService`` instead.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting, MeetingStatus
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger(__name__)

# These task names are Celery registration contracts. Adding a meeting job type
# requires both a registered worker and an entry here.
JOB_TYPE_TO_TASK: dict[DealJobType, str] = {
    DealJobType.MEETING_BRIEFING: "meeting_prep_task.process",
    DealJobType.MEETING_SUMMARY: "meeting_summary_task.process",
    DealJobType.TRANSCRIPT_UPLOAD: "transcript_upload_task.process",
}


class DealMeetingJobServiceError(Exception):
    """Carry an HTTP-safe message and status from the service to the router."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealMeetingJobService:
    """Create and query event-scoped jobs in one organisation.

    Jobs for different types or events may run in parallel. ``create_meeting_job``
    reuses an active job found for the same deal, event, and type, but the
    SELECT-before-INSERT path is not atomic under concurrent requests.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        """Bind the tenant write session and the user who owns new SSE jobs."""
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
        """Persist a meeting job, publish its worker task, and commit it.

        This lower-level entry point does not deduplicate; callers that want
        active-job reuse must use ``create_meeting_job``. Queue
        publication happens before commit so a broker failure can roll back the
        new row. Worker retries cover a pre-commit read race only when the API
        commit eventually succeeds; a post-publication commit failure is not recovered.

        Args:
            job_type: Type of job to create
            deal_id: The deal this job is for
            event_id: Optional calendar event (for meeting-related jobs)
            stakeholder_id: Optional stakeholder (for stakeholder-related jobs)
            metadata: Optional job-specific metadata

        Returns:
            The created DealJobs for SSE subscription
        """
        # Reject missing event/stakeholder context before allocating a durable job ID.
        self._validate_job_request(job_type, event_id, stakeholder_id)

        # Flush assigns the job ID needed by both the Celery payload and SSE URL.
        # The row is still rollback-able until queue publication succeeds.
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

        # Only explicitly registered meeting types may cross the queue boundary.
        task_name = JOB_TYPE_TO_TASK.get(job_type)
        if not task_name:
            raise DealMeetingJobServiceError(
                f"No task configured for job type: {job_type.value}", status_code=400
            )

        # Use the async publisher so a broker round trip does not block the API loop.
        # A publish failure rolls back the uncommitted row and returns a stable 500.
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
        """Return an active equivalent job or create a new meeting job.

        Active-job lookup runs before transcript gating to reuse already-visible
        work across collaborators. It is not user-scoped or concurrency-safe,
        while SSE remains owner-scoped. A completed summary is treated as manual
        regeneration and skips the downstream deal-insight pipeline.

        Args:
            deal_id: The deal this job is for
            event_id: The calendar event for this meeting
            job_type: Type of meeting job (must be in JOB_TYPE_TO_TASK)

        Returns:
            The created or existing DealJobs

        Raises:
            DealMeetingJobServiceError: If the event is missing or transcript work is in progress.
        """
        # copilotgtm: this unlocked lookup is best-effort duplicate suppression.
        # Add a database uniqueness/lock boundary if concurrent duplicate work appears.
        active_job = await self.get_active_job(deal_id, job_type, event_id=event_id)
        if active_job:
            logger.info(
                "%s job already in progress for deal %s event %s, returning existing job",
                job_type.value,
                deal_id,
                event_id,
            )
            return active_job

        # Gate only new summaries; an existing job above remains reconnectable.
        if job_type == DealJobType.MEETING_SUMMARY:
            await self._verify_transcript_for_summary(event_id)

        # Regeneration refreshes the meeting artifact without re-running deal insights
        # that were already derived from the first completed summary.
        metadata: dict[str, object] = {"event_id": str(event_id)}
        if job_type == DealJobType.MEETING_SUMMARY and await self._has_completed_meeting_summary(
            deal_id, event_id
        ):
            metadata["trigger_type"] = "manual_regeneration"
            metadata["skip_insight_pipeline"] = True

        return await self.create_job(
            job_type=job_type,
            deal_id=deal_id,
            event_id=event_id,
            metadata=metadata,
        )

    async def _has_completed_meeting_summary(self, deal_id: UUID, event_id: UUID) -> bool:
        """Return True when the meeting already has a completed summary payload.

        Regeneration should refresh only the meeting summary itself, not kick off the
        downstream deal insight pipeline again.
        """
        # Both IDs originate at the route; organisation scope is the trust boundary
        # preventing another tenant's association from affecting regeneration behavior.
        stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
        )
        result = await self.session.execute(stmt)
        association = result.scalars().first()
        if not association:
            return False

        return (
            association.meeting_summary_status == GenerationStatus.COMPLETED
            and association.meeting_summary_data is not None
        )

    async def _verify_transcript_for_summary(self, event_id: UUID) -> None:
        """Gate summary generation — only block when a backend job is already in flight.

        All other states are allowed through because the summary worker calls
        ensure_transcript() which will (re-)attempt the fetch if needed.

        Raises:
            DealMeetingJobServiceError: If the event doesn't exist or a
                fetch/processing job is already running (FETCHING/PROCESSING).
        """
        # Event id is client-controlled through the API path, so the org filter
        # remains the trust boundary even though event UUIDs are globally unique.
        event_stmt = select(CalendarEvent).where(
            col(CalendarEvent.organisation_id) == self.organisation_id,
            col(CalendarEvent.event_id) == event_id,
        )
        event = (await self.session.execute(event_stmt)).scalars().first()
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

        # Missing/failed transcript state is allowed through because the summary worker
        # owns the retry/fetch decision. Only concurrent backend work is rejected here.
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
        """Return an active job for reconnect and duplicate suppression.

        Organisation, deal, type, and optional event jointly scope the lookup;
        a caller-provided UUID alone is never trusted as tenant authorization.
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
        """Load a tenant-local job by primary key; callers enforce user ownership."""
        return await self.session.get(DealJobs, job_id)

    async def list_jobs_for_deal(
        self,
        deal_id: UUID,
        job_type: DealJobType | None = None,
        status: JobStatus | None = None,
    ) -> list[DealJobs]:
        """List tenant/deal-scoped jobs newest first, with optional filters."""
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
        """Validate context fields required by the selected worker contract."""
        # Registered meeting workers load their source event from this ID.
        if job_type in JOB_TYPE_TO_TASK and not event_id:
            raise DealMeetingJobServiceError(
                f"{job_type.value} requires an event_id.", status_code=400
            )

        # Retained for callers using this generic lower-level entry point.
        if job_type == DealJobType.STAKEHOLDER_ENRICHMENT and not stakeholder_id:
            raise DealMeetingJobServiceError(
                f"{job_type.value} requires a stakeholder_id.", status_code=400
            )

    async def close(self):
        """Close the caller-supplied session, tolerating cleanup-only failures."""
        try:
            await self.session.close()
        except Exception:
            # Cleanup errors cannot change an already-returned API result.
            logger.debug("DealMeetingJobService close exception", exc_info=True)
