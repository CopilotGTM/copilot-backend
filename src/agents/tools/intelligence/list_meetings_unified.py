"""Unified current-user calendar listing tool for agents.

Called by the General and realtime voice agents when a user asks about their
schedule or meeting history. This module owns the current-user calendar
boundary: results must come through the user's active calendar integration and
`CalendarEventUserState`, while deal rows are optional enrichment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import selectinload

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
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import get_context_anchor_local, get_context_anchor_utc
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES as CALENDAR_INTEGRATION_TYPES,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

MeetingTimeDirection = Literal["past", "future", "present"]


def normalize_meeting_limit(limit: int | None) -> int:
    """Clamp agent-provided result limits to the meeting-list contract.

    The default of 10 keeps vague requests like "show my next meetings" useful
    without flooding the model context; 50 is the hard cap for explicit asks.
    """
    if limit is None:
        return 10
    return max(1, min(limit, 50))


def _parse_datetime(
    value: str | datetime | None,
    metadata: dict[str, Any] | None = None,
) -> datetime | None:
    """Parse tool datetime inputs into UTC-aware values.

    Agents may pass ISO strings or already-parsed datetimes. Naive datetimes are
    treated as user-local when timezone metadata is available.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            timezone_name = (metadata or {}).get("user_timezone")
            return parse_agent_datetime(
                value,
                timezone_name=timezone_name if isinstance(timezone_name, str) else None,
                metadata=metadata,
            )
        return value.astimezone(UTC)

    timezone_name = (metadata or {}).get("user_timezone")
    return parse_agent_datetime(
        value,
        timezone_name=timezone_name if isinstance(timezone_name, str) else None,
        metadata=metadata,
    )


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    return UUID(value)


def _normalize_time_direction(value: str | None) -> MeetingTimeDirection | None:
    if value in {"past", "future", "present"}:
        return cast(MeetingTimeDirection, value)
    return None


def _meeting_search_expressions(event_c: Any, query: str) -> tuple[Any, Any, Any, Any]:
    """Build deterministic and trigram fallback expressions for meeting identity.

    This mirrors agent deal search: direct identity matches remain authoritative,
    while PostgreSQL trigram similarity recovers speech transcription and spelling
    variations only when the direct query returns no meetings.
    """
    attendee_c = cast(Any, CalendarAttendee).__table__.c
    lowered_query = query.lower()

    title = func.lower(func.coalesce(event_c.title, ""))
    organiser_name = func.lower(func.coalesce(event_c.organiser_name, ""))
    organiser_email = func.lower(func.coalesce(event_c.organiser_email, ""))

    def attendee_exists(condition: Any) -> Any:
        """Correlate one attendee identity condition to the candidate event."""
        return (
            select(1)
            .where(attendee_c.event_id == event_c.event_id, condition)
            .correlate(CalendarEvent)
            .exists()
        )

    attendee_name = func.lower(func.coalesce(attendee_c.display_name, ""))
    attendee_email = func.lower(func.coalesce(attendee_c.email, ""))
    exact_attendee = attendee_exists(
        or_(attendee_name == lowered_query, attendee_email == lowered_query)
    )
    prefix_attendee = attendee_exists(
        or_(
            attendee_name.like(f"{lowered_query}%"),
            attendee_email.like(f"{lowered_query}%"),
        )
    )
    contains_attendee = attendee_exists(
        or_(
            attendee_name.like(f"%{lowered_query}%"),
            attendee_email.like(f"%{lowered_query}%"),
        )
    )

    deterministic_checks = (
        (title == lowered_query, 6.0),
        (organiser_name == lowered_query, 5.5),
        (organiser_email == lowered_query, 5.0),
        (exact_attendee, 4.5),
        (title.like(f"{lowered_query}%"), 4.0),
        (organiser_name.like(f"{lowered_query}%"), 3.8),
        (organiser_email.like(f"{lowered_query}%"), 3.6),
        (prefix_attendee, 3.4),
        (title.like(f"%{lowered_query}%"), 3.0),
        (organiser_name.like(f"%{lowered_query}%"), 2.8),
        (organiser_email.like(f"%{lowered_query}%"), 2.6),
        (contains_attendee, 2.4),
    )
    deterministic_filter = or_(*(condition for condition, _score in deterministic_checks))
    deterministic_score = case(*deterministic_checks, else_=0.0)

    attendee_fuzzy_filter = attendee_exists(
        or_(attendee_c.display_name.op("%")(query), attendee_c.email.op("%")(query))
    )
    attendee_fuzzy_score = (
        select(
            func.max(
                func.greatest(
                    func.similarity(func.coalesce(attendee_c.display_name, ""), query),
                    func.similarity(func.coalesce(attendee_c.email, ""), query),
                )
            )
        )
        .where(attendee_c.event_id == event_c.event_id)
        .correlate(CalendarEvent)
        .scalar_subquery()
    )
    fuzzy_filter = or_(
        event_c.title.op("%")(query),
        event_c.organiser_name.op("%")(query),
        event_c.organiser_email.op("%")(query),
        attendee_fuzzy_filter,
    )
    fuzzy_score = func.greatest(
        func.similarity(func.coalesce(event_c.title, ""), query),
        func.similarity(func.coalesce(event_c.organiser_name, ""), query),
        func.similarity(func.coalesce(event_c.organiser_email, ""), query),
        func.coalesce(attendee_fuzzy_score, 0.0),
    )
    return deterministic_filter, deterministic_score, fuzzy_filter, fuzzy_score


