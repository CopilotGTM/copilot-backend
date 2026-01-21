"""List Meetings By Date Tool - Flexible org-wide meeting listing.

This tool lists meetings across all deals with flexible date filtering.
Supports preset filters (upcoming, recent, today) and custom date ranges.

USE CASES:
- "What meetings are coming up this week?" → filter_type="upcoming"
- "Show me recent meetings" → filter_type="recent"
- "What meetings do I have today?" → filter_type="today"
- "List meetings from last month" → filter_type="custom" with dates

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. organisation_id comes from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# Type alias for filter types
FilterType = Literal["upcoming", "recent", "today", "custom"]


def _get_date_range(
    filter_type: FilterType,
    days: int,
    start_date: date | None,
    end_date: date | None,
) -> tuple[datetime, datetime]:
    """Calculate start and end datetime based on filter type.

    Args:
        filter_type: One of "upcoming", "recent", "today", "custom"
        days: Number of days for upcoming/recent filters
        start_date: Custom start date (for filter_type="custom")
        end_date: Custom end date (for filter_type="custom")

    Returns:
        Tuple of (start_datetime, end_datetime) in UTC
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    if filter_type == "today":
        return today_start, today_end

    if filter_type == "upcoming":
        return now, now + timedelta(days=days)

    if filter_type == "recent":
        return now - timedelta(days=days), now

    # filter_type == "custom"
    if start_date and end_date:
        start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
        end_dt = datetime.combine(end_date, datetime.max.time(), tzinfo=UTC)
        return start_dt, end_dt

    # Fallback to last 7 days if custom dates not provided
    return now - timedelta(days=7), now


