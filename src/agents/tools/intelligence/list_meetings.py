"""List deal meetings tool for agents.

Tool for listing meetings associated with a deal.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Prevents cross-deal data access (deal-scoped tool)

This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
as parameters - both come from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_meetings(
    *,
    context: AgentContext,
    limit: int = 10,
    include_past: bool = True,
    include_upcoming: bool = True,
) -> list[dict[str, Any]]:
    """List all meetings associated with a deal.

    Returns meeting summary information including metadata about
    available transcripts and AI-generated insights.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    Args:
        context: AgentContext containing organisation and deal metadata
        limit: Maximum number of meetings to return (default: 10)
        include_past: Include meetings that have already occurred (default: True)
        include_upcoming: Include future meetings (default: True)

    Returns:
        List of meeting dictionaries with summary information:
        - event_id: Calendar event UUID
        - title: Meeting title/subject
        - start_at: Meeting start time (ISO format)
        - end_at: Meeting end time (ISO format)
        - status: Calendar event status
        - organiser_name: Name of meeting organizer
        - organiser_email: Email of meeting organizer
        - attendee_count: Number of attendees
        - attendees: List of attendee info (email, name, response_status)
        - conference_link: Video conference URL (if available)
        - location: Meeting location
        - has_transcript: Whether transcript is available
        - has_summary: Whether AI summary is available
        - has_prep: Whether meeting prep is available
        - has_followup: Whether follow-up actions are available
        - association_status: SUGGESTED, CONFIRMED, or REJECTED
        - match_score: Confidence score of deal association
    """
    # SECURITY: Both organisation_id and deal_id come from context, not parameters
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("list_deal_meetings: missing organisation_id in context")
        return []

    if not deal_id:
        logger.warning("list_deal_meetings: missing deal_id in context")
        return []

    effective_limit = max(1, min(limit, 50))
    now = datetime.now(UTC)

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
    event_c = cast(Any, CalendarEvent).__table__.c

    # Build query - join CalendarEventDealAssociation with CalendarEvent
    # Use cast(Any, ...) for relationship attributes to satisfy type checker
    stmt = (
        select(CalendarEventDealAssociation)
        .options(
            selectinload(cast(Any, CalendarEventDealAssociation).event).selectinload(
                cast(Any, CalendarEvent).attendees
            )
        )
        .where(
            assoc_c.organisation_id == organisation_id,
            assoc_c.deal_id == deal_id,
            assoc_c.status == AssociationStatus.ACTIVE,  # Only include active associations
        )
        .join(CalendarEvent, assoc_c.event_id == event_c.event_id)
    )

    # Apply time filters
    if not include_past and not include_upcoming:
        logger.warning("list_deal_meetings: both include_past and include_upcoming are False")
        return []

    if not include_past:
        stmt = stmt.where(event_c.start_at >= now)
    if not include_upcoming:
        stmt = stmt.where(event_c.start_at < now)

    # Order by start time (most recent first for past, soonest first for upcoming)
    stmt = stmt.order_by(event_c.start_at.desc()).limit(effective_limit)

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            associations = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deal_meetings: database error: {e}")
            return []

        # For each event, check if there's a Meeting record with transcript
        event_ids = [assoc.event_id for assoc in associations]

        meeting_c = cast(Any, Meeting).__table__.c
        meeting_stmt = select(Meeting).where(
            meeting_c.organisation_id == organisation_id,
            meeting_c.calendar_event_id.in_(event_ids),
            meeting_c.is_deleted.is_(False),
        )

        try:
            meeting_result = await session.execute(meeting_stmt)
            meetings = {m.calendar_event_id: m for m in meeting_result.scalars().all()}
        except Exception as e:
            logger.exception(f"list_deal_meetings: error fetching meetings: {e}")
            meetings = {}

        # Transform to output format (while session is active)
        output: list[dict[str, Any]] = []
        for assoc in associations:
            event = assoc.event
            meeting = meetings.get(event.event_id)

            # Build attendee list
            attendees = []
            for att in event.attendees[:10]:  # Limit attendees in output
                attendees.append(
                    {
                        "email": att.email,
                        "name": att.display_name,
                        "response_status": att.response_status,
                        "is_organiser": att.is_organiser,
                    }
                )

            output.append(
                {
                    "event_id": str(event.event_id),
                    "title": event.title,
                    "start_at": event.start_at.isoformat() if event.start_at else None,
                    "end_at": event.end_at.isoformat() if event.end_at else None,
                    "status": event.status,
                    "organiser_name": event.organiser_name,
                    "organiser_email": event.organiser_email,
                    "attendee_count": event.attendee_count,
                    "attendees": attendees,
                    "location": event.location,
                    # Transcript/intelligence status
                    "has_transcript": meeting is not None and meeting.status == MeetingStatus.READY,
                    "has_summary": assoc.meeting_summary_status == GenerationStatus.COMPLETED,
                    "has_prep": assoc.meeting_prep_status == GenerationStatus.COMPLETED,
                    "has_followup": assoc.meeting_followup_status == GenerationStatus.COMPLETED,
                    # Association metadata
                    "association_status": assoc.status.value
                    if isinstance(assoc.status, AssociationStatus)
                    else assoc.status,
                    "match_score": assoc.match_score,
                }
            )
    # Session released

    logger.info("list_deal_meetings: found %d meetings for deal %s", len(output), deal_id)
    return output


def create_list_deal_meetings(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_meetings tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
    as parameters - both come from context.
    """

    async def list_deal_meetings_tool(
        limit: int = 10,
        include_past: bool = True,
        include_upcoming: bool = True,
    ) -> list[dict[str, Any]]:
        """List all meetings associated with the current deal.

        Use this tool to see what meetings have occurred or are scheduled
        with a customer. Shows both past meetings (with transcript availability)
        and upcoming meetings.

        Args:
            limit: Maximum number of results (default: 10, max: 50)
            include_past: Include past meetings (default: True)
            include_upcoming: Include future meetings (default: True)

        Note: organisation_id and deal_id are automatically set from context
        and cannot be overridden. This tool only queries the current deal.
        """
        logger.info(
            "[TOOL CALLED] list_deal_meetings - limit=%d, include_past=%s, include_upcoming=%s",
            limit,
            include_past,
            include_upcoming,
        )

        if not context.organisation_id:
            logger.warning("list_deal_meetings unavailable: missing organisation_id in context")
            return []

        if not context.deal_id:
            logger.warning("list_deal_meetings unavailable: missing deal_id in context")
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealMeetings",
            tool_func=list_deal_meetings,
            parameters={
                "context": context,
                "limit": limit,
                "include_past": include_past,
                "include_upcoming": include_upcoming,
            },
            description=f"Listing meetings for deal: {context.deal_id}",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] list_deal_meetings returned %d meetings",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return list_deal_meetings_tool
