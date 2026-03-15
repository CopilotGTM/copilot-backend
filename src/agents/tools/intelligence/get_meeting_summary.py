"""Get Meeting Summary Tool - Retrieve AI-generated meeting summary.

This tool fetches the AI-generated meeting summary for a specific meeting.
The summary is stored in CalendarEventDealAssociation.meeting_summary_data.

USE CASE:
When a user asks about what was discussed in a meeting, action items,
decisions made, or key topics from a past meeting.

RETURNS:
- status: Generation status (NOT_STARTED, PENDING, IN_PROGRESS, COMPLETED, FAILED)
- data: The meeting summary JSON when status is COMPLETED, containing:
  - executive_summary: Brief overview of the meeting
  - key_topics: Main topics discussed
  - action_items: Tasks and follow-ups identified
  - decisions: Decisions made during the meeting
  - And more fields depending on the summary schema

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. organisation_id comes from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_meeting_summary(
    event_id: str,
    *,
    context: AgentContext,
) -> dict[str, Any] | None:
    """Get the AI-generated summary for a meeting.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        event_id: The calendar event UUID
        context: AgentContext containing organisation metadata

    Returns:
        Dictionary with status and summary data, or None if not found.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_meeting_summary: missing organisation_id in context")
        return None

    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        logger.warning("get_meeting_summary: invalid event_id format: %s", event_id)
        return None

    event_c = cast(Any, CalendarEvent).__table__.c
    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c

    # First get the event to verify it belongs to this org
    event_stmt = select(CalendarEvent).where(
        event_c.organisation_id == organisation_id,
        event_c.event_id == parsed_event_id,
    )

    async with context.acquire_session() as session:
        try:
            event_result = await session.execute(event_stmt)
            event = event_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_summary: error fetching event: {e}")
            return None

        if not event:
            logger.info("get_meeting_summary: event not found: %s", event_id)
            return None

        # Get the deal association which contains the summary.
        # A meeting can be associated with multiple deals, so scope by
        # deal_id when available; otherwise prefer the one with a completed summary.
        assoc_stmt = select(CalendarEventDealAssociation).where(
            assoc_c.event_id == parsed_event_id,
        )

        if context.deal_id:
            assoc_stmt = assoc_stmt.where(assoc_c.deal_id == context.deal_id)

        assoc_stmt = assoc_stmt.order_by(assoc_c.meeting_summary_status.asc()).limit(1)

        try:
            assoc_result = await session.execute(assoc_stmt)
            association = assoc_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_summary: error fetching association: {e}")
            return None

        if not association:
            logger.info("get_meeting_summary: no deal association for event: %s", event_id)
            return {
                "event_id": event_id,
                "title": event.title,
                "status": "NOT_AVAILABLE",
                "message": "This meeting is not associated with a deal",
                "data": None,
            }

        # Build response
        status = (
            association.meeting_summary_status.value
            if isinstance(association.meeting_summary_status, GenerationStatus)
            else association.meeting_summary_status
        )

        output: dict[str, Any] = {
            "event_id": event_id,
            "title": event.title,
            "start_at": event.start_at.isoformat() if event.start_at else None,
            "status": status,
            "data": None,
        }

        if association.meeting_summary_status == GenerationStatus.COMPLETED:
            output["data"] = association.meeting_summary_data
        elif association.meeting_summary_status == GenerationStatus.FAILED:
            output["message"] = "Summary generation failed"
        elif association.meeting_summary_status == GenerationStatus.IN_PROGRESS:
            output["message"] = "Summary is being generated"
        elif association.meeting_summary_status == GenerationStatus.PENDING:
            output["message"] = "Summary generation is pending"
        elif association.meeting_summary_status == GenerationStatus.NOT_STARTED:
            output["message"] = "Summary has not been generated yet"

    logger.info("get_meeting_summary: found summary for event %s (status: %s)", event_id, status)
    return output


def create_get_meeting_summary(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_meeting_summary tool bound to a context.

    SECURITY: organisation_id comes from context (immutable).
    """

    async def get_meeting_summary_tool(
        event_id: str,
    ) -> dict[str, Any] | None:
        """Get the AI-generated summary for a meeting.

        Use this tool to retrieve what was discussed in a meeting,
        including key topics, action items, and decisions.

        Args:
            event_id: The meeting/calendar event UUID

        Returns:
        - status: COMPLETED, PENDING, IN_PROGRESS, NOT_STARTED, or FAILED
        - data: The summary content when status is COMPLETED, containing:
          - executive_summary: Brief meeting overview
          - key_topics: Main topics discussed
          - action_items: Tasks identified
          - decisions: Decisions made
        """
        logger.info("[TOOL CALLED] get_meeting_summary - event_id='%s'", event_id)

        if not context.organisation_id:
            logger.warning("get_meeting_summary unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetMeetingSummary",
            tool_func=get_meeting_summary,
            parameters={
                "event_id": event_id,
                "context": context,
            },
            description="Reviewing meeting summary",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] get_meeting_summary - status: %s",
                result.get("status", "unknown"),
            )
        else:
            logger.info("[TOOL RESULT] get_meeting_summary returned None")

        return result

    return get_meeting_summary_tool


__all__ = [
    "get_meeting_summary",
    "create_get_meeting_summary",
]
