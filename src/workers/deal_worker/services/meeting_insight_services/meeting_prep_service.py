"""Meeting Prep (Briefing) Service.

Generates pre-meeting preparation briefings using AI agents.
Follows fresh session pattern - releases DB connections before LLM calls.

Also provides pipeline functionality to find upcoming meetings needing prep
and trigger batch processing via Celery task chains.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from celery import signature
from sqlalchemy import or_, select
from sqlalchemy.sql.expression import cast
from sqlalchemy.types import String
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.services.meeting_insight_services.meeting_summary_service import (
    AUTOMATED_PIPELINE_USER_ID,
    NonRetryableJobError,
    wrap_generated_content,
)
from src.workers.deal_worker.utils.deal_job_utils import (
    count_emails_for_briefing,
    count_prior_meetings_for_briefing,
    get_association,
    get_deal,
    get_event,
    get_organisation_name,
)

logger = get_logger(__name__)

# How far ahead to look for upcoming meetings (in days)
PREP_LOOKAHEAD_DAYS = 2


@dataclass
class PrepPipelineResult:
    """Result of triggering the prep pipeline for a deal."""

    meeting_found: bool = False
    event_id: UUID | None = None
    job_created: bool = False
    job_skipped: bool = False  # Already had active job or prep exists
    task_triggered: bool = False
    skipped_pending_summaries: bool = False  # Skipped because past summaries not done


class MeetingPrepService:
    """Service for generating meeting prep briefings.

    Follows fresh session pattern:
    - Phase 1: Load context (fresh session, release before LLM)
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
            deal_id: Deal UUID (required for generate(), optional for pipeline).
            event_id: Calendar event UUID (required for generate(), optional for pipeline).
            publisher: Optional Redis publisher for SSE progress updates.
        """
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.event_id = event_id
        self.publisher = publisher

    def _publish_progress(self, message: str, stage: str, progress: float) -> None:
        """Publish progress if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, stage, progress)

    async def generate(self) -> dict:
        """Generate meeting prep briefing.

        Returns:
            Dictionary containing the briefing data wrapped with metadata.

        Raises:
            NonRetryableJobError: If required data is missing.
            ValueError: If deal_id or event_id not set.
        """
        if not self.deal_id or not self.event_id:
            raise ValueError("deal_id and event_id are required for generate()")

        from src.agents.workflows.briefing import BriefingAgent, BriefingInput, EventDetails

        # Phase 1: Load context (fresh session, release before agent call)
        self._publish_progress("Loading context...", "Querying Database", 0.1)

        org_name: str | None = None
        deal_name: str | None = None
        company_name: str | None = None
        event_title: str | None = None
        event_description: str | None = None
        event_start_at: str | None = None
        event_end_at: str | None = None
        organiser_name: str | None = None
        organiser_email: str | None = None
        attendee_count: int | None = None
        attendees_preview: list[str] = []
        prior_meetings_count: int | None = None
        emails_exchanged_count: int | None = None
        association_id: UUID | None = None

        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id)

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, self.deal_id)
            event = await get_event(session, self.organisation_id, self.event_id)
            association = await get_association(
                session, self.organisation_id, self.deal_id, self.event_id
            )

            if not deal or not event:
                raise NonRetryableJobError("Missing deal or event data", "invalid_data")

            deal_name = deal.name
            company_name = deal.company_name
            event_title = event.title
            event_description = event.description
            event_start_at = event.start_at.isoformat() if event.start_at else None
            event_end_at = event.end_at.isoformat() if event.end_at else None
            organiser_name = event.organiser_name
            organiser_email = event.organiser_email
            attendee_count = event.attendee_count

            if event.attendees:
                attendees_preview = [
                    (a.display_name or a.email)
                    for a in event.attendees
                    if (a.display_name or a.email)
                ][:8]

            if association:
                association_id = association.association_id

            # Get interaction counts (non-fatal if fails)
            current_dt = datetime.now(UTC)
            try:
                prior_meetings_count = await count_prior_meetings_for_briefing(
                    session,
                    self.organisation_id,
                    self.deal_id,
                    before_date=current_dt,
                    exclude_event_id=self.event_id,
                )
                emails_exchanged_count = await count_emails_for_briefing(
                    session, self.organisation_id, self.deal_id, before_date=current_dt
                )
            except Exception:
                logger.debug("Unable to compute briefing interaction counts", exc_info=True)

            # Mark as GENERATING
            if association:
                association.meeting_prep_status = GenerationStatus.GENERATING
                await session.commit()
        # Session released

        self._publish_progress("Preparing AI briefing...", "Analysing", 0.25)

        logger.info("Preparing briefing for deal %s, event %s", self.deal_id, self.event_id)

        # Build input for agent
        current_dt = datetime.now(UTC)
        input_data = BriefingInput(
            deal_id=str(self.deal_id),
            event_id=str(self.event_id),
            current_datetime=current_dt.isoformat(),
            prior_meetings_count=prior_meetings_count,
            emails_exchanged_count=emails_exchanged_count,
            deal_name=deal_name,
            company_name=company_name,
            event_details=EventDetails(
                title=event_title,
                description=event_description,
                start_at=event_start_at,
                end_at=event_end_at,
                organiser_name=organiser_name,
                organiser_email=organiser_email,
                attendee_count=attendee_count,
                attendees_preview=attendees_preview,
            ),
        )

        # Phase 2: Call agent (tools acquire sessions on-demand)
        self._publish_progress("Generating briefing with AI...", "Agent in Progress", 0.4)

        # Agent doesn't hold sessions - tools acquire on-demand via context.acquire_session()
        # SECURITY: Both organisation_id and deal_id are baked into agent context (immutable)
        agent = BriefingAgent(
            organisation_id=self.organisation_id,
            deal_id=self.deal_id,
            publisher=self.publisher,
            org_name=org_name,
        )
        briefing_data = await agent.generate(input_data)

        self._publish_progress("Saving results...", "Saving Results", 0.9)

        # Phase 3: Save result (fresh session)
        briefing_dict = briefing_data.model_dump()
        wrapped_result = wrap_generated_content(briefing_dict)

        if association_id:
            async for session in acquire_org_async_session(self.organisation_id):
                from src.domain.models.organisation_tables.sync.calendar import (
                    CalendarEventDealAssociation,
                )

                assoc = await session.get(CalendarEventDealAssociation, association_id)
                if assoc:
                    assoc.meeting_prep_data = wrapped_result
                    assoc.meeting_prep_status = GenerationStatus.COMPLETED
                    await session.commit()

        return wrapped_result

    # Pipeline: Automatic Prep for Upcoming Meetings

    async def _get_next_meeting_needing_prep(self, deal_id: UUID) -> tuple[UUID, UUID] | None:
        """Get the next upcoming meeting that needs prep generation.

        Finds the single next upcoming meeting (within PREP_LOOKAHEAD_DAYS)
        that doesn't have prep data yet.

        Returns:
            Tuple of (event_id, association_id) if found, None otherwise.
        """
        now = datetime.now(UTC)
        lookahead_end = now + timedelta(days=PREP_LOOKAHEAD_DAYS)

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(
                    col(CalendarEvent.event_id),
                    col(CalendarEventDealAssociation.association_id),
                )
                .join(
                    CalendarEventDealAssociation,
                    col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
                )
                .where(
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.deal_id) == deal_id,
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                    # Upcoming: start_at > now
                    col(CalendarEvent.start_at) > now,
                    # Within lookahead window: start_at <= now + 2 days
                    col(CalendarEvent.start_at) <= lookahead_end,
                    # Prep not generated: check for BOTH SQL NULL and JSON null
                    or_(
                        col(CalendarEventDealAssociation.meeting_prep_data).is_(None),
                        cast(col(CalendarEventDealAssociation.meeting_prep_data), String) == "null",
                    ),
                )
                # Order by start_at to get the NEXT meeting (soonest first)
                .order_by(col(CalendarEvent.start_at).asc())
                # Only take the first one
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.first()
            if row:
                return (row[0], row[1])
        return None

    async def _get_active_prep_job(self, session, deal_id: UUID, event_id: UUID) -> DealJobs | None:
        """Check if there's already an active prep job for this event."""
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.event_id) == event_id,
            col(DealJobs.job_type) == DealJobType.MEETING_BRIEFING,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        result = await session.execute(stmt)
        job: DealJobs | None = result.scalars().first()
        return job

    async def _create_prep_job(self, session, deal_id: UUID, event_id: UUID) -> DealJobs:
        """Create a DealJobs record for the meeting prep."""
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.MEETING_BRIEFING,
            deal_id=deal_id,
            event_id=event_id,
            status=JobStatus.PENDING,
            job_metadata={"source": "automated_pipeline", "event_id": str(event_id)},
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        return job

    async def _has_pending_summaries(self, deal_id: UUID) -> bool:
        """Check if any past meetings with READY transcripts are missing summaries.

        Returns True if there are past meetings that:
        - Have READY transcripts (Meeting.status = READY)
        - Are missing summaries (meeting_summary_data is NULL or JSON null)

        This ensures prep is only triggered when all past meeting context is available.

        Edge cases that return False (no pending = safe to trigger prep):
        - No past meetings exist
        - Past meetings exist but none have transcripts (all NOT_AVAILABLE)
        - All past meetings with transcripts have summaries generated

        Args:
            deal_id: Deal to check.

        Returns:
            True if there are pending summaries (should skip prep),
            False if all summaries are done (safe to trigger prep).
        """
        from src.domain.models.organisation_tables.sync.meeting_intelligence import (
            Meeting,
            MeetingStatus,
        )

        now = datetime.now(UTC)

        async for session in acquire_org_async_session(self.organisation_id):
            # Count past meetings with READY transcripts that are MISSING summaries
            stmt = (
                select(CalendarEvent.event_id)
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
                    # Past meetings only
                    col(CalendarEvent.end_at) < now,
                    # Has READY transcript
                    col(Meeting.status) == MeetingStatus.READY,
                    # Summary NOT generated (SQL NULL or JSON null)
                    or_(
                        col(CalendarEventDealAssociation.meeting_summary_data).is_(None),
                        cast(col(CalendarEventDealAssociation.meeting_summary_data), String)
                        == "null",
                    ),
                )
            )
            result = await session.execute(stmt)
            pending_count = len(result.all())

            if pending_count > 0:
                logger.debug(
                    "Deal %s has %d past meeting(s) with READY transcript but no summary",
                    deal_id,
                    pending_count,
                )
                return True

        return False

    async def process_deal_pipeline(self, deal_id: UUID) -> PrepPipelineResult:
        """Check for upcoming meetings needing prep and trigger task if needed.

        Only processes the NEXT upcoming meeting (within 2 days) for the deal.
        If prep is already generated or a job is running, does nothing.

        Args:
            deal_id: Deal to check for upcoming meetings.

        Returns:
            PrepPipelineResult with status of what was done.
        """
        result = PrepPipelineResult()

        # Find the next meeting needing prep
        meeting_info = await self._get_next_meeting_needing_prep(deal_id)

        if not meeting_info:
            logger.debug("No upcoming meetings need prep for deal %s", deal_id)
            return result

        event_id, _association_id = meeting_info
        result.meeting_found = True
        result.event_id = event_id

        logger.info(
            "Found upcoming meeting needing prep: deal=%s event=%s",
            deal_id,
            event_id,
        )

        # Check if all past meeting summaries are complete
        # Prep needs context from past meetings, so wait until all summaries are done
        if await self._has_pending_summaries(deal_id):
            logger.info(
                "Skipping prep for deal %s - past meeting summaries still pending",
                deal_id,
            )
            result.skipped_pending_summaries = True
            return result

        # Check if job already exists and create if not
        job_id: UUID | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            existing_job = await self._get_active_prep_job(session, deal_id, event_id)
            if existing_job:
                logger.debug(
                    "Active prep job already exists: deal=%s event=%s job=%s",
                    deal_id,
                    event_id,
                    existing_job.job_id,
                )
                result.job_skipped = True
                return result

            # Create new job
            job = await self._create_prep_job(session, deal_id, event_id)
            job_id = job.job_id
            result.job_created = True
            await session.commit()

            logger.debug(
                "Created prep job: deal=%s event=%s job=%s",
                deal_id,
                event_id,
                job_id,
            )

        # Trigger the Celery task
        if job_id:
            try:
                sig = signature(
                    "meeting_prep_task.process",
                    args=[str(self.organisation_id), str(job_id)],
                    queue="deal_jobs",
                )
                if sig:
                    sig.apply_async()
                    result.task_triggered = True
                    logger.info(
                        "Triggered prep task: deal=%s event=%s job=%s",
                        deal_id,
                        event_id,
                        job_id,
                    )
            except Exception as e:
                logger.error(
                    "Failed to trigger prep task: deal=%s event=%s error=%s",
                    deal_id,
                    event_id,
                    str(e),
                )

        return result
