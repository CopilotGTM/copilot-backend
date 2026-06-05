"""Agent tool for sending a meeting recording bot to a user's calendar event.

Called by chat and realtime agents when a user asks to add the meeting bot to a
call. The tool never dispatches directly from a raw event id: it first proves
the event was synced from the current user's active calendar integration, then
delegates provider idempotency to `MeetingRecordingDispatchService`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.timezone import get_context_anchor_utc
from src.service.meeting_bot.dispatch_service import MeetingRecordingDispatchService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE.value,
    IntegrationType.MICROSOFT.value,
)


async def send_meeting_bot_invite(
    *,
    context: AgentContext,
    event_id: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Send the meeting bot to a user-owned calendar event.

    Security boundary:
    - `organisation_id` and `user_id` come only from `AgentContext`.
    - The event must be visible through the user's own active calendar
      integration and `CalendarEventUserState` before the dispatch service is
      called. This prevents a user from sending a bot to another user's event by
      guessing a calendar event UUID.

    Confirmation boundary:
    - If `event_id` is omitted, the tool infers the next meeting and returns a
      confirmation preview unless `confirmed=True`.
    - If `event_id` is provided, the user has already selected a specific
      meeting, so the tool dispatches after ownership verification.
    """
    if not context.user_id:
        logger.warning("send_meeting_bot_invite: missing user_id in context")
        return {"status": "error", "reason": "missing_user_context"}

    try:
        parsed_event_id = UUID(event_id) if event_id else None
    except ValueError:
        return {"status": "error", "reason": "invalid_event_id"}

    event = await _resolve_user_calendar_event(context=context, event_id=parsed_event_id)
    if event is None:
        return {"status": "not_found", "reason": "meeting_not_found"}

    if parsed_event_id is None and not confirmed:
        return {
            "status": "confirmation_required",
            "event": _format_confirmation_event(event),
            "message": "Confirm before sending the meeting bot to this next meeting.",
        }

    dispatch_service = MeetingRecordingDispatchService(context.organisation_id)
    dispatch_result = await dispatch_service.dispatch_event(event.event_id)
    return {
        **dispatch_result,
        "event": _format_confirmation_event(event),
    }


async def _resolve_user_calendar_event(
    *,
    context: AgentContext,
    event_id: UUID | None,
) -> CalendarEvent | None:
    """Resolve one event through the current user's calendar integration.

    A raw calendar event UUID is not a trust boundary. The joins below prove the
    event belongs to a calendar integration connected by the current user before
    any provider-side bot action can happen.
    """
    event_c = cast(Any, CalendarEvent).__table__.c
    user_state_c = cast(Any, CalendarEventUserState).__table__.c
    integration_c = cast(Any, KnowledgeIntegration).__table__.c

    stmt = (
        select(CalendarEvent)
        .join(CalendarEventUserState, user_state_c.event_id == event_c.event_id)
        .join(
            KnowledgeIntegration,
            integration_c.integration_id == user_state_c.integration_id,
        )
        .where(
            event_c.organisation_id == context.organisation_id,
            user_state_c.is_deleted.is_(False),
            integration_c.organisation_id == context.organisation_id,
            integration_c.created_by_user_id == context.user_id,
            integration_c.is_active.is_(True),
            integration_c.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
        )
    )

    if event_id:
        stmt = stmt.where(event_c.event_id == event_id)
    else:
        # "Next meeting" is inferred from the current agent anchor. The caller
        # must confirm this inferred target before dispatching the bot.
        stmt = stmt.where(event_c.start_at >= get_context_anchor_utc(context.metadata))

    stmt = stmt.order_by(event_c.start_at.asc()).limit(1)

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
        except Exception as exc:
            logger.exception("send_meeting_bot_invite: database error: %s", exc)
            return None

    return result.scalars().first()


def _format_confirmation_event(event: CalendarEvent) -> dict[str, Any]:
    """Return the human-safe event fields needed for confirmation receipts."""
    return {
        "event_id": str(event.event_id),
        "title": event.title,
        "start_at": event.start_at.isoformat() if isinstance(event.start_at, datetime) else None,
        "end_at": event.end_at.isoformat() if isinstance(event.end_at, datetime) else None,
        "conference_link": event.conference_link,
    }


def create_send_meeting_bot_invite(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Create the send-meeting-bot tool bound to immutable agent context."""

    async def send_meeting_bot_invite_tool(
        event_id: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Send a meeting recording bot to a specific or next calendar event.

        Use this only after the user asks for a meeting bot. For "next meeting"
        requests, call once with `confirmed=False` to show the selected meeting,
        then call again with `confirmed=True` after the user confirms.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SendMeetingBotInvite",
            tool_func=send_meeting_bot_invite,
            parameters={
                "context": context,
                "event_id": event_id,
                "confirmed": confirmed,
            },
            description="Sending the meeting bot to a calendar event",
            fallback_value={"status": "error", "reason": "tool_failed"},
        )
        return dict(result) if result else {"status": "error", "reason": "tool_failed"}

    return send_meeting_bot_invite_tool


__all__ = [
    "create_send_meeting_bot_invite",
    "send_meeting_bot_invite",
]
