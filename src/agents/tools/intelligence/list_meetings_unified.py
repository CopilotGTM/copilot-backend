"""Unified current-user calendar listing tool for agents.

Called by the General and realtime voice agents when a user asks about their
schedule or meeting history. This module owns the current-user calendar
boundary: results must come through the user's active calendar integration and
`CalendarEventUserState`, while deal rows are optional enrichment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased, selectinload

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.infra.utils.timezone import get_context_anchor_utc
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE.value,
    IntegrationType.MICROSOFT.value,
)


def normalize_meeting_limit(limit: int | None) -> int:
    """Clamp agent-provided result limits to the meeting-list contract.

    The default of 10 keeps vague requests like "show my next meetings" useful
    without flooding the model context; 50 is the hard cap for explicit asks.
    """
    if limit is None:
        return 10
    return max(1, min(limit, 50))


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse tool datetime inputs into UTC-aware values.

    Agents may pass ISO strings or already-parsed datetimes. Naive datetimes are
    treated as UTC because higher-level agent context is responsible for
    resolving user-local phrases like "today at 4pm" before calling this tool.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    return UUID(value)


async def list_meetings(
    *,
    context: AgentContext,
    start_datetime: str | datetime | None = None,
    end_datetime: str | datetime | None = None,
    limit: int | None = 10,
    deal_id: str | None = None,
    attendee_email: str | None = None,
    attendee_name: str | None = None,
    has_transcript: bool | None = None,
    has_summary: bool | None = None,
) -> list[dict[str, Any]]:
    """List meetings from the current user's own connected calendar.

    Security boundary:
    - `organisation_id` and `user_id` come only from `AgentContext`.
    - The query is rooted in the user's active Google/Outlook calendar
      integrations, then `CalendarEventUserState`, so attendee email or deal ID
      cannot be used to pull another user's calendar data.
    """
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not organisation_id:
        logger.warning("list_meetings: missing organisation_id in context")
        return []
    if not user_id:
        logger.warning("list_meetings: missing user_id in context")
        return []

    try:
        start_dt = _parse_datetime(start_datetime) or get_context_anchor_utc(context.metadata)
        end_dt = _parse_datetime(end_datetime)
        parsed_deal_id = _parse_uuid(deal_id)
    except ValueError as exc:
        logger.warning("list_meetings: invalid input: %s", exc)
        return []

    effective_limit = normalize_meeting_limit(limit)

    event_c = cast(Any, CalendarEvent).__table__.c
    user_state_c = cast(Any, CalendarEventUserState).__table__.c
    integration_c = cast(Any, KnowledgeIntegration).__table__.c
    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
    deal_c = cast(Any, CRMDeal).__table__.c
    meeting_c = cast(Any, Meeting).__table__.c

    # Deal association is enrichment by default. When a deal_id is supplied it
    # becomes an additional narrowing filter, but never replaces the user
    # calendar ownership boundary built below.
    assoc_join = and_(
        assoc_c.event_id == event_c.event_id,
        assoc_c.organisation_id == organisation_id,
        assoc_c.status == AssociationStatus.ACTIVE,
    )
    if parsed_deal_id:
        assoc_join = and_(assoc_join, assoc_c.deal_id == parsed_deal_id)

    meeting_join = and_(
        meeting_c.calendar_event_id == event_c.event_id,
        meeting_c.organisation_id == organisation_id,
        meeting_c.is_deleted.is_(False),
    )

    # Start from the user's active calendar integration rather than attendees.
    # Attendee email only proves someone was invited; CalendarEventUserState
    # proves this event was actually synced from the current user's calendar.
    stmt = (
        select(CalendarEvent, CalendarEventDealAssociation, CRMDeal, Meeting)
        .options(selectinload(cast(Any, CalendarEvent).attendees))
        .join(CalendarEventUserState, user_state_c.event_id == event_c.event_id)
        .join(
            KnowledgeIntegration,
            integration_c.integration_id == user_state_c.integration_id,
        )
        .outerjoin(CalendarEventDealAssociation, assoc_join)
        .outerjoin(
            CRMDeal,
            and_(
                deal_c.deal_id == assoc_c.deal_id,
                deal_c.organisation_id == organisation_id,
                deal_c.is_deleted.is_(False),
            ),
        )
        .outerjoin(Meeting, meeting_join)
        .where(
            event_c.organisation_id == organisation_id,
            user_state_c.is_deleted.is_(False),
            integration_c.organisation_id == organisation_id,
            integration_c.created_by_user_id == user_id,
            integration_c.is_active.is_(True),
            integration_c.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
            event_c.start_at >= start_dt if end_dt is None else event_c.start_at < end_dt,
        )
    )

    if end_dt is not None:
        # Bounded windows use overlap semantics so a meeting that started before
        # the window but is still in progress is not hidden from "what is on now?"
        # style questions.
        stmt = stmt.where(event_c.end_at > start_dt)

    if parsed_deal_id:
        stmt = stmt.where(assoc_c.deal_id == parsed_deal_id)

    if attendee_email or attendee_name:
        # Attendee filters are search refinements only. They are intentionally
        # applied after the user-calendar ownership join above.
        attendee_filter = aliased(CalendarAttendee)
        filter_c = cast(Any, attendee_filter).__table__.c
        stmt = stmt.join(attendee_filter, filter_c.event_id == event_c.event_id)
        if attendee_email:
            stmt = stmt.where(func.lower(filter_c.email) == attendee_email.lower())
        if attendee_name:
            stmt = stmt.where(filter_c.display_name.ilike(f"%{attendee_name}%"))

    if has_transcript is True:
        stmt = stmt.where(meeting_c.status == MeetingStatus.READY)
    elif has_transcript is False:
        stmt = stmt.where(
            or_(meeting_c.meeting_id.is_(None), meeting_c.status != MeetingStatus.READY)
        )

    if has_summary is True:
        stmt = stmt.where(assoc_c.meeting_summary_status == GenerationStatus.COMPLETED)
    elif has_summary is False:
        stmt = stmt.where(
            or_(
                assoc_c.association_id.is_(None),
                assoc_c.meeting_summary_status != GenerationStatus.COMPLETED,
            )
        )

    stmt = stmt.order_by(event_c.start_at.asc()).limit(effective_limit)

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            rows = list(result.all())
        except Exception as exc:
            logger.exception("list_meetings: database error: %s", exc)
            return []

    return _format_meeting_rows(rows)


def _format_meeting_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Convert joined meeting rows into the compact structure agents consume."""
    output: list[dict[str, Any]] = []
    seen_event_ids: set[UUID] = set()

    for row in rows:
        event, assoc, deal, meeting = row
        # A calendar event can have multiple active deal associations. Keep one
        # result per calendar event so "next 10 meetings" means meetings, not
        # meeting-deal association rows.
        if event.event_id in seen_event_ids:
            continue
        seen_event_ids.add(event.event_id)

        attendees = [
            {
                "email": attendee.email,
                "name": resolve_attendee_display_name(attendee.display_name, attendee.email),
                "response_status": attendee.response_status,
                "is_organiser": attendee.is_organiser,
            }
            for attendee in (event.attendees or [])[:10]
        ]

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
                "status": event.status,
                "location": event.location,
                "conference_link": event.conference_link,
                "online_meeting_provider": event.online_meeting_provider,
                "organiser_name": resolve_attendee_display_name(
                    event.organiser_name, event.organiser_email
                ),
                "organiser_email": event.organiser_email,
                "attendee_count": event.attendee_count or 0,
                "attendees": attendees,
                "deal": _format_deal(deal) if deal else None,
                "has_transcript": meeting is not None and meeting.status == MeetingStatus.READY,
                "has_summary": assoc is not None
                and assoc.meeting_summary_status == GenerationStatus.COMPLETED,
                "has_prep": assoc is not None
                and assoc.meeting_prep_status == GenerationStatus.COMPLETED,
            }
        )

    return output


