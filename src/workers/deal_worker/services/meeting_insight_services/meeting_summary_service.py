"""Meeting Summary Service.

Generates post-meeting summaries using AI agents.
Follows fresh session pattern - releases DB connections before LLM calls.

Also provides pipeline functionality to find events needing summaries
and trigger batch processing via Celery task chains.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from celery import signature
from sqlalchemy import or_, select
from sqlalchemy.sql.expression import cast
from sqlalchemy.types import String
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting, MeetingStatus
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger
from src.service.crm.jobs.deal_job_event_publisher import DealJobRedisPublisher
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name
from src.service.integrations.transcripts import TranscriptService
from src.workers.deal_worker.utils.deal_job_utils import (
    get_association,
    get_deal,
    get_event,
    get_organisation_agent_context,
)
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline_async,
    get_or_create_deal_memory_job,
)

logger = get_logger(__name__)

# Current schema version for generated content payloads
SCHEMA_VERSION = "1.0"

# System user UUID for automated pipeline jobs
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _normalize_internal_domains(domains_raw: list[str] | None) -> list[str]:
    """Normalize admin-configured internal domains for prompt use.

    Summary-side attendee classification should rely only on explicitly
    configured seller domains, not inferred participant data.
    """
    normalized: set[str] = set()
    if not domains_raw:
        return []

    for domain in domains_raw:
        if not domain or not domain.strip():
            continue
        cleaned = domain.strip().lower()
        normalized.add(cleaned)
        if cleaned.startswith("www."):
            normalized.add(cleaned.removeprefix("www."))

    return sorted(normalized)


def _build_summary_attendees(
    attendees: list[CalendarAttendee],
) -> list[dict[str, str | bool | None]]:
    """Convert calendar attendees into the prompt input format."""
    return [
        {
            "email": attendee.email,
            "name": resolve_attendee_display_name(attendee.display_name, attendee.email),
            "response_status": attendee.response_status,
            "is_organiser": attendee.is_organiser,
        }
        for attendee in attendees
        if attendee.email
    ]


class NonRetryableJobError(Exception):
    """Error that should not trigger a job retry.

    Use this for permanent failures like:
    - No transcript available (meeting wasn't recorded)
    - Invalid meeting data
    - User-facing errors that won't resolve with retry
    """

    def __init__(self, message: str, error_code: str = "permanent_failure"):
        super().__init__(message)
        self.error_code = error_code


def wrap_generated_content(data: dict) -> dict:
    """Wrap generated content with standard metadata envelope.

    All generated content (briefing, summary, follow-up) should be wrapped
    with this envelope to provide versioning and timestamp metadata.

    Args:
        data: The generated content payload

    Returns:
        Wrapped payload with schema_version, generated_at, and data fields
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "data": data,
    }


@dataclass
class PipelineResult:
    """Result of triggering the summary pipeline."""

    meetings_found: int = 0
    jobs_created: int = 0
    jobs_skipped: int = 0  # Already had active job
    tasks_triggered: int = 0
    meetings_processed: int = 0  # For backwards compatibility
    deal_memory_triggered: bool = False  # Whether deal memory was added to chain
    risk_blocker_triggered: bool = False  # Whether risk blocker extraction was added to chain
    stakeholder_analysis_triggered: bool = False  # Whether stakeholder analysis was added to chain


