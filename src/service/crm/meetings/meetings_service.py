"""CRM service layer for managing deal meetings."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.api.schemas.crm_models import (
    DealJobResponse,
    DealMeetingResponse,
    MeetingAttendee,
    MeetingJobsStatus,
)
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting, MeetingStatus
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger("meetings_service")


class MeetingsServiceError(Exception):
    """Custom exception for meetings service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MeetingsService:
    """Service for managing CRM deal meetings."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def list_meetings_by_deal(self, deal_id: UUID) -> list[DealMeetingResponse]:
        """Get all meetings associated with a specific deal."""
        # Fetch Meetings (No Deal Join needed as stage is available in deal context)
        stmt = (
            select(CalendarEventDealAssociation, CalendarEvent)
            .join(
                CalendarEvent,
                col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
            )
            .options(selectinload(cast(Any, CalendarEvent.attendees)))
            .where(
                col(CalendarEventDealAssociation.deal_id) == deal_id,
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            )
        )
        result = await self.session.execute(stmt)
        rows = result.all()

        # Collect event IDs to fetch active jobs
        event_ids = [row[1].event_id for row in rows]
        active_jobs_map = await self._get_active_meeting_jobs(event_ids)

        meetings = []
        for assoc, event in rows:
            attendees = [
                MeetingAttendee(
                    name=resolve_attendee_display_name(att.display_name, att.email),
                    email=att.email,
                    status=att.response_status,
                )
                for att in event.attendees
            ]

            # Get active jobs for this event
            active_jobs = active_jobs_map.get(event.event_id)

            meetings.append(
                DealMeetingResponse(
                    event_id=event.event_id,
                    subject=event.title,
                    start_at=event.start_at,
                    status=assoc.status,
                    attendees=attendees,
                    # Meeting Prep (Briefing)
                    meeting_prep_status=assoc.meeting_prep_status,
                    meeting_prep_data=assoc.meeting_prep_data,
                    # Meeting Summary
                    meeting_summary_status=assoc.meeting_summary_status,
                    meeting_summary_data=assoc.meeting_summary_data,
                    # Meeting Follow-up
                    meeting_followup_status=assoc.meeting_followup_status,
                    meeting_followup_data=assoc.meeting_followup_data,
                    # Active jobs
                    active_jobs=active_jobs,
                )
            )
        return meetings

    # Meeting job types that can be active
    MEETING_JOB_TYPES = {
        DealJobType.MEETING_BRIEFING,
        DealJobType.MEETING_SUMMARY,
        DealJobType.MEETING_FOLLOWUP,
        DealJobType.TRANSCRIPT_UPLOAD,
    }

    async def _get_active_meeting_jobs(
        self, event_ids: list[UUID]
    ) -> dict[UUID, MeetingJobsStatus]:
        """
        Fetch all active meeting jobs for a list of events.
        Returns a map of event_id -> MeetingJobsStatus.
        """
        if not event_ids:
            return {}

        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.event_id).in_(event_ids),
            col(DealJobs.job_type).in_(self.MEETING_JOB_TYPES),
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        result = await self.session.execute(stmt)
        jobs = result.scalars().all()

        # Group jobs by event_id
        jobs_by_event: dict[UUID, MeetingJobsStatus] = {}
        for job in jobs:
            if not job.event_id:
                continue

            if job.event_id not in jobs_by_event:
                jobs_by_event[job.event_id] = MeetingJobsStatus()

            job_response = DealJobResponse(
                job_id=job.job_id,
                job_type=str(job.job_type),
                status=str(job.status),
            )

            # Map to appropriate field based on job type
            if job.job_type == DealJobType.MEETING_BRIEFING:
                jobs_by_event[job.event_id].meeting_briefing = job_response
            elif job.job_type == DealJobType.MEETING_SUMMARY:
                jobs_by_event[job.event_id].meeting_summary = job_response
            elif job.job_type == DealJobType.MEETING_FOLLOWUP:
                jobs_by_event[job.event_id].meeting_followup = job_response
            elif job.job_type == DealJobType.TRANSCRIPT_UPLOAD:
                jobs_by_event[job.event_id].transcript_upload = job_response

        return jobs_by_event

    async def update_meeting_status(
        self, deal_id: UUID, event_id: UUID, status: AssociationStatus
    ) -> None:
        """Update the status of a meeting association.

        With simplified status model:
        - ACTIVE: Default, visible in timeline
        - REMOVED: User removed from timeline
        - CANCELLED: System-set when meeting is cancelled

        Note: Stakeholder discovery happens automatically during matching,
        no need for manual trigger on status change.
        """
        stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        association = result.scalars().first()

        if not association:
            raise MeetingsServiceError("Meeting association not found.", status_code=404)

        association.status = status
        await self.session.commit()

    async def _validate_transcript_upload(self, deal_id: UUID, event_id: UUID) -> None:
        """Shared validation for presign and finalize transcript upload.

        Checks event association, removed/cancelled status, existing transcript,
        and in-progress processing.
        """
        # Verify event belongs to this deal and is not removed/cancelled
        assoc_stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.deal_id) == deal_id,
        )
        assoc = (await self.session.execute(assoc_stmt)).scalars().first()
        if not assoc:
            raise MeetingsServiceError(
                "Calendar event not found or not associated with this deal.",
                status_code=404,
            )
        if assoc.status in (AssociationStatus.REMOVED, AssociationStatus.CANCELLED):
            raise MeetingsServiceError(
                "Cannot upload transcript for a removed or cancelled meeting.",
                status_code=409,
            )

        # Reject if meeting already has a transcript or is being processed
        meeting_stmt = select(Meeting).where(
            col(Meeting.calendar_event_id) == event_id,
            col(Meeting.organisation_id) == self.organisation_id,
        )
        existing_meeting = (await self.session.execute(meeting_stmt)).scalars().first()
        if existing_meeting:
            if existing_meeting.status == MeetingStatus.READY:
                raise MeetingsServiceError(
                    "A transcript already exists for this meeting.",
                    status_code=409,
                )
            if existing_meeting.status in (MeetingStatus.FETCHING, MeetingStatus.PROCESSING):
                raise MeetingsServiceError(
                    "Transcript is currently being processed. Please try again shortly.",
                    status_code=409,
                )

    async def presign_transcript_upload(
        self,
        deal_id: UUID,
        event_id: UUID,
        user_id: UUID,
        filename: str,
        mimetype: str,
        size_bytes: int,
    ) -> tuple[DealJobs, str, int]:
        """Validate meeting, create a PENDING job, and return a write SAS URL.

        Returns (job, upload_url, expires_in_seconds).
        If an upload job is already in progress, returns that job with no URL.
        """
        await self._validate_transcript_upload(deal_id, event_id)

        # Idempotent — return existing job if one is already in progress
        active_stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.event_id) == event_id,
            col(DealJobs.job_type) == DealJobType.TRANSCRIPT_UPLOAD,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        active_job = (await self.session.execute(active_stmt)).scalars().first()
        if active_job:
            return active_job, "", 0

        storage_path = f"transcripts/{deal_id}/{event_id}/{filename}"

        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=user_id,
            job_type=DealJobType.TRANSCRIPT_UPLOAD,
            deal_id=deal_id,
            event_id=event_id,
            status=JobStatus.PENDING,
            job_metadata={
                "storage_path": storage_path,
                "mimetype": mimetype,
                "filename": filename,
                "size_bytes": size_bytes,
            },
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)

        expires_minutes = 15
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        try:
            upload_url = await storage.generate_write_sas(storage_path, minutes=expires_minutes)
        finally:
            await storage.close()

        await self.session.commit()

        logger.info(
            "Transcript presign created: org=%s deal=%s event=%s job=%s",
            self.organisation_id,
            deal_id,
            event_id,
            job.job_id,
        )
        return job, upload_url, expires_minutes * 60

    async def finalize_transcript_upload(
        self, deal_id: UUID, event_id: UUID, job_id: UUID
    ) -> DealJobs:
        """Mark upload as in-progress and enqueue the extraction worker.

        Called after the client has uploaded the file to the SAS URL.
        """
        job = await self.session.get(DealJobs, job_id)
        if not job:
            raise MeetingsServiceError("Upload job not found.", status_code=404)
        if job.deal_id != deal_id or job.event_id != event_id:
            raise MeetingsServiceError("Job does not match this meeting.", status_code=400)
        if job.organisation_id != self.organisation_id:
            raise MeetingsServiceError("Upload job not found.", status_code=404)
        if job.status != JobStatus.PENDING:
            raise MeetingsServiceError("Job is not in pending state.", status_code=409)

        job.status = JobStatus.IN_PROGRESS
        await self.session.flush()

        await send_deal_task_async(
            "transcript_upload_task.process",
            args=[str(self.organisation_id), str(job.job_id)],
            queue="deal_jobs",
            routing_key="deal_jobs",
        )

        await self.session.commit()

        logger.info(
            "Transcript upload finalized: org=%s deal=%s event=%s job=%s",
            self.organisation_id,
            deal_id,
            event_id,
            job.job_id,
        )
        return job

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("MeetingsService close exception", exc_info=True)
