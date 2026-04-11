"""List Meetings By Date Tool - Flexible org-wide meeting listing.

This tool lists meetings across all deals with flexible date filtering.
Supports preset filters (upcoming, recent, today) and custom date ranges.

USE CASES:
- "What meetings are coming up this week?" → filter_type="upcoming"
- "Show me recent meetings" → filter_type="recent"
- "What meetings do I have today?" → filter_type="today"
- "List meetings from last month" → filter_type="custom" with dates
- "Show me meetings with john@company.com" → attendee_email="john@company.com"
- "List meetings with the CFO" → attendee_name="CFO" (partial match on display name)

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. organisation_id comes from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import aliased, selectinload

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.infra.utils.timezone import get_context_anchor_utc, get_context_timezone_name
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name

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
    timezone_name: str,
    anchor_utc: datetime,
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
    anchor_local = anchor_utc.astimezone(ZoneInfo(timezone_name))
    today_start_local = anchor_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_local = today_start_local + timedelta(days=1)

    if filter_type == "today":
        return today_start_local.astimezone(UTC), today_end_local.astimezone(UTC)

    if filter_type == "upcoming":
        return anchor_utc, (anchor_local + timedelta(days=days)).astimezone(UTC)

    if filter_type == "recent":
        return (anchor_local - timedelta(days=days)).astimezone(UTC), anchor_utc

    # filter_type == "custom"
    if start_date and end_date:
        tz = ZoneInfo(timezone_name)
        start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).astimezone(UTC)
        end_dt = datetime.combine(end_date, datetime.max.time(), tzinfo=tz).astimezone(UTC)
        return start_dt, end_dt

    # Fallback to last 7 days if custom dates not provided
    return (anchor_local - timedelta(days=7)).astimezone(UTC), anchor_utc


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
    attendee_email: str | None = None,
    attendee_name: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List the current user's meetings across deals within a date range.

    When context.user_email is set, only returns meetings where that email
    appears in the attendee list. When user_email is None, returns all
    meetings in the organisation (backwards-compatible).

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - user_email: Scopes results to the requesting user's meetings

    Args:
        context: AgentContext containing organisation metadata and user_email
        filter_type: "upcoming", "recent", "today", or "custom"
        days: Number of days for upcoming/recent filters (default: 7)
        start_date: Start date for custom filter
        end_date: End date for custom filter
        deal_id: Optional deal UUID to filter by specific deal
        has_transcript: Filter by transcript availability
        has_summary: Filter by summary availability
        attendee_email: Filter meetings where this email is an attendee (exact, case-insensitive)
        attendee_name: Filter meetings where an attendee's name contains this string (ILIKE)
        limit: Maximum number of meetings to return (default: 50)

    Returns:
        List of meeting dictionaries with deal context.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("list_meetings_by_date: missing organisation_id in context")
        return []

    effective_limit = max(1, min(limit, 100))
    timezone_name = get_context_timezone_name(context.metadata)
    anchor_utc = get_context_anchor_utc(context.metadata)
    start_dt, end_dt = _get_date_range(
        filter_type, days, start_date, end_date, timezone_name, anchor_utc
    )

    user_email = context.user_email

    # Table columns for type-safe queries
    event_c = cast(Any, CalendarEvent).__table__.c
    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
    deal_c = cast(Any, CRMDeal).__table__.c
    attendee_c = cast(Any, CalendarAttendee).__table__.c

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

    # Filter by user email — only return meetings where the user is an attendee
    if user_email:
        stmt = stmt.join(CalendarAttendee, attendee_c.event_id == event_c.event_id).where(
            attendee_c.email == user_email
        )

    # Optional attendee filter (separate from user_email scoping)
    if attendee_email or attendee_name:
        attendee_filter = aliased(CalendarAttendee)
        filter_c = cast(Any, attendee_filter).__table__.c
        stmt = stmt.join(attendee_filter, filter_c.event_id == event_c.event_id)
        if attendee_email:
            stmt = stmt.where(func.lower(filter_c.email) == attendee_email.lower())
        if attendee_name:
            stmt = stmt.where(filter_c.display_name.ilike(f"%{attendee_name}%"))

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
                    "organiser_name": resolve_attendee_display_name(
                        event.organiser_name, event.organiser_email
                    ),
                    "organiser_email": event.organiser_email,
                    "has_transcript": has_transcript_val,
                    "has_summary": assoc.meeting_summary_status == GenerationStatus.COMPLETED,
                    "has_prep": assoc.meeting_prep_status == GenerationStatus.COMPLETED,
                }
            )

    logger.info(
        "list_meetings_by_date: found %d meetings (filter=%s, days=%d, timezone=%s, anchor=%s)",
        len(output),
        filter_type,
        days,
        timezone_name,
        anchor_utc.isoformat(),
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
        attendee_email: str | None = None,
        attendee_name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List the current user's meetings with flexible date filtering.

        Returns only meetings where the current user is an attendee.
        Use this tool when the user asks about their meetings, schedule, or calendar.
        Supports filtering by a specific attendee's email or name.

        Args:
            filter_type: Date filter preset. Must be one of:
                - "upcoming": Meetings in the next N days from now (use 'days' to control N, default 7).
                - "recent": Meetings in the past N days from now (use 'days' to control N, default 7).
                - "today": Only today's meetings (ignores 'days', 'start_date', 'end_date').
                - "custom": Custom date range. Requires both 'start_date' and 'end_date'.
            days: Number of days for "upcoming" or "recent" filter_type. Default is 7.
                Ignored when filter_type is "today" or "custom".
                Examples: days=1 for tomorrow only, days=14 for next two weeks, days=30 for next month.
            start_date: Start of custom date range in ISO format (YYYY-MM-DD), e.g. "2025-01-15".
                Only used when filter_type is "custom". Must be paired with end_date.
            end_date: End of custom date range in ISO format (YYYY-MM-DD), e.g. "2025-01-31".
                Only used when filter_type is "custom". Must be paired with start_date.
            deal_id: Optional deal UUID string to narrow results to a specific deal.
                If omitted, returns meetings across all deals.
            has_transcript: If True, only meetings with a transcript. If False, only without.
                If omitted (None), no transcript filter is applied.
            has_summary: If True, only meetings with an AI summary. If False, only without.
                If omitted (None), no summary filter is applied.
            attendee_email: Filter meetings where this email is an attendee
                (exact match, case-insensitive). Useful for finding meetings with a specific person.
            attendee_name: Filter meetings where an attendee's display name contains this string
                (case-insensitive partial match). Useful for role-based searches like "CFO".
            limit: Maximum number of meetings to return. Default 50, max 100.

        Returns:
            List of meeting objects, each containing:
            - event_id: Calendar event UUID
            - title: Meeting subject/title
            - start_at: Meeting start time in ISO 8601 datetime format (UTC)
            - end_at: Meeting end time in ISO 8601 datetime format (UTC)
            - duration_minutes: Meeting duration in minutes
            - deal: Associated deal info (deal_id, name, company_name, amount, currency)
            - attendee_count: Total number of attendees
            - external_attendee_count: Number of external (non-organiser) attendees
            - organiser_name: Name of the meeting organiser
            - organiser_email: Email of the meeting organiser
            - has_transcript: Whether a meeting transcript is available
            - has_summary: Whether an AI-generated summary is available
            - has_prep: Whether meeting preparation notes are available
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
                "attendee_email": attendee_email,
                "attendee_name": attendee_name,
                "limit": limit,
            },
            description=f"Finding {filter_type} meetings",
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
