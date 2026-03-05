"""Transcript Fetch Task Service.

This service handles the business logic for fetching transcripts for deal-associated meetings.
It is used by the scheduled Celery tasks in transcript_fetch_tasks.py.

Architecture:
    - Deal-centric processing: for each deal, fetch transcripts → trigger pipeline
    - Sequential processing: process one meeting at a time (no parallel)
    - Track retry attempts and status transitions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.transcripts import TranscriptService

logger = get_logger(__name__)

# Configuration
TRANSCRIPT_LOOKBACK_DAYS = 90  # Always look back 90 days for all meetings
TRANSCRIPT_MAX_FETCH_ATTEMPTS = 2  # Retry twice (~1 hour window at 30 min intervals)


@dataclass
class DealTranscriptFetchResult:
    """Result of transcript fetch for a single deal."""

    deal_id: UUID
    meetings_found: int = 0
    transcripts_fetched: int = 0
    transcripts_pending: int = 0
    transcripts_not_available: int = 0
    transcripts_failed: int = 0
    pipeline_triggered: bool = False
    pipeline_meetings_processed: int = 0
    pipeline_deal_memory_triggered: bool = False  # Deal memory added to pipeline
    pipeline_risk_blocker_triggered: bool = False  # Risk blocker extraction added to chain
    pipeline_stakeholder_triggered: bool = False  # Stakeholder analysis added to chain
    # Meeting prep (briefing) for upcoming meetings
    prep_triggered: bool = False
    prep_event_id: UUID | None = None
    prep_skipped_pending_summaries: bool = False  # Skipped due to pending summaries


@dataclass
class TranscriptFetchResult:
    """Result of transcript fetch for the organisation."""

    deals_processed: int = 0
    meetings_found: int = 0  # Meetings needing fetch
    transcripts_fetched: int = 0  # Successfully fetched
    transcripts_pending: int = 0  # Will retry next run
    transcripts_not_available: int = 0  # Permanently failed
    transcripts_failed: int = 0  # Error during fetch
    pipelines_triggered: int = 0
    deal_memories_triggered: int = 0  # Deal memory tasks added to pipelines
    risk_blockers_triggered: int = 0  # Risk blocker extraction tasks added to chains
    stakeholder_analyses_triggered: int = 0  # Stakeholder analysis tasks added to chains
    fetching_reset: int = 0  # Meetings reset from FETCHING to PENDING
    # Meeting prep stats
    prep_jobs_created: int = 0
    prep_jobs_skipped: int = 0
    prep_waiting_for_summaries: int = 0  # Skipped due to pending summaries
    deal_results: list[DealTranscriptFetchResult] = field(default_factory=list)


class TranscriptFetchTaskService:
    """Service for fetching transcripts for deal-associated meetings.

    Uses fresh session pattern for better connection management:
    - Acquires fresh session per meeting operation
    - Sessions are released between meetings, not held during external API calls

    Deal-centric approach:
    1. Get all deals needing transcript fetch
    2. For each deal: fetch transcripts → trigger pipeline → move to next deal
    3. Process meetings sequentially (one at a time)
    """

    def __init__(self, organisation_id: UUID):
        """Initialize the service.

        Args:
            organisation_id: UUID of the organisation
        """
        self.organisation_id = organisation_id

    async def _get_meetings_needing_fetch_internal(
        self, session: AsyncSession
    ) -> list[CalendarEvent]:
        """Get all meetings that need transcript fetch for the organisation.

        Args:
            session: Database session to use for query.

        Single query that returns meetings where:
        - Associated with a deal (active association)
        - Meeting has ended (end_at < now)
        - Has online meeting info (Teams/Zoom/Meet)
        - Either:
          a) No Meeting record exists yet (first attempt)
          b) PENDING/FAILED status with attempts < MAX

        Excludes:
        - READY status (already have transcript)
        - NOT_AVAILABLE with attempts >= MAX (permanent failure)
        """
        now = datetime.now(UTC)

        attendees_rel = cast(Any, CalendarEvent.attendees)
        stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_rel))
            .join(
                CalendarEventDealAssociation,
                col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
            )
            .outerjoin(
                Meeting,
                col(Meeting.calendar_event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.end_at) < now,  # Meeting has ended
                col(CalendarEvent.online_meeting_id).isnot(None),  # Has online meeting
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                or_(
                    # No Meeting record yet → first attempt
                    col(Meeting.meeting_id).is_(None),
                    # PENDING with attempts < MAX → retry
                    and_(
                        col(Meeting.status) == MeetingStatus.PENDING,
                        col(Meeting.transcript_fetch_attempts) < TRANSCRIPT_MAX_FETCH_ATTEMPTS,
                    ),
                    # FAILED with attempts < MAX → retry
                    and_(
                        col(Meeting.status) == MeetingStatus.FAILED,
                        col(Meeting.transcript_fetch_attempts) < TRANSCRIPT_MAX_FETCH_ATTEMPTS,
                    ),
                ),
            )
            .distinct()  # In case a meeting is associated with multiple deals
        )

        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _process_single_meeting(self, event: CalendarEvent) -> dict[str, Any]:
        """Process a single meeting: fetch transcript with retry tracking.

        Delegates entirely to TranscriptService which handles:
        - Finding integrations (organizer first, then attendees)
        - Trying each integration
        - Tracking fetch attempts
        - Updating meeting status

        Args:
            event: CalendarEvent to process (with attendees already loaded).

        Returns:
            Dict with result: {"status": "fetched"|"pending"|"not_available"|"failed", ...}
        """
        logger.info(
            "  → Fetching transcript for event %s (title: %s, provider: %s)",
            event.event_id,
            event.title[:50] if event.title else "No title",
            event.online_meeting_provider,
        )

        # Create TranscriptService without session - it acquires its own
        transcript_service = TranscriptService(self.organisation_id)

        # Delegate to TranscriptService - it handles everything
        fetch_status = await transcript_service.fetch_transcript_for_event(
            event=event,
            max_attempts=TRANSCRIPT_MAX_FETCH_ATTEMPTS,
        )

        logger.info(
            "  ← Event %s result: status=%s, integrations_tried=%d, fetched_with=%s, error=%s",
            event.event_id,
            fetch_status.status,
            fetch_status.integrations_tried,
            fetch_status.fetched_with,
            fetch_status.error[:100] if fetch_status.error else None,
        )

        return {
            "event_id": str(event.event_id),
            "status": fetch_status.status,
            "integrations_tried": fetch_status.integrations_tried,
            "fetched_with": fetch_status.fetched_with,
            "error": fetch_status.error,
        }

    async def _process_meetings_sequential(
        self,
        events: list[CalendarEvent],
    ) -> list[dict[str, Any]]:
        """Process meetings one at a time sequentially.

        Each meeting handles its own session management internally.
        No sessions are held during external API calls (transcript fetching).

        Args:
            events: List of CalendarEvent objects (with attendees already loaded).

        Returns:
            List of result dictionaries from each meeting processing.
        """
        results: list[dict[str, Any]] = []
        total = len(events)

        for idx, event in enumerate(events, 1):
            logger.debug("  Processing meeting %d/%d: %s", idx, total, event.event_id)
            try:
                result = await self._process_single_meeting(event)
                results.append(result)
            except Exception as e:
                logger.error(
                    "  Error processing event %s: %s",
                    event.event_id,
                    str(e),
                    exc_info=True,
                )
                results.append(
                    {
                        "event_id": str(event.event_id),
                        "status": "failed",
                        "error": str(e),
                    }
                )

        logger.info("  Processed %d/%d meetings", len(results), total)
        return results

    async def _get_open_deals(self, session: AsyncSession) -> list[UUID]:
        """Get all open deals that have calendar events associated.

        Returns:
            List of deal_ids that are open and have calendar event associations.
        """
        from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus

        stmt = (
            select(col(CRMDeal.deal_id))
            .join(
                CalendarEventDealAssociation,
                col(CalendarEventDealAssociation.deal_id) == col(CRMDeal.deal_id),
            )
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
            )
            .distinct()
        )

        result = await session.execute(stmt)
        return [row[0] for row in result.all()]

    async def _get_meetings_for_deal(
        self, session: AsyncSession, deal_id: UUID
    ) -> list[CalendarEvent]:
        """Get meetings for a specific deal that need transcript fetch.

        Args:
            session: Database session to use.
            deal_id: Deal to get meetings for.

        Returns:
            List of CalendarEvents needing transcript fetch for this deal.
        """
        now = datetime.now(UTC)

        attendees_rel = cast(Any, CalendarEvent.attendees)
        stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_rel))
            .join(
                CalendarEventDealAssociation,
                col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
            )
            .outerjoin(
                Meeting,
                col(Meeting.calendar_event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.deal_id) == deal_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                col(CalendarEvent.end_at) < now,  # Meeting has ended
                col(CalendarEvent.online_meeting_id).isnot(None),  # Has online meeting
                or_(
                    # No Meeting record yet → first attempt
                    col(Meeting.meeting_id).is_(None),
                    # PENDING with attempts < MAX → retry
                    and_(
                        col(Meeting.status) == MeetingStatus.PENDING,
                        col(Meeting.transcript_fetch_attempts) < TRANSCRIPT_MAX_FETCH_ATTEMPTS,
                    ),
                    # FAILED with attempts < MAX → retry
                    and_(
                        col(Meeting.status) == MeetingStatus.FAILED,
                        col(Meeting.transcript_fetch_attempts) < TRANSCRIPT_MAX_FETCH_ATTEMPTS,
                    ),
                ),
            )
            .distinct()
        )

        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def process_deal(self, deal_id: UUID) -> DealTranscriptFetchResult:
        """Process transcript fetch for a single deal and trigger pipeline.

        Deal-centric processing:
        1. Fetch transcripts for this deal's meetings
        2. Immediately trigger meeting summary pipeline for this deal
        3. Pipeline generates meeting summaries -> risk + stakeholder -> deal memory

        Uses fresh session pattern throughout.

        Args:
            deal_id: Deal to process.

        Returns:
            DealTranscriptFetchResult with counts and pipeline status.
        """
        from src.workers.deal_worker.services.meeting_insight_services import (
            MeetingPrepService,
            MeetingSummaryService,
        )

        result = DealTranscriptFetchResult(deal_id=deal_id)

        # Phase 1: Get meetings for this deal (fresh session)
        events: list[CalendarEvent] = []
        async for session in acquire_org_async_session(self.organisation_id):
            events = await self._get_meetings_for_deal(session, deal_id)
            result.meetings_found = len(events)
        # Session released

        # Phase 2: Fetch transcripts if any meetings need it
        if events:
            logger.info(
                "  Fetching transcripts for %d meeting(s) in deal %s",
                len(events),
                deal_id,
            )

            processing_results = await self._process_meetings_sequential(events)

            # Aggregate results using comprehensions
            statuses = [res.get("status", "failed") for res in processing_results]
            result.transcripts_fetched = sum(1 for s in statuses if s == "fetched")
            result.transcripts_pending = sum(1 for s in statuses if s == "pending")
            result.transcripts_not_available = sum(1 for s in statuses if s == "not_available")
            result.transcripts_failed = sum(
                1 for s in statuses if s not in ("fetched", "pending", "not_available")
            )

            logger.info(
                "  Transcript fetch for deal %s: found=%d, fetched=%d, pending=%d, not_available=%d, failed=%d",
                deal_id,
                result.meetings_found,
                result.transcripts_fetched,
                result.transcripts_pending,
                result.transcripts_not_available,
                result.transcripts_failed,
            )
        else:
            logger.info("  No meetings need transcript fetch for deal %s", deal_id)

        # Phase 3: ALWAYS trigger pipeline for this deal (unconditionally)
        # The pipeline has its own query (_get_events_needing_summary) that finds
        # READY transcripts needing summaries - independent of transcript fetch results.
        # This ensures summaries are generated even if transcripts were fetched in previous runs.
        # Pipeline: meeting summaries (oldest first) -> parallel risk+stakeholder -> deal memory
        logger.info("  Triggering meeting summary pipeline for deal %s", deal_id)
        result.pipeline_triggered = True
        summary_service = MeetingSummaryService(self.organisation_id)
        pipeline_result = await summary_service.process_deal_pipeline(deal_id)
        result.pipeline_meetings_processed = pipeline_result.meetings_processed
        result.pipeline_deal_memory_triggered = pipeline_result.deal_memory_triggered
        result.pipeline_risk_blocker_triggered = pipeline_result.risk_blocker_triggered
        result.pipeline_stakeholder_triggered = pipeline_result.stakeholder_analysis_triggered

        logger.info(
            "  Pipeline result for deal %s: jobs_created=%d, jobs_skipped=%d, "
            "tasks_triggered=%d, deal_memory=%s, risk_blocker=%s, stakeholder=%s",
            deal_id,
            pipeline_result.jobs_created,
            pipeline_result.jobs_skipped,
            pipeline_result.tasks_triggered,
            pipeline_result.deal_memory_triggered,
            pipeline_result.risk_blocker_triggered,
            pipeline_result.stakeholder_analysis_triggered,
        )

        # Phase 4: Check for upcoming meetings needing prep (within 2 days)
        # Only triggers for the NEXT upcoming meeting if:
        # - Prep not already generated
        # - All past meeting summaries are complete (prep needs this context)
        prep_service = MeetingPrepService(self.organisation_id)
        prep_result = await prep_service.process_deal_pipeline(deal_id)

        if prep_result.meeting_found:
            result.prep_triggered = prep_result.task_triggered
            result.prep_event_id = prep_result.event_id
            result.prep_skipped_pending_summaries = prep_result.skipped_pending_summaries
            logger.info(
                "  Prep pipeline for deal %s: meeting_found=%s, job_created=%s, "
                "task_triggered=%s, skipped_pending_summaries=%s, event=%s",
                deal_id,
                prep_result.meeting_found,
                prep_result.job_created,
                prep_result.task_triggered,
                prep_result.skipped_pending_summaries,
                prep_result.event_id,
            )

        return result

    async def _reset_stuck_fetching_meetings(self) -> int:
        """Reset meetings stuck in FETCHING state back to PENDING.

        This should be called at the end of process_all() to clean up any
        meetings that got stuck in FETCHING state due to errors, timeouts,
        or worker crashes.

        Returns:
            Number of meetings reset from FETCHING to PENDING.
        """
        from sqlalchemy import update
        from sqlalchemy.engine import CursorResult

        reset_count = 0

        async for session in acquire_org_async_session(self.organisation_id):
            # Update all FETCHING meetings back to PENDING
            stmt = (
                update(Meeting)
                .where(
                    col(Meeting.organisation_id) == self.organisation_id,
                    col(Meeting.status) == MeetingStatus.FETCHING,
                )
                .values(
                    status=MeetingStatus.PENDING,
                    status_message="Reset from FETCHING state after task completion",
                )
            )
            cursor_result = cast(CursorResult[Any], await session.execute(stmt))
            reset_count = cursor_result.rowcount or 0
            await session.commit()

        if reset_count > 0:
            logger.info(
                "Reset %d meeting(s) from FETCHING to PENDING for org %s",
                reset_count,
                self.organisation_id,
            )

        return reset_count

    async def process_all(self) -> TranscriptFetchResult:
        """Process transcript fetch for all deals in the organisation.

        Deal-centric approach:
        - For each deal with meetings needing transcript fetch:
          1. Fetch transcripts for that deal's meetings
          2. Immediately trigger pipeline (meeting summaries -> risk + stakeholder -> deal memory)
          3. Move to next deal

        This is simpler and more intuitive than fetching all transcripts first
        then triggering pipelines separately.

        Returns:
            TranscriptFetchResult with aggregate counts.
        """
        result = TranscriptFetchResult()

        # Phase 1: Get all deals needing transcript fetch (fresh session)
        deal_ids: list[UUID] = []
        async for session in acquire_org_async_session(self.organisation_id):
            deal_ids = await self._get_open_deals(session)
        # Session released

        if not deal_ids:
            logger.info("No deals need transcript fetch for org %s", self.organisation_id)
            return result

        logger.info(
            "Found %d deals needing transcript fetch for org %s",
            len(deal_ids),
            self.organisation_id,
        )

        # Phase 2: Process each deal sequentially
        # For each deal: fetch transcripts → trigger pipeline → move to next
        for idx, deal_id in enumerate(deal_ids, 1):
            logger.info(
                "[%d/%d] Processing deal %s for transcript fetch",
                idx,
                len(deal_ids),
                deal_id,
            )
            deal_result = await self.process_deal(deal_id)
            result.deal_results.append(deal_result)
            logger.info(
                "[%d/%d] Deal %s complete: meetings=%d, fetched=%d, pending=%d, not_available=%d, failed=%d",
                idx,
                len(deal_ids),
                deal_id,
                deal_result.meetings_found,
                deal_result.transcripts_fetched,
                deal_result.transcripts_pending,
                deal_result.transcripts_not_available,
                deal_result.transcripts_failed,
            )
            result.deals_processed += 1
            result.meetings_found += deal_result.meetings_found
            result.transcripts_fetched += deal_result.transcripts_fetched
            result.transcripts_pending += deal_result.transcripts_pending
            result.transcripts_not_available += deal_result.transcripts_not_available
            result.transcripts_failed += deal_result.transcripts_failed
            if deal_result.pipeline_triggered:
                result.pipelines_triggered += 1
            if deal_result.pipeline_deal_memory_triggered:
                result.deal_memories_triggered += 1
            if deal_result.pipeline_risk_blocker_triggered:
                result.risk_blockers_triggered += 1
            if deal_result.pipeline_stakeholder_triggered:
                result.stakeholder_analyses_triggered += 1
            # Track prep jobs
            if deal_result.prep_triggered:
                result.prep_jobs_created += 1
            elif deal_result.prep_skipped_pending_summaries:
                # Skipped because past summaries still pending
                result.prep_waiting_for_summaries += 1
            elif deal_result.prep_event_id:
                # Meeting found but job was skipped (already exists)
                result.prep_jobs_skipped += 1

        # Phase 3: Reset any meetings stuck in FETCHING state
        # This ensures they get retried in the next run
        result.fetching_reset = await self._reset_stuck_fetching_meetings()

        logger.info(
            "Transcript fetch complete for org %s: deals=%d, meetings=%d, fetched=%d, "
            "pending=%d, not_available=%d, failed=%d, pipelines=%d, deal_memories=%d, "
            "risk_blockers=%d, stakeholder_analyses=%d, fetching_reset=%d, "
            "prep_created=%d, prep_skipped=%d, prep_waiting=%d",
            self.organisation_id,
            result.deals_processed,
            result.meetings_found,
            result.transcripts_fetched,
            result.transcripts_pending,
            result.transcripts_not_available,
            result.transcripts_failed,
            result.pipelines_triggered,
            result.deal_memories_triggered,
            result.risk_blockers_triggered,
            result.stakeholder_analyses_triggered,
            result.fetching_reset,
            result.prep_jobs_created,
            result.prep_jobs_skipped,
            result.prep_waiting_for_summaries,
        )

        return result