class MeetingSummaryService:
    """Service for generating meeting summaries.

    Provides two main capabilities:
    1. Single event summary generation (generate method)
    2. Batch pipeline for processing multiple events (process_deal_pipeline)

    Follows fresh session pattern:
    - Phase 1: Load context and verify transcript (fresh session)
    - Phase 2: Call agent (agent manages its own session)
    - Phase 3: Save result (fresh session)
    """

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID | None = None,
        event_id: UUID | None = None,
        publisher: DealJobRedisPublisher | None = None,
    ):
        """Initialize the service.

        Args:
            organisation_id: Organisation UUID.
            deal_id: Deal UUID (required for generate, optional for pipeline).
            event_id: Calendar event UUID (required for generate, optional for pipeline).
            publisher: Optional Redis publisher for SSE progress updates.
        """
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.event_id = event_id
        self.publisher = publisher
        self.last_token_usage = None

    def _publish_progress(self, message: str, stage: str, progress: float) -> None:
        """Publish progress if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, stage, progress)

    # =========================================================================
    # Single Event Summary Generation
    # =========================================================================

    async def generate(self) -> dict:
        """Generate meeting summary for a single event.

        Returns:
            Dictionary containing the summary data wrapped with metadata.

        Raises:
            NonRetryableJobError: If transcript is not available or data is missing.
            RuntimeError: For retryable errors (connection issues, pending transcript).
            ValueError: If deal_id or event_id not provided.
        """
        if not self.deal_id or not self.event_id:
            raise ValueError("deal_id and event_id are required for generate()")

        from src.agents.workflows.summary import SummaryAgent, SummaryInput

        # Phase 1: Load context and verify transcript (fresh session)
        self._publish_progress("Loading meeting context...", "Querying Database", 0.1)

        org_name: str | None = None
        organisation_profile: str | None = None
        internal_domains: list[str] = []
        association_id: UUID | None = None
        online_meeting_id: str | None = None
        online_meeting_provider: str | None = None
        attendee_roster: list[dict[str, str | bool | None]] = []

        async for master_session in acquire_master_async_session():
            org_name, organisation_profile = await get_organisation_agent_context(
                master_session, self.organisation_id
            )
            org_stmt = select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == self.organisation_id,
            )
            org_result = await master_session.execute(org_stmt)
            internal_domains = _normalize_internal_domains(org_result.scalar_one_or_none())

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, self.deal_id)
            event = await get_event(session, self.organisation_id, self.event_id)

            if not deal or not event:
                raise NonRetryableJobError("Missing deal or event data", "invalid_data")

            # Store event info for error messages
            online_meeting_id = event.online_meeting_id
            online_meeting_provider = event.online_meeting_provider
            attendee_roster = _build_summary_attendees(list(event.attendees))

            association = await get_association(
                session, self.organisation_id, self.deal_id, self.event_id
            )
            if association:
                association_id = association.association_id
                # Mark as GENERATING
                association.meeting_summary_status = GenerationStatus.GENERATING
                await session.commit()
        # Session released

        self._publish_progress("Verifying transcript availability...", "Analysing", 0.15)

        # Verify transcript (TranscriptService manages its own sessions)
        transcript_service = TranscriptService(self.organisation_id)
        meeting = await transcript_service.ensure_transcript(self.event_id)
        meeting_id: UUID | None = meeting.meeting_id if meeting else None

        if not meeting:
            logger.info(
                "No Meeting record for event %s. Event has online_meeting_id=%s, provider=%s",
                self.event_id,
                online_meeting_id,
                online_meeting_provider,
                extra={
                    "event": "meeting_summary_no_transcript_record",
                    "organisation_id": str(self.organisation_id),
                    "event_id": str(self.event_id),
                    "deal_id": str(self.deal_id),
                    "online_meeting_id": str(online_meeting_id) if online_meeting_id else None,
                    "online_meeting_provider": online_meeting_provider,
                },
            )
            raise NonRetryableJobError(
                "No transcript available for this meeting. The meeting may not have been "
                "recorded or transcription was not enabled.",
                "no_transcript",
            )

        if not meeting.full_transcript:
            logger.info(
                "Meeting record exists but no transcript. Status=%s, message=%s",
                meeting.status,
                meeting.status_message,
                extra={
                    "event": "meeting_summary_transcript_missing",
                    "organisation_id": str(self.organisation_id),
                    "event_id": str(self.event_id),
                    "deal_id": str(self.deal_id),
                    "meeting_status": str(meeting.status),
                    "status_message": meeting.status_message,
                },
            )

            # if meeting.status == MeetingStatus.FAILED:
            #     error_msg = meeting.status_message or ""
            #     if "connection" in error_msg.lower() or "timeout" in error_msg.lower():
            #         self._publish_progress("Connection error - will retry", "error", 0.4)
            #         raise RuntimeError(f"Teams API connection error (retryable): {error_msg}")

            if meeting.status == MeetingStatus.NOT_AVAILABLE:
                raise NonRetryableJobError(
                    "No transcript available for this meeting. The meeting may not have been "
                    "recorded or transcription was not enabled.",
                    "no_transcript",
                )

            # PENDING after ensure_transcript() means fetch was attempted but
            # transcript is not available (no integration, meeting not recorded, etc.)
            raise NonRetryableJobError(
                "No transcript available for this meeting. The meeting may not have been "
                "recorded or transcription was not enabled.",
                "no_transcript",
            )

        self._publish_progress("Checking stakeholders...", "Getting Stakeholders", 0.2)

        logger.info(
            "Preparing summary for meeting %s (deal %s)",
            self.event_id,
            self.deal_id,
            extra={
                "event": "meeting_summary_started",
                "organisation_id": str(self.organisation_id),
                "event_id": str(self.event_id),
                "deal_id": str(self.deal_id),
                "attendee_count": len(attendee_roster),
            },
        )

        # Build input for agent
        current_dt = datetime.now(UTC)
        input_data = SummaryInput(
            event_id=str(self.event_id),
            deal_id=str(self.deal_id),
            attendees=attendee_roster,
            internal_domains=internal_domains,
            current_datetime=current_dt.isoformat(),
        )

        # Phase 2: Call agent (tools acquire sessions on-demand)
        self._publish_progress("Generating summary with AI...", "Generation", 0.3)

        # Agent doesn't hold sessions - tools acquire on-demand via context.acquire_session()
        # SECURITY: Both organisation_id and deal_id are baked into agent context (immutable)
        agent = SummaryAgent(
            organisation_id=self.organisation_id,
            deal_id=self.deal_id,
            publisher=self.publisher,
            org_name=org_name,
            organisation_profile=organisation_profile,
        )
        summary_data = await agent.generate(input_data)
        self.last_token_usage = agent.last_token_usage

        self._publish_progress("Saving results...", "Saving", 0.9)

        # Phase 3: Save result (fresh session)
        summary_dict = summary_data.model_dump()
        wrapped_result = wrap_generated_content(summary_dict)

        if association_id:
            async for session in acquire_org_async_session(self.organisation_id):
                # Short-term compatibility: deal timelines still read the association,
                # while newer meeting surfaces can read the canonical Meeting row.
                # Keep both writes in one save phase so the payloads do not drift.
                assoc = await session.get(CalendarEventDealAssociation, association_id)
                if assoc:
                    assoc.meeting_summary_data = wrapped_result
                    assoc.meeting_summary_status = GenerationStatus.COMPLETED
                if meeting_id:
                    meeting_row = await session.get(Meeting, meeting_id)
                    if meeting_row:
                        meeting_row.meeting_summary_data = wrapped_result
                        meeting_row.meeting_summary_status = GenerationStatus.COMPLETED
                        meeting_row.meeting_summary_generated_at = datetime.now(UTC)
                if assoc or meeting_id:
                    await session.commit()

        return wrapped_result

    async def generate_meeting_level_summary(self) -> dict:
        """Generate summary for a transcribed meeting that is not linked to a deal.

        Called by the Recall webhook summary job path when `DealJobs.deal_id` is
        null. It stores generated content on the Meeting row and intentionally
        avoids deal-memory/risk/stakeholder pipelines.
        """
        if not self.event_id:
            raise ValueError("event_id is required for generate_meeting_level_summary()")

        from src.agents.workflows.meeting_level_summary import (
            MeetingLevelSummaryAgent,
            MeetingLevelSummaryInput,
        )

        self._publish_progress("Loading meeting context...", "Querying Database", 0.1)

        org_name: str | None = None
        organisation_profile: str | None = None
        internal_domains: list[str] = []
        event: CalendarEvent | None = None
        meeting_id: UUID | None = None
        transcript: str | None = None
        attendee_roster: list[dict[str, str | bool | None]] = []

        async for master_session in acquire_master_async_session():
            # Master data is read first, then released before the LLM call below.
            # The prompt only needs org name/domains, not a live DB session.
            org_name, organisation_profile = await get_organisation_agent_context(
                master_session, self.organisation_id
            )
            org_stmt = select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == self.organisation_id,
            )
            org_result = await master_session.execute(org_stmt)
            internal_domains = _normalize_internal_domains(org_result.scalar_one_or_none())

        async for session in acquire_org_async_session(self.organisation_id):
            # The org filter is the immutable tenant boundary for both event and
            # transcript rows; event_id alone can come from webhook-triggered work.
            event = await get_event(session, self.organisation_id, self.event_id)
            if not event:
                raise NonRetryableJobError("Missing event data", "invalid_data")

            attendee_roster = _build_summary_attendees(list(event.attendees))
            meeting_stmt = select(Meeting).where(
                col(Meeting.organisation_id) == self.organisation_id,
                col(Meeting.calendar_event_id) == self.event_id,
                col(Meeting.is_deleted).is_(False),
            )
            meeting = (await session.execute(meeting_stmt)).scalars().first()
            if not meeting:
                raise NonRetryableJobError(
                    "No transcript available for this meeting. The meeting may not have been "
                    "recorded or transcription was not enabled.",
                    "no_transcript",
                )
            if not meeting.full_transcript:
                if meeting.status == MeetingStatus.NOT_AVAILABLE:
                    raise NonRetryableJobError(
                        "No transcript available for this meeting. The meeting may not have been "
                        "recorded or transcription was not enabled.",
                        "no_transcript",
                    )
                raise NonRetryableJobError(
                    "No transcript available for this meeting. The meeting may not have been "
                    "recorded or transcription was not enabled.",
                    "no_transcript",
                )

            meeting.meeting_summary_status = GenerationStatus.GENERATING
            meeting_id = meeting.meeting_id
            transcript = meeting.full_transcript
            await session.commit()

        # The DB sessions above are closed before the model call so LLM latency
        # cannot hold an org connection from the pool.
        self._publish_progress("Generating meeting summary with AI...", "Generation", 0.3)
        agent = MeetingLevelSummaryAgent(
            organisation_id=self.organisation_id,
            org_name=org_name,
            organisation_profile=organisation_profile,
        )
        summary_data = await agent.generate(
            MeetingLevelSummaryInput(
                event_id=str(self.event_id),
                title=event.title,
                start_at=event.start_at.isoformat(),
                end_at=event.end_at.isoformat(),
                attendees=attendee_roster,
                internal_domains=internal_domains,
                transcript=transcript,
                current_datetime=datetime.now(UTC).isoformat(),
            ),
        )
        self.last_token_usage = agent.last_token_usage
        wrapped_result = wrap_generated_content(summary_data.model_dump())

        self._publish_progress("Saving results...", "Saving", 0.9)
        async for session in acquire_org_async_session(self.organisation_id):
            # Re-open a fresh session for the write. The Meeting row is the durable
            # storage target for summaries that have no deal association.
            meeting = await session.get(Meeting, meeting_id)
            if meeting:
                meeting.meeting_summary_data = wrapped_result
                meeting.meeting_summary_status = GenerationStatus.COMPLETED
                meeting.meeting_summary_generated_at = datetime.now(UTC)
                await session.commit()

        return wrapped_result

    # =========================================================================
    # Pipeline: Batch Processing for Multiple Events
    # =========================================================================

    async def _get_events_needing_summary(self, deal_id: UUID) -> list[tuple[UUID, UUID, UUID]]:
        """Get events needing summary: ended, transcript ready, no summary.

        Returns:
            List of (event_id, association_id, deal_id) tuples, oldest first.
        """
        now = datetime.now(UTC)
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(
                    col(CalendarEvent.event_id),
                    col(CalendarEventDealAssociation.association_id),
                    col(CalendarEventDealAssociation.deal_id),
                )
                .join(
                    CalendarEventDealAssociation,
                    col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
                )
                .join(
                    Meeting,
                    col(Meeting.calendar_event_id) == col(CalendarEvent.event_id),
                )
                .where(
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.deal_id) == deal_id,
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                    col(CalendarEvent.end_at) < now,
                    col(Meeting.status) == MeetingStatus.READY,
                    # Check for BOTH SQL NULL and JSON null (JSONB 'null' value)
                    or_(
                        col(CalendarEventDealAssociation.meeting_summary_data).is_(None),
                        cast(col(CalendarEventDealAssociation.meeting_summary_data), String)
                        == "null",
                    ),
                )
                .order_by(col(CalendarEvent.start_at).asc())
            )
            result = await session.execute(stmt)
            return [(row[0], row[1], row[2]) for row in result.all()]
        return []

    async def _get_active_job(self, session, deal_id: UUID, event_id: UUID) -> DealJobs | None:
        """Check if there's already an active job for this event."""
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.event_id) == event_id,
            col(DealJobs.job_type) == DealJobType.MEETING_SUMMARY,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        result = await session.execute(stmt)
        job: DealJobs | None = result.scalars().first()
        return job

    async def _create_job(self, session, deal_id: UUID, event_id: UUID) -> DealJobs:
        """Create a DealJobs record for the meeting summary."""
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.MEETING_SUMMARY,
            deal_id=deal_id,
            event_id=event_id,
            status=JobStatus.PENDING,
            job_metadata={"source": "automated_pipeline", "event_id": str(event_id)},
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        return job

    async def _get_active_task_job(self, session, deal_id: UUID, event_id: UUID) -> DealJobs | None:
        """Return an existing task-sync job for an in-flight meeting summary."""
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.event_id) == event_id,
            col(DealJobs.job_type) == DealJobType.DEAL_TASK_SYNC,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        return (await session.execute(stmt)).scalars().first()

    async def _create_task_job(
        self,
        session,
        deal_id: UUID,
        event_id: UUID,
        association_id: UUID,
        summary_job_id: UUID,
    ) -> DealJobs:
        """Create the task-sync job paired with one meeting summary job."""
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.DEAL_TASK_SYNC,
            deal_id=deal_id,
            event_id=event_id,
            status=JobStatus.PENDING,
            job_metadata={
                "source": "meeting_summary_completed",
                "chained_from": "meeting_summary",
                "source_association_id": str(association_id),
                "source_summary_job_id": str(summary_job_id),
            },
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        return job

    async def _trigger_deal_insight_pipeline(self, deal_id: UUID, result: PipelineResult) -> None:
        """Trigger the parallel risk+stakeholder -> deal memory pipeline."""
        deal_memory_job_id: UUID | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            deal_memory_job_id, created = await get_or_create_deal_memory_job(
                session,
                self.organisation_id,
                deal_id,
                "automated_pipeline",
                "meeting_summaries_completed",
                chained_from="meeting_summaries",
            )
            if created:
                logger.debug(
                    "Created deal memory job for deal=%s job=%s",
                    deal_id,
                    deal_memory_job_id,
                )
            else:
                logger.debug(
                    "Using existing deal memory job for deal=%s job=%s",
                    deal_id,
                    deal_memory_job_id,
                )
            await session.commit()

        if deal_memory_job_id:
            try:
                await dispatch_deal_insight_pipeline_async(
                    str(self.organisation_id),
                    str(deal_id),
                    str(deal_memory_job_id),
                )
                result.deal_memory_triggered = True
                result.risk_blocker_triggered = True
                result.stakeholder_analysis_triggered = True
                result.tasks_triggered = 3

                logger.info(
                    "Triggered parallel insight pipeline for deal %s: risk_blocker + stakeholder -> deal_memory=%s",
                    deal_id,
                    deal_memory_job_id,
                    extra={
                        "event": "meeting_summary_insight_pipeline_triggered",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                        "deal_memory_job_id": str(deal_memory_job_id),
                    },
                )
            except Exception as e:
                logger.error(
                    "Failed to trigger deal insight pipeline: deal=%s error=%s",
                    deal_id,
                    str(e),
                    extra={
                        "event": "meeting_summary_insight_pipeline_failed",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                        "error": str(e),
                    },
                )

    async def _deal_needs_memory(self, deal_id: UUID) -> bool:
        """Check if deal needs memory (has meeting summaries but no deal memory).

        Returns True if:
        - Deal has at least 1 completed meeting summary (READY transcript + summary data)
        - AND deal memory is missing

        This catches cases where meeting summaries were done in previous runs
        but deal memory wasn't generated yet.
        """
        from src.domain.models.organisation_tables.sync.deal_memory_models import DealMemory

        now = datetime.now(UTC)

        async for session in acquire_org_async_session(self.organisation_id):
            # A deal should only rebuild memory after at least one completed
            # meeting summary exists; otherwise downstream insight agents have
            # no new meeting context to absorb.
            stmt = (
                select(col(CalendarEventDealAssociation.association_id))
                .join(
                    CalendarEvent,
                    col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
                )
                .join(
                    Meeting,
                    col(Meeting.calendar_event_id) == col(CalendarEvent.event_id),
                )
                .where(
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.deal_id) == deal_id,
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                    col(CalendarEvent.end_at) < now,
                    col(Meeting.status) == MeetingStatus.READY,
                    # Has summary data (not NULL and not JSON null)
                    col(CalendarEventDealAssociation.meeting_summary_data).isnot(None),
                    cast(col(CalendarEventDealAssociation.meeting_summary_data), String) != "null",
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            has_meeting_summaries = result.first() is not None

            if not has_meeting_summaries:
                return False

            memory_exists = (
                (
                    await session.execute(
                        select(DealMemory).where(
                            col(DealMemory.organisation_id) == self.organisation_id,
                            col(DealMemory.deal_id) == deal_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            return memory_exists is None

        return False

    async def process_deal_pipeline(self, deal_id: UUID) -> PipelineResult:
        """Create jobs and trigger meeting summary tasks for a deal's events.

        Pipeline execution pattern:
        1. Find events: ended, transcript READY, no summary yet (oldest first)
        2. Create DealJobs record for each (skip if active job exists)
        3. Chain meeting summary tasks so they run sequentially
        4. Add the parallel risk+stakeholder -> deal memory pipeline at the end
        5. Return immediately (fire-and-forget)

        Args:
            deal_id: Deal to process.

        Returns:
            PipelineResult with counts of created jobs and triggered tasks.
        """
        result = PipelineResult()

        # Summaries run oldest-first so the final deal-memory build sees meeting
        # context in the same chronological order the seller experienced it.
        events = await self._get_events_needing_summary(deal_id)
        result.meetings_found = len(events)

        if not events:
            # No new meeting summaries needed, but check if deal memory is missing
            # This handles cases where meeting summaries were done in previous runs
            if await self._deal_needs_memory(deal_id):
                logger.info(
                    "No pending meeting summaries for deal %s, but deal memory is missing. "
                    "Triggering deal insight pipeline directly.",
                    deal_id,
                    extra={
                        "event": "meeting_summary_no_pending_summaries_memory_missing",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                    },
                )
                await self._trigger_deal_insight_pipeline(deal_id, result)
            else:
                logger.debug("No events need summary for deal %s", deal_id)
            return result

        logger.info(
            "Processing %d events for deal %s (creating jobs, chaining tasks)",
            len(events),
            deal_id,
            extra={
                "event": "meeting_summary_pipeline_started",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal_id),
                "events_found": len(events),
            },
        )

        # Build durable jobs first, then wire Celery signatures from committed IDs.
        # This lets retries recover from broker failures without losing visibility.
        summary_task_job_ids: list[tuple[UUID, UUID]] = []
        deal_memory_job_id: UUID | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            for event_id, association_id, event_deal_id in events:
                # Reuse active jobs so an overlapping pipeline request does not
                # generate two summaries for the same deal/event pair.
                existing_job = await self._get_active_job(session, event_deal_id, event_id)
                if existing_job:
                    logger.debug(
                        "Active job already exists for deal=%s event=%s job=%s",
                        event_deal_id,
                        event_id,
                        existing_job.job_id,
                    )
                    result.jobs_skipped += 1
                    # The existing pipeline owns both summary completion and its
                    # task sync. Re-chaining an IN_PROGRESS summary would skip its
                    # worker guard and run task extraction before the summary exists.
                    continue

                # New jobs stay pending until the Celery chain reaches them.
                # The job row is visible to API/SSE clients before execution starts.
                summary_job = await self._create_job(session, event_deal_id, event_id)
                result.jobs_created += 1
                logger.debug(
                    "Created job for deal=%s event=%s job=%s",
                    event_deal_id,
                    event_id,
                    summary_job.job_id,
                )

                task_job = await self._get_active_task_job(session, event_deal_id, event_id)
                if not task_job:
                    task_job = await self._create_task_job(
                        session,
                        event_deal_id,
                        event_id,
                        association_id,
                        summary_job.job_id,
                    )
                summary_task_job_ids.append((summary_job.job_id, task_job.job_id))

            # The deal-memory job is the fan-in point after all meeting summaries
            # complete, so downstream risk/stakeholder tasks see fresh context.
            deal_memory_job_id, created = await get_or_create_deal_memory_job(
                session,
                self.organisation_id,
                deal_id,
                "automated_pipeline",
                "meeting_summaries_completed",
                chained_from="meeting_summaries",
            )
            if created:
                logger.debug(
                    "Created deal memory job for deal=%s job=%s",
                    deal_id,
                    deal_memory_job_id,
                )
            else:
                logger.debug(
                    "Using existing deal memory job for deal=%s job=%s",
                    deal_id,
                    deal_memory_job_id,
                )

            await session.commit()

        if not summary_task_job_ids:
            logger.debug("No jobs to trigger for deal %s", deal_id)
            return result

        # Build task signatures for the chain
        task_signatures = []
        for summary_job_id, task_job_id in summary_task_job_ids:
            # Each summary task must carry the current request/task lineage into the chained pipeline.
            sig = signature(
                "meeting_summary_task.process",
                args=[str(self.organisation_id), str(summary_job_id)],
                queue="deal_jobs",
                headers=correlation_headers(),
                immutable=True,  # Don't pass result to next task
            )
            task_signatures.append(sig)
            task_signatures.append(
                signature(
                    "deal_tasks.sync_from_summary",
                    kwargs={
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                        "job_id": str(task_job_id),
                    },
                    queue="deal_jobs",
                    headers=correlation_headers(),
                    immutable=True,
                )
            )

        try:
            await dispatch_deal_insight_pipeline_async(
                str(self.organisation_id),
                str(deal_id),
                str(deal_memory_job_id),
                prefix_signatures=task_signatures,
            )
            result.tasks_triggered = len(task_signatures) + 3
            result.deal_memory_triggered = True
            result.risk_blocker_triggered = True
            result.stakeholder_analysis_triggered = True
            logger.info(
                "Triggered chain of %d tasks for deal %s (meeting_summaries=%d, "
                "deal_memory=%s, risk_blocker=%s, stakeholder=%s, created=%d, skipped=%d)",
                result.tasks_triggered,
                deal_id,
                len(summary_task_job_ids),
                result.deal_memory_triggered,
                result.risk_blocker_triggered,
                result.stakeholder_analysis_triggered,
                result.jobs_created,
                result.jobs_skipped,
                extra={
                    "event": "meeting_summary_task_chain_triggered",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "tasks_triggered": result.tasks_triggered,
                    "jobs_created": result.jobs_created,
                    "jobs_skipped": result.jobs_skipped,
                    "event_count": len(summary_task_job_ids),
                    "deal_memory_job_id": str(deal_memory_job_id) if deal_memory_job_id else None,
                },
            )
        except Exception as e:
            logger.error(
                "Failed to trigger task chain: deal=%s error=%s",
                deal_id,
                str(e),
                extra={
                    "event": "meeting_summary_task_chain_failed",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "error": str(e),
                },
            )
            raise

        # For backwards compat
        result.meetings_processed = result.tasks_triggered

        return result