def _resolve_meeting_match(
    event: CalendarEvent,
    query: str,
    score: float | None,
    *,
    fuzzy: bool,
) -> dict[str, Any]:
    """Describe which meeting identity best explains a search result."""
    candidates: list[tuple[str, str]] = []
    for field, value in (
        ("title", event.title),
        ("organiser_name", event.organiser_name),
        ("organiser_email", event.organiser_email),
    ):
        if isinstance(value, str) and value.strip():
            candidates.append((field, value))
    for attendee in event.attendees or []:
        if attendee.display_name and attendee.display_name.strip():
            candidates.append(("attendee_name", attendee.display_name))
        if attendee.email and attendee.email.strip():
            candidates.append(("attendee_email", attendee.email))

    normalized_query = query.strip().casefold()
    if fuzzy:
        field, value = max(
            candidates,
            key=lambda candidate: SequenceMatcher(
                None, normalized_query, candidate[1].casefold()
            ).ratio(),
        )
        match_type = "fuzzy"
    else:
        field, value, match_type = next(
            (
                (field, value, match_type)
                for match_type, predicate in (
                    ("exact", lambda text: text == normalized_query),
                    ("prefix", lambda text: text.startswith(normalized_query)),
                    ("substring", lambda text: normalized_query in text),
                )
                for field, value in candidates
                if predicate(value.casefold())
            ),
            ("title", event.title or "", "substring"),
        )

    return {
        "field": field,
        "value": value,
        "type": match_type,
        "score": float(score or 0.0),
    }