def _format_deal(deal: CRMDeal) -> dict[str, Any]:
    """Format optional deal enrichment without leaking CRM provider internals."""
    return {
        "deal_id": str(deal.deal_id),
        "name": deal.name,
        "company_name": deal.company_name,
        "amount": float(deal.amount) if deal.amount else None,
        "currency": deal.currency,
    }


def create_list_meetings(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Create the unified meeting-listing tool bound to immutable agent context.

    The returned callable intentionally accepts time/deal/search filters only;
    organisation and user boundaries remain baked into `AgentContext`.
    """

    async def list_meetings_tool(
        start_datetime: str | None = None,
        end_datetime: str | None = None,
        limit: int = 10,
        deal_id: str | None = None,
        attendee_email: str | None = None,
        attendee_name: str | None = None,
        has_transcript: bool | None = None,
        has_summary: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List meetings from your connected calendar.

        Use this tool for schedule and meeting-list questions. Results are
        restricted to the current user's own connected calendar; `deal_id`,
        attendee, transcript, and summary fields are only narrowing filters.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListMeetings",
            tool_func=list_meetings,
            parameters={
                "context": context,
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
                "limit": limit,
                "deal_id": deal_id,
                "attendee_email": attendee_email,
                "attendee_name": attendee_name,
                "has_transcript": has_transcript,
                "has_summary": has_summary,
            },
            description="Finding meetings from your calendar",
            fallback_value=[],
        )
        return list(result) if result else []

    return list_meetings_tool


__all__ = [
    "create_list_meetings",
    "list_meetings",
    "normalize_meeting_limit",
]