async def list_meetings_by_date(
    *,
    context: AgentContext,
    filter_type: FilterType = "upcoming",
    days: int = 7,
    start_date: date | None = None,
    end_date: date | None = None,
    deal_id: str | None = None,
    has_transcript: bool | None = None,
    has_summary: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List meetings across all deals within a date range.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        context: AgentContext containing organisation metadata
        filter_type: "upcoming", "recent", "today", or "custom"
        days: Number of days for upcoming/recent filters (default: 7)
        start_date: Start date for custom filter
        end_date: End date for custom filter
        deal_id: Optional deal UUID to filter by specific deal
        has_transcript: Filter by transcript availability
        has_summary: Filter by summary availability
        limit: Maximum number of meetings to return (default: 50)

    Returns:
        List of meeting dictionaries with deal context.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("list_meetings_by_date: missing organisation_id in context")
        return []

    effective_limit = max(1, min(limit, 100))
    start_dt, end_dt = _get_date_range(filter_type, days, start_date, end_date)

    # Table columns for type-safe queries
    event_c = cast(Any, CalendarEvent).__table__.c
    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
    deal_c = cast(Any, CRMDeal).__table__.c

    # Build query
    stmt = (
        select(CalendarEventDealAssociation)
        .options(
            selectinload(cast(Any, CalendarEventDealAssociation).event).selectinload(
                cast(Any, CalendarEvent).attendees
            ),
            selectinload(cast(Any, CalendarEventDealAssociation).deal),
        )
        .join(CalendarEvent, assoc_c.event_id == event_c.event_id)
        .join(CRMDeal, assoc_c.deal_id == deal_c.deal_id)
        .where(
            assoc_c.organisation_id == organisation_id,
            assoc_c.status == AssociationStatus.ACTIVE,
            event_c.start_at >= start_dt,
            event_c.start_at <= end_dt,
            deal_c.is_deleted.is_(False),
        )
    )

    # Optional deal filter
    if deal_id:
        stmt = stmt.where(assoc_c.deal_id == deal_id)

    # Optional summary filter
    if has_summary is True:
        stmt = stmt.where(assoc_c.meeting_summary_status == GenerationStatus.COMPLETED)
    elif has_summary is False:
        stmt = stmt.where(assoc_c.meeting_summary_status != GenerationStatus.COMPLETED)

    # Order by start time
    if filter_type == "upcoming":
        stmt = stmt.order_by(event_c.start_at.asc())  # Soonest first
    else:
        stmt = stmt.order_by(event_c.start_at.desc())  # Most recent first

    stmt = stmt.limit(effective_limit)

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            associations = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_meetings_by_date: database error: {e}")
            return []

        # Get meeting records for transcript status
        event_ids = [assoc.event_id for assoc in associations]
        meetings_map: dict[Any, Meeting] = {}

        if event_ids:
            meeting_c = cast(Any, Meeting).__table__.c
            meeting_stmt = select(Meeting).where(
                meeting_c.organisation_id == organisation_id,
                meeting_c.calendar_event_id.in_(event_ids),
                meeting_c.is_deleted.is_(False),
            )

            try:
                meeting_result = await session.execute(meeting_stmt)
                meetings_map = {m.calendar_event_id: m for m in meeting_result.scalars().all()}
            except Exception as e:
                logger.exception(f"list_meetings_by_date: error fetching meetings: {e}")

        # Build output
        output: list[dict[str, Any]] = []
        for assoc in associations:
            event = assoc.event
            deal = assoc.deal
            meeting = meetings_map.get(event.event_id)

            # Check transcript filter
            has_transcript_val = meeting is not None and meeting.status == MeetingStatus.READY
            if has_transcript is True and not has_transcript_val:
                continue
            if has_transcript is False and has_transcript_val:
                continue

            # Count external attendees
            external_count = sum(
                1 for att in (event.attendees or []) if att.email and not att.is_organiser
            )

            output.append(
                {
                    "event_id": str(event.event_id),
                    "title": event.title,
                    "start_at": event.start_at.isoformat() if event.start_at else None,
                    "end_at": event.end_at.isoformat() if event.end_at else None,
                    "duration_minutes": (
                        int((event.end_at - event.start_at).total_seconds() / 60)
                        if event.start_at and event.end_at
                        else None
                    ),
                    "deal": {
                        "deal_id": str(deal.deal_id),
                        "name": deal.name,
                        "company_name": deal.company_name,
                        "amount": float(deal.amount) if deal.amount else None,
                        "currency": deal.currency,
                    },
                    "attendee_count": event.attendee_count or 0,
                    "external_attendee_count": external_count,
                    "organiser_name": event.organiser_name,
                    "organiser_email": event.organiser_email,
                    "has_transcript": has_transcript_val,
                    "has_summary": assoc.meeting_summary_status == GenerationStatus.COMPLETED,
                    "has_prep": assoc.meeting_prep_status == GenerationStatus.COMPLETED,
                }
            )

    logger.info(
        "list_meetings_by_date: found %d meetings (filter=%s, days=%d)",
        len(output),
        filter_type,
        days,
    )
    return output


def create_list_meetings_by_date(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_meetings_by_date tool bound to a context.

    SECURITY: organisation_id comes from context (immutable).
    """

    async def list_meetings_by_date_tool(
        filter_type: str = "upcoming",
        days: int = 7,
        start_date: str | None = None,
        end_date: str | None = None,
        deal_id: str | None = None,
        has_transcript: bool | None = None,
        has_summary: bool | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List meetings with flexible date filtering.

        Use this tool to find meetings across all deals. Supports preset filters
        for common queries and custom date ranges.

        Args:
            filter_type: Filter preset - "upcoming", "recent", "today", or "custom"
                - "upcoming": Next N days (default: 7)
                - "recent": Past N days (default: 7)
                - "today": Today's meetings only
                - "custom": Use start_date and end_date
            days: Number of days for upcoming/recent filters (default: 7)
            start_date: Start date for custom filter (YYYY-MM-DD)
            end_date: End date for custom filter (YYYY-MM-DD)
            deal_id: Optional deal UUID to filter by specific deal
            has_transcript: Filter by transcript availability (True/False/None)
            has_summary: Filter by summary availability (True/False/None)
            limit: Maximum results (default: 50, max: 100)

        Returns list of meetings with:
        - event_id, title, start_at, end_at, duration_minutes
        - deal info (deal_id, name, company_name, amount)
        - attendee counts, organiser info
        - has_transcript, has_summary, has_prep flags
        """
        logger.info("[TOOL CALLED] list_meetings_by_date - filter=%s, days=%d", filter_type, days)

        if not context.organisation_id:
            logger.warning("list_meetings_by_date unavailable: missing organisation_id")
            return []

        # Parse dates if provided
        parsed_start: date | None = None
        parsed_end: date | None = None

        if start_date:
            try:
                parsed_start = date.fromisoformat(start_date)
            except ValueError:
                logger.warning("Invalid start_date format: %s", start_date)

        if end_date:
            try:
                parsed_end = date.fromisoformat(end_date)
            except ValueError:
                logger.warning("Invalid end_date format: %s", end_date)

        # Validate filter_type
        valid_filters: list[FilterType] = ["upcoming", "recent", "today", "custom"]
        if filter_type not in valid_filters:
            filter_type = "upcoming"

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListMeetingsByDate",
            tool_func=list_meetings_by_date,
            parameters={
                "context": context,
                "filter_type": cast(FilterType, filter_type),
                "days": days,
                "start_date": parsed_start,
                "end_date": parsed_end,
                "deal_id": deal_id,
                "has_transcript": has_transcript,
                "has_summary": has_summary,
                "limit": limit,
            },
            description=f"Listing {filter_type} meetings",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] list_meetings_by_date returned %d meetings",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return list_meetings_by_date_tool


__all__ = [
    "list_meetings_by_date",
    "create_list_meetings_by_date",
]