async def list_meetings(
    *,
    context: AgentContext,
    start_datetime: str | datetime | None = None,
    end_datetime: str | datetime | None = None,
    limit: int | None = 10,
    deal_id: str | None = None,
    attendee_email: str | None = None,
    attendee_name: str | None = None,
    query: str | None = None,
    has_transcript: bool | None = None,
    has_summary: bool | None = None,
    time_direction: MeetingTimeDirection | None = None,
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
        anchor_dt = get_context_anchor_utc(context.metadata)
        start_dt = _parse_datetime(start_datetime, context.metadata) or anchor_dt
        end_dt = _parse_datetime(end_datetime, context.metadata)
        parsed_deal_id = _parse_uuid(deal_id)
    except ValueError as exc:
        logger.warning("list_meetings: invalid input: %s", exc)
        return []

    metadata_direction = _normalize_time_direction(
        str((context.metadata or {}).get("temporal_direction") or "")
    )
    effective_direction = time_direction or metadata_direction
    metadata_limit = (context.metadata or {}).get("temporal_limit")
    effective_limit = normalize_meeting_limit(limit)
    if limit == 10 and isinstance(metadata_limit, int):
        effective_limit = normalize_meeting_limit(metadata_limit)
    search_query = query.strip() if query and query.strip() else None

    explicit_start = start_datetime is not None
    explicit_end = end_datetime is not None
    use_past_window = effective_direction == "past" and not explicit_end
    use_present_window = (
        effective_direction == "present" and not explicit_start and not explicit_end
    )
    if use_present_window:
        local_anchor = get_context_anchor_local(context.metadata)
        local_start = local_anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = local_start.astimezone(UTC)
        end_dt = (local_start + timedelta(days=1)).astimezone(UTC)

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
        )
    )

    if use_past_window:
        if explicit_start and start_dt != anchor_dt:
            stmt = stmt.where(event_c.start_at >= start_dt)
        stmt = stmt.where(event_c.start_at < anchor_dt)
    elif use_present_window:
        stmt = stmt.where(event_c.start_at < end_dt)
    elif end_dt is None:
        stmt = stmt.where(event_c.start_at >= start_dt)
    else:
        stmt = stmt.where(event_c.start_at < end_dt)

    if end_dt is not None:
        # Bounded windows use overlap semantics so a meeting that started before
        # the window but is still in progress is not hidden from "what is on now?"
        # style questions.
        stmt = stmt.where(event_c.end_at > start_dt)

    if parsed_deal_id:
        stmt = stmt.where(assoc_c.deal_id == parsed_deal_id)

    if attendee_email or attendee_name:
        # Use an attendee subquery rather than a join so a meeting with several
        # matching invitees still occupies only one result slot.
        filter_c = cast(Any, CalendarAttendee).__table__.c
        attendee_stmt = select(filter_c.event_id).where(filter_c.event_id == event_c.event_id)
        if attendee_email:
            attendee_stmt = attendee_stmt.where(
                func.lower(filter_c.email) == attendee_email.lower()
            )
        if attendee_name:
            attendee_stmt = attendee_stmt.where(filter_c.display_name.ilike(f"%{attendee_name}%"))
        stmt = stmt.where(attendee_stmt.correlate(CalendarEvent).exists())

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

    time_order = event_c.start_at.desc() if use_past_window else event_c.start_at.asc()

    async with context.acquire_session() as session:
        try:
            fuzzy_match = False
            if search_query:
                (
                    deterministic_filter,
                    deterministic_score,
                    fuzzy_filter,
                    fuzzy_score,
                ) = _meeting_search_expressions(event_c, search_query)
                deterministic_stmt = (
                    stmt.where(deterministic_filter)
                    .add_columns(deterministic_score.label("search_score"))
                    .order_by(deterministic_score.desc(), time_order)
                    .limit(effective_limit)
                )
                result = await session.execute(deterministic_stmt)
                rows = list(result.all())

                # Fuzzy matches are recovery candidates only. Running this query
                # after an empty direct result keeps a named meeting authoritative.
                if not rows:
                    fuzzy_match = True
                    fuzzy_stmt = (
                        stmt.where(fuzzy_filter)
                        .add_columns(fuzzy_score.label("search_score"))
                        .order_by(fuzzy_score.desc(), time_order)
                        .limit(effective_limit)
                    )
                    result = await session.execute(fuzzy_stmt)
                    rows = list(result.all())
            else:
                result = await session.execute(stmt.order_by(time_order).limit(effective_limit))
                rows = list(result.all())
        except Exception as exc:
            logger.exception("list_meetings: database error: %s", exc)
            return []

    return _format_meeting_rows(
        rows,
        query=search_query,
        fuzzy_match=fuzzy_match if search_query else False,
    )


def _format_meeting_rows(
    rows: list[Any],
    *,
    query: str | None = None,
    fuzzy_match: bool = False,
) -> list[dict[str, Any]]:
    """Convert joined meeting rows into the compact structure agents consume."""
    output: list[dict[str, Any]] = []
    seen_event_ids: set[UUID] = set()

    for row in rows:
        event, assoc, deal, meeting = row[:4]
        search_score = row[4] if query and len(row) > 4 else None
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

        formatted = {
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
        if query:
            formatted["match"] = _resolve_meeting_match(
                event,
                query,
                search_score,
                fuzzy=fuzzy_match,
            )
        output.append(formatted)

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
        query: str | None = None,
        has_transcript: bool | None = None,
        has_summary: bool | None = None,
        time_direction: str | None = None,
    ) -> list[dict[str, Any]]:
        """List meetings from your connected calendar.

        Use this tool for schedule and meeting-list questions. Results are
        restricted to the current user's own connected calendar; `deal_id`,
        attendee, transcript, and summary fields are only narrowing filters.
        Use `time_direction="past"` for prior/history/recent-past requests and
        `time_direction="future"` for upcoming/next/scheduled requests when no
        explicit datetime range is supplied.

        Pass only the identifying title, person, company, or email in `query`;
        do not pass the user's full request. Combine it with date filters when
        the user supplies both, such as Monday plus a participant name.
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
                "query": query,
                "has_transcript": has_transcript,
                "has_summary": has_summary,
                "time_direction": _normalize_time_direction(time_direction),
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
