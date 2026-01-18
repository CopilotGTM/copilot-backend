"""Transcript Fetch Task Service.

This service handles the business logic for fetching transcripts for deal-associated meetings.
It is used by the scheduled Celery tasks in transcript_fetch_tasks.py.

Architecture:
    - Single query to get all meetings needing transcript fetch
    - Process meetings in parallel with integration priority
    - Track retry attempts and status transitions
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
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
from src.service.integrations.transcript_service import TranscriptService

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Configuration
TRANSCRIPT_LOOKBACK_DAYS = 90  # Always look back 90 days for all meetings
TRANSCRIPT_MAX_FETCH_ATTEMPTS = 2  # Retry twice (~1 hour window at 30 min intervals)
TRANSCRIPT_PARALLEL_FETCH_LIMIT = 3  # Max concurrent fetches per org


@dataclass
class TranscriptFetchResult:
    """Result of transcript fetch for the organisation."""

    meetings_found: int = 0  # Meetings needing fetch
    transcripts_fetched: int = 0  # Successfully fetched
    transcripts_pending: int = 0  # Will retry next run
    transcripts_not_available: int = 0  # Permanently failed
    transcripts_failed: int = 0  # Error during fetch


class TranscriptFetchTaskService:
    """Service for fetching transcripts for deal-associated meetings.

    Uses fresh session pattern for better connection management:
    - Org-level: Acquires fresh session per parallel meeting task
    - Each parallel task is independent with its own session + TranscriptService
    - Sessions are released between meetings, not held during external API calls

    Simple approach:
    1. Single query to get all meetings needing transcript fetch
    2. Process them in parallel with integration priority (fresh session per task)
    3. Track results
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

    async def _process_single_meeting_internal(
        self, session: AsyncSession, event: CalendarEvent
    ) -> dict[str, Any]:
        """Process a single meeting: try to fetch transcript with owner first, then fallback.

        Args:
            session: Database session to use for queries and writes.
            event: CalendarEvent to process (with attendees already loaded).

        Returns:
            Dict with result: {"status": "fetched"|"pending"|"not_available"|"failed", ...}
        """
        result: dict[str, Any] = {
            "event_id": str(event.event_id),
            "status": "failed",
            "integrations_tried": 0,
        }

        # Create TranscriptService with this session
        transcript_service = TranscriptService(session, self.organisation_id)

        # Get existing Meeting record (if any)
        existing_meeting = await transcript_service.get_meeting_by_event(event.event_id)
        current_attempts = existing_meeting.transcript_fetch_attempts if existing_meeting else 0

        # Find integrations to try (owner first, then attendees)
        integrations = await transcript_service.find_integrations_for_event(event)

        if not integrations:
            logger.warning(
                "No integrations found for event %s (organizer: %s)",
                event.event_id,
                event.organiser_email,
            )
            # No integrations available - create/update Meeting record
            new_attempts = current_attempts + 1
            if not existing_meeting:
                meeting = Meeting(
                    organisation_id=self.organisation_id,
                    calendar_event_id=event.event_id,
                    status=MeetingStatus.PENDING
                    if new_attempts < TRANSCRIPT_MAX_FETCH_ATTEMPTS
                    else MeetingStatus.NOT_AVAILABLE,
                    status_message="No integrations available to fetch transcript",
                    transcript_fetch_attempts=new_attempts,
                )
                session.add(meeting)
            else:
                existing_meeting.transcript_fetch_attempts = new_attempts
                if new_attempts >= TRANSCRIPT_MAX_FETCH_ATTEMPTS:
                    existing_meeting.status = MeetingStatus.NOT_AVAILABLE
                    existing_meeting.status_message = "No integrations available after max attempts"
                else:
                    existing_meeting.status = MeetingStatus.PENDING
                    existing_meeting.status_message = f"No integrations available (attempt {new_attempts}/{TRANSCRIPT_MAX_FETCH_ATTEMPTS})"

            await session.commit()
            result["status"] = (
                "not_available" if new_attempts >= TRANSCRIPT_MAX_FETCH_ATTEMPTS else "pending"
            )
            return result

        # Try each integration in order
        meeting = existing_meeting

        for integration in integrations:
            result["integrations_tried"] += 1

            fetch_result = await transcript_service.try_fetch_with_integration(
                event=event,
                integration=integration,
                existing_meeting=meeting,
            )

            meeting = fetch_result.meeting

            if fetch_result.success:
                # Success! Transcript fetched
                if meeting:
                    meeting.transcript_fetch_attempts = current_attempts + 1
                    await session.commit()
                result["status"] = "fetched"
                result["fetched_with"] = integration.integration_email
                logger.info(
                    "Transcript fetched for event %s using %s",
                    event.event_id,
                    integration.integration_email,
                )
                return result

            if not fetch_result.should_retry_other_integration:
                # Don't try other integrations (e.g., provider not supported)
                break

            logger.debug(
                "Integration %s failed for event %s, trying next...",
                integration.integration_email,
                event.event_id,
            )

        # All integrations tried, no transcript found
        new_attempts = current_attempts + 1

        if meeting:
            meeting.transcript_fetch_attempts = new_attempts

            if new_attempts >= TRANSCRIPT_MAX_FETCH_ATTEMPTS:
                # Max attempts reached - mark as permanently not available
                meeting.status = MeetingStatus.NOT_AVAILABLE
                meeting.status_message = (
                    f"No transcript after {TRANSCRIPT_MAX_FETCH_ATTEMPTS} attempts "
                    f"with {result['integrations_tried']} integration(s). "
                    "Meeting may not have been recorded."
                )
                result["status"] = "not_available"
                logger.info(
                    "Transcript permanently not available for event %s after %d attempts",
                    event.event_id,
                    new_attempts,
                )
            else:
                # Keep as PENDING for retry on next run
                meeting.status = MeetingStatus.PENDING
                meeting.status_message = (
                    f"Attempt {new_attempts}/{TRANSCRIPT_MAX_FETCH_ATTEMPTS}: "
                    f"No transcript found with {result['integrations_tried']} integration(s). "
                    "Will retry - transcript may still be processing."
                )
                result["status"] = "pending"
                logger.info(
                    "No transcript for event %s (attempt %d/%d, will retry)",
                    event.event_id,
                    new_attempts,
                    TRANSCRIPT_MAX_FETCH_ATTEMPTS,
                )

            await session.commit()

        return result

    async def _process_meetings_parallel(
        self,
        events: list[CalendarEvent],
        max_concurrent: int = TRANSCRIPT_PARALLEL_FETCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Process multiple meetings in parallel with concurrency limit.

        Each parallel task acquires its own fresh session to avoid holding
        connections during external API calls (Teams/Zoom/Meet).

        Args:
            events: List of CalendarEvent objects (with attendees already loaded).
            max_concurrent: Maximum concurrent tasks (default 3).

        Returns:
            List of result dictionaries from each meeting processing.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_with_semaphore(event: CalendarEvent) -> dict[str, Any]:
            async with semaphore:
                # Each parallel task gets its own fresh session
                async for session in acquire_org_async_session(self.organisation_id):
                    try:
                        return await self._process_single_meeting_internal(session, event)
                    except Exception as e:
                        await session.rollback()
                        logger.error(
                            "Error processing event %s: %s",
                            event.event_id,
                            str(e),
                            exc_info=True,
                        )
                        return {
                            "event_id": str(event.event_id),
                            "status": "failed",
                            "error": str(e),
                        }

                # Session not acquired (shouldn't happen)
                return {
                    "event_id": str(event.event_id),
                    "status": "failed",
                    "error": "Failed to acquire database session",
                }

        tasks = [process_with_semaphore(event) for event in events]
        return await asyncio.gather(*tasks)

    async def process_all(self) -> TranscriptFetchResult:
        """Process transcript fetch for all eligible meetings in the organisation.

        Uses fresh session pattern for better connection management:
        - Phase 1: Fresh session to query meetings needing fetch
        - Phase 2: Parallel processing, each task gets its own fresh session

        This ensures connections are released between meetings and not held
        during external API calls to Teams/Zoom/Meet.

        Returns:
            TranscriptFetchResult with aggregate counts.
        """
        result = TranscriptFetchResult()

        # Phase 1: Query meetings needing fetch (fresh session for read)
        events: list[CalendarEvent] = []
        async for session in acquire_org_async_session(self.organisation_id):
            events = await self._get_meetings_needing_fetch_internal(session)
            result.meetings_found = len(events)
            # Session released after this block - events are detached but
            # attendees were eagerly loaded via selectinload

        if not events:
            logger.info("No meetings need transcript fetch for org %s", self.organisation_id)
            return result

        logger.info(
            "Found %d meetings needing transcript fetch for org %s",
            len(events),
            self.organisation_id,
        )

        # Phase 2: Process meetings in parallel (each task gets fresh session)
        processing_results = await self._process_meetings_parallel(events)

        # Aggregate results
        for res in processing_results:
            status = res.get("status", "failed")
            if status == "fetched":
                result.transcripts_fetched += 1
            elif status == "pending":
                result.transcripts_pending += 1
            elif status == "not_available":
                result.transcripts_not_available += 1
            else:
                result.transcripts_failed += 1

        logger.info(
            "Transcript fetch complete for org %s: found=%d, fetched=%d, pending=%d, not_available=%d, failed=%d",
            self.organisation_id,
            result.meetings_found,
            result.transcripts_fetched,
            result.transcripts_pending,
            result.transcripts_not_available,
            result.transcripts_failed,
        )

        return result
