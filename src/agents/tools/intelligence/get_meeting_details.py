"""Get meeting details tool for agents.

Tool for retrieving detailed information about a specific meeting,
including attendees, transcript status, and AI-generated insights.

SECURITY: organisation_id and user_id come from immutable context. An optional
deal_id is authorized through the shared CRM deal policy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import (
    AgentCRMAccessError,
    require_agent_deal_access,
    resolve_agent_deal_id,
)
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
)
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name
from src.service.crm.utils.internal_domains import get_internal_domains, is_internal_email
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES as CALENDAR_INTEGRATION_TYPES,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_meeting_details(
    event_id: str,
    *,
    context: AgentContext,
    deal_id: str | None = None,
    include_transcript: bool = False,
    include_summary: bool = True,
    include_prep: bool = False,
    include_followup: bool = True,
) -> dict[str, Any] | None:
    """Get detailed information about a specific meeting.

    Returns comprehensive meeting details including attendees, AI-generated
    summary, prep notes, and follow-up actions.

    SECURITY: interactive reads must come through the current user's active
    calendar. Userless background briefing reads must come through the immutable
    deal association in context; an event UUID alone never grants access.

    Args:
        event_id: The calendar event UUID as a string
        context: AgentContext containing organisation metadata
        deal_id: Optional visible deal UUID; defaults to context deal
        include_transcript: Include full transcript text (default: False, can be large)
        include_summary: Include AI-generated meeting summary (default: True)
        include_prep: Include meeting prep notes (default: False)
        include_followup: Include follow-up actions (default: True)

    Returns:
        Meeting dictionary with detailed information, or None if not found
    """
    organisation_id = context.organisation_id
    user_id = context.user_id
    try:
        effective_deal_id = resolve_agent_deal_id(context, deal_id)
    except AgentCRMAccessError:
        return None

    if not organisation_id:
        logger.warning("get_meeting_details: missing organisation_id in context")
        return None
    if not user_id and not effective_deal_id:
        logger.warning("get_meeting_details: missing user or deal access boundary")
        return None

    # Parse event_id
    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        logger.warning(f"get_meeting_details: invalid event_id format: {event_id}")
        return None

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    event_c = cast(Any, CalendarEvent).__table__.c
    user_state_c = cast(Any, CalendarEventUserState).__table__.c
    integration_c = cast(Any, KnowledgeIntegration).__table__.c
    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
    meeting_c = cast(Any, Meeting).__table__.c

    # Internal domains live in the master schema. Load them before acquiring the
    # org session so the cross-schema lookup cannot hold two pooled sessions.
    internal_domains = await get_internal_domains(organisation_id)

    async with context.acquire_session() as session:
        if effective_deal_id is not None:
            try:
                await require_agent_deal_access(context, session, effective_deal_id)
            except AgentCRMAccessError:
                return None
        event_stmt = select(CalendarEvent).options(selectinload(cast(Any, CalendarEvent).attendees))
        event_access_filters: tuple[Any, ...]
        if user_id and effective_deal_id is None:
            # The provider event ID is externally supplied, so current-user
            # calendar ownership is the interactive-read authorization boundary.
            event_stmt = event_stmt.join(
                CalendarEventUserState, user_state_c.event_id == event_c.event_id
            ).join(
                KnowledgeIntegration,
                integration_c.integration_id == user_state_c.integration_id,
            )
            event_access_filters = (
                user_state_c.is_deleted.is_(False),
                integration_c.organisation_id == organisation_id,
                integration_c.created_by_user_id == user_id,
                integration_c.is_active.is_(True),
                integration_c.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
            )
        else:
            # Any deal-scoped read must follow an active association. This keeps
            # context fallback equivalent to passing the same deal explicitly.
            event_stmt = event_stmt.join(
                CalendarEventDealAssociation,
                assoc_c.event_id == event_c.event_id,
            )
            event_access_filters = (
                assoc_c.organisation_id == organisation_id,
                assoc_c.deal_id == effective_deal_id,
                assoc_c.status == AssociationStatus.ACTIVE,
            )
        event_stmt = event_stmt.where(
            event_c.event_id == parsed_event_id,
            event_c.organisation_id == organisation_id,
            *event_access_filters,
        ).limit(1)

        try:
            event_result = await session.execute(event_stmt)
            event = event_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_details: error fetching event: {e}")
            return None

        if not event:
            logger.info(f"get_meeting_details: event not found: {event_id}")
            return None

        # Fetch deal association (for AI insights)
        assoc_stmt = select(CalendarEventDealAssociation).where(
            assoc_c.organisation_id == organisation_id,
            assoc_c.event_id == parsed_event_id,
            assoc_c.status == AssociationStatus.ACTIVE,
        )
        if effective_deal_id:
            assoc_stmt = assoc_stmt.where(assoc_c.deal_id == effective_deal_id)

        try:
            assoc_result = await session.execute(assoc_stmt)
            association = assoc_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_details: error fetching association: {e}")
            association = None

        # Fetch meeting record (for transcript)
        meeting_stmt = select(Meeting).where(
            meeting_c.organisation_id == organisation_id,
            meeting_c.calendar_event_id == parsed_event_id,
            meeting_c.is_deleted.is_(False),
        )

        try:
            meeting_result = await session.execute(meeting_stmt)
            meeting = meeting_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_details: error fetching meeting: {e}")
            meeting = None

        # Build attendee list (while session is active for relationship access)
        attendees = []
        for att in event.attendees:
            # An empty domain set cannot prove that an invitee is external. Keep
            # the classification unknown so downstream research fails closed.
            attendee_is_internal = (
                is_internal_email(att.email, internal_domains) if internal_domains else None
            )
            attendees.append(
                {
                    "email": att.email,
                    "name": resolve_attendee_display_name(att.display_name, att.email),
                    "response_status": att.response_status,
                    "is_organiser": att.is_organiser,
                    "is_internal": attendee_is_internal,
                }
            )

        # Calculate duration
        duration_minutes = None
        if event.start_at and event.end_at:
            duration_minutes = int((event.end_at - event.start_at).total_seconds() / 60)

        # Build output (while session is active)
        output: dict[str, Any] = {
            "association_id": str(association.association_id) if association else None,
            "source_record_id": str(association.association_id) if association else None,
            "event_id": str(event.event_id),
            "title": event.title,
            "description": event.description,
            "start_at": event.start_at.isoformat() if event.start_at else None,
            "end_at": event.end_at.isoformat() if event.end_at else None,
            "duration_minutes": duration_minutes,
            "organiser": {
                "name": resolve_attendee_display_name(event.organiser_name, event.organiser_email),
                "email": event.organiser_email,
            },
            "attendees": attendees,
            "attendee_count": event.attendee_count,
            "location": event.location,
            "conference_link": event.conference_link,
            "status": event.status,
            "is_all_day": event.is_all_day,
        }

        # Transcript information
        output["transcript"] = {
            "available": meeting is not None and meeting.status == MeetingStatus.READY,
            "status": meeting.status.value
            if meeting and isinstance(meeting.status, MeetingStatus)
            else (meeting.status if meeting else None),
            "source": meeting.source.value
            if meeting and isinstance(meeting.source, MeetingSource)
            else (meeting.source if meeting else None),
            "language": meeting.transcript_language if meeting else None,
            "duration_seconds": meeting.duration_seconds if meeting else None,
        }

        # Include full transcript if requested and available
        if include_transcript and meeting and meeting.full_transcript:
            output["transcript"]["full_text"] = meeting.full_transcript

        # AI-generated insights from association
        if association:
            output["association_status"] = (
                association.status.value
                if isinstance(association.status, AssociationStatus)
                else association.status
            )
            output["match_score"] = association.match_score

            if include_summary:
                output["summary"] = {
                    "status": association.meeting_summary_status.value
                    if isinstance(association.meeting_summary_status, GenerationStatus)
                    else association.meeting_summary_status,
                    "data": association.meeting_summary_data
                    if association.meeting_summary_status == GenerationStatus.COMPLETED
                    else None,
                }

            if include_prep:
                output["meeting_prep"] = {
                    "status": association.meeting_prep_status.value
                    if isinstance(association.meeting_prep_status, GenerationStatus)
                    else association.meeting_prep_status,
                    "data": association.meeting_prep_data
                    if association.meeting_prep_status == GenerationStatus.COMPLETED
                    else None,
                }

            if include_followup:
                output["followup"] = {
                    "status": association.meeting_followup_status.value
                    if isinstance(association.meeting_followup_status, GenerationStatus)
                    else association.meeting_followup_status,
                    "data": association.meeting_followup_data
                    if association.meeting_followup_status == GenerationStatus.COMPLETED
                    else None,
                }
        else:
            output["association_status"] = None
            output["match_score"] = None
            if include_summary:
                output["summary"] = None
            if include_prep:
                output["meeting_prep"] = None
            if include_followup:
                output["followup"] = None
    # Session released

    logger.info(f"get_meeting_details: found meeting {event_id}")
    return output


def create_get_meeting_details(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_meeting_details tool bound to a context.

    SECURITY: Context supplies immutable organisation/user boundaries; an
    optional deal selection is checked by the shared CRM access policy.

    Args:
        context: AgentContext with organisation metadata
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def get_meeting_details_tool(
        event_id: str,
        deal_id: str | None = None,
        include_transcript: bool = False,
        include_summary: bool = True,
        include_prep: bool = False,
        include_followup: bool = True,
    ) -> dict[str, Any] | None:
        """Get detailed information about a specific meeting.

        Use this tool when you need comprehensive information about a meeting,
        including who was invited, what was discussed (summary), and any follow-up
        actions. This is useful for:
        - Reviewing what happened in a customer meeting
        - Preparing for a follow-up meeting
        - Understanding action items from past meetings

        Args:
            event_id: The calendar event UUID (from list_deal_meetings)
            deal_id: Visible deal UUID. If omitted, uses current deal context.
            include_transcript: Include full transcript text (default: False, can be large)
            include_summary: Include AI meeting summary (default: True)
            include_prep: Include meeting prep notes (default: False)
            include_followup: Include follow-up actions (default: True)

        The returned people are calendar invitees with RSVP states; they are not
        proof of actual attendance. `is_internal=null` means the organisation
        has no reliable domain classification; never enrich those invitees.
        """
        logger.info(
            f"[TOOL CALLED] get_meeting_details - event_id='{event_id}', "
            f"include_transcript={include_transcript}, include_summary={include_summary}"
        )

        if not context.organisation_id or not (context.user_id or context.deal_id or deal_id):
            logger.warning("get_meeting_details unavailable: missing event access context")
            return None

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetMeetingDetails",
            tool_func=get_meeting_details,
            parameters={
                "event_id": event_id,
                "context": context,
                "deal_id": deal_id,
                "include_transcript": include_transcript,
                "include_summary": include_summary,
                "include_prep": include_prep,
                "include_followup": include_followup,
            },
            description="Looking up meeting details",
            fallback_value=None,
        )

        if result:
            logger.info(
                f"[TOOL RESULT] get_meeting_details found meeting: {result.get('title', 'Unknown')}"
            )
        else:
            logger.info("[TOOL RESULT] get_meeting_details returned None (not found)")

        return dict(result) if result else None

    return get_meeting_details_tool
