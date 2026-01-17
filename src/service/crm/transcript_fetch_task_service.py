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
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
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

    Simple approach:
    1. Single query to get all meetings needing transcript fetch
    2. Process them in parallel with integration priority
    3. Track results
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """Initialize the service.

        Args:
            session: Async database session scoped to the organisation
            organisation_id: UUID of the organisation
        """
        self.session = session
        self.organisation_id = organisation_id
        self.transcript_service = TranscriptService(session, organisation_id)

    async def get_meetings_needing_fetch(self) -> list[CalendarEvent]:
        """Get all meetings that need transcript fetch for the organisation.

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

        stmt = (
            select(CalendarEvent)
            .options(selectinload(CalendarEvent.attendees))  # type: ignore
            .join(
                CalendarEventDealAssociation,
                CalendarEventDealAssociation.event_id == CalendarEvent.event_id,  # type: ignore
            )
            .outerjoin(Meeting, Meeting.calendar_event_id == CalendarEvent.event_id)  # type: ignore
            .where(
                CalendarEvent.organisation_id == self.organisation_id,  # type: ignore
                CalendarEvent.end_at < now,  # type: ignore  # Meeting has ended
                CalendarEvent.online_meeting_id.isnot(None),  # type: ignore  # Has online meeting
                CalendarEventDealAssociation.status == AssociationStatus.ACTIVE,  # type: ignore
                or_(
                    # No Meeting record yet → first attempt
                    Meeting.meeting_id.is_(None),  # type: ignore
                    # PENDING with attempts < MAX → retry
                    and_(
                        Meeting.status == MeetingStatus.PENDING,  # type: ignore
                        Meeting.transcript_fetch_attempts < TRANSCRIPT_MAX_FETCH_ATTEMPTS,  # type: ignore
                    ),
                    # FAILED with attempts < MAX → retry
                    and_(
                        Meeting.status == MeetingStatus.FAILED,  # type: ignore
                        Meeting.transcript_fetch_attempts < TRANSCRIPT_MAX_FETCH_ATTEMPTS,  # type: ignore
                    ),
                ),
            )
            .distinct()  # In case a meeting is associated with multiple deals
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def process_single_meeting(self, event: CalendarEvent) -> dict[str, Any]:
        """Process a single meeting: try to fetch transcript with owner first, then fallback.

        Returns:
            Dict with result: {"status": "fetched"|"pending"|"not_available"|"failed", ...}
        """
        result: dict[str, Any] = {
            "event_id": str(event.event_id),
            "status": "failed",
            "integrations_tried": 0,
        }

        # Get existing Meeting record (if any)
        existing_meeting = await self.transcript_service.get_meeting_by_event(event.event_id)
        current_attempts = existing_meeting.transcript_fetch_attempts if existing_meeting else 0

        # Find integrations to try (owner first, then attendees)
        integrations = await self.transcript_service.find_integrations_for_event(event)

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
                self.session.add(meeting)
            else:
                existing_meeting.transcript_fetch_attempts = new_attempts
                if new_attempts >= TRANSCRIPT_MAX_FETCH_ATTEMPTS:
                    existing_meeting.status = MeetingStatus.NOT_AVAILABLE
                    existing_meeting.status_message = "No integrations available after max attempts"
                else:
                    existing_meeting.status = MeetingStatus.PENDING
                    existing_meeting.status_message = f"No integrations available (attempt {new_attempts}/{TRANSCRIPT_MAX_FETCH_ATTEMPTS})"

            await self.session.commit()
            result["status"] = (
                "not_available" if new_attempts >= TRANSCRIPT_MAX_FETCH_ATTEMPTS else "pending"
            )
            return result

        # Try each integration in order
        meeting = existing_meeting

        for integration in integrations:
            result["integrations_tried"] += 1

            fetch_result = await self.transcript_service.try_fetch_with_integration(
                event=event,
                integration=integration,
                existing_meeting=meeting,
            )

            meeting = fetch_result.meeting

            if fetch_result.success:
                # Success! Transcript fetched
                if meeting:
                    meeting.transcript_fetch_attempts = current_attempts + 1
                    await self.session.commit()
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

            await self.session.commit()

        return result

    async def process_meetings_parallel(
        self,
        events: list[CalendarEvent],
        max_concurrent: int = TRANSCRIPT_PARALLEL_FETCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Process multiple meetings in parallel with concurrency limit."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_with_semaphore(event: CalendarEvent) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await self.process_single_meeting(event)
                except Exception as e:
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

        tasks = [process_with_semaphore(event) for event in events]
        return await asyncio.gather(*tasks)

    async def process_all(self) -> TranscriptFetchResult:
        """Process transcript fetch for all eligible meetings in the organisation.

        Simple flow:
        1. Get all meetings needing fetch (single query)
        2. Process in parallel
        3. Return results
        """
        result = TranscriptFetchResult()

        # Single query to get all meetings needing transcript fetch
        events = await self.get_meetings_needing_fetch()
        result.meetings_found = len(events)

        if not events:
            logger.info("No meetings need transcript fetch for org %s", self.organisation_id)
            return result

        logger.info(
            "Found %d meetings needing transcript fetch for org %s",
            len(events),
            self.organisation_id,
        )

        # Process all meetings in parallel
        processing_results = await self.process_meetings_parallel(events)

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
