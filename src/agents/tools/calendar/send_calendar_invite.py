"""Agent tool for sending user-owned Outlook calendar invites."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.calendar_invites.models import (
    CalendarInviteAttendee,
    CalendarInviteCreateRequest,
    CalendarInviteError,
)
from src.service.calendar_invites.service import CalendarInviteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities


def _parse_datetime(value: str, timezone_name: str) -> datetime | None:
    # Parse with timezone-aware behavior first, then fall back to natural-language
    # parsing. This keeps strict ISO inputs fast and lets users pass friendly
    # inputs from chat without breaking invite creation.
    from zoneinfo import ZoneInfo

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            dateparser: Any = import_module("dateparser")
            parsed = dateparser.parse(
                value,
                settings={
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "TIMEZONE": timezone_name,
                    "TO_TIMEZONE": "UTC",
                },
            )
        except Exception:
            return None

    if not parsed:
        return None
    if parsed.tzinfo is None:
        try:
            # Localize naïve values to sender timezone so downstream comparisons
            # and Graph conversion are done against an explicit offset.
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except (KeyError, ValueError):
            parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_attendees(
    attendees: list[str] | list[dict[str, Any]],
) -> list[CalendarInviteAttendee]:
    # Normalize mixed list shapes from agent callers into a strict model list.
    # Some tool clients send plain email strings while others send display
    # metadata; both forms represent the same attendee intent.
    normalized: list[CalendarInviteAttendee] = []
    for attendee in attendees:
        if isinstance(attendee, str):
            normalized.append(CalendarInviteAttendee(email=attendee))
            continue
        if isinstance(attendee, dict):
            normalized.append(
                CalendarInviteAttendee(
                    email=str(attendee.get("email", "")),
                    name=attendee.get("name"),
                    optional=bool(attendee.get("optional", False)),
                )
            )
    return normalized


async def send_calendar_invite(
    *,
    context: AgentContext,
    title: str,
    start_time: str,
    end_time: str,
    attendees: list[str] | list[dict[str, Any]],
    body: str | None = None,
    location: str | None = None,
    deal_id: str | None = None,
    timezone: str | None = None,
    sender_email: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build and validate a request, then delegate invite creation to service."""

    # The tool layer only normalizes inputs and validates the authenticated user.
    # All side-effect work (scope checks, provider writes, writer sync) stays in
    # CalendarInviteService where retry and transactional ordering are handled.
    # Tool layer guardrail: do not call downstream service unless we can tie the
    # request to an authenticated agent user and parse both bounds.
    user_id = context.user_id
    if not user_id:
        return {"status": "error", "message": "Cannot send invite: user not identified."}

    timezone_name = timezone or context.metadata.get("user_timezone") or "UTC"
    start_at = _parse_datetime(start_time, timezone_name)
    end_at = _parse_datetime(end_time, timezone_name)
    if not start_at or not end_at:
        return {
            "status": "error",
            "message": "Could not understand the meeting start or end time.",
        }
    if end_at <= start_at:
        return {"status": "error", "message": "Meeting end time must be after start time."}

    try:
        # Build and validate the DTO before touching DB/session state.
        # This catches malformed user input early and keeps bad tool calls from
        # creating partial invite audit rows.
        request = CalendarInviteCreateRequest(
            organisation_id=context.organisation_id,
            requested_by_user_id=user_id,
            title=title,
            body=body,
            location=location,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone_name,
            attendees=_normalize_attendees(attendees),
            deal_id=deal_id,
            sender_email=sender_email,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    async with context.acquire_session() as session:
        # Keep DB session scoped only around local writes/reads.
        # All external Graph work is performed inside CalendarInviteService, which
        # performs its own scoped writes and status updates.
        service = CalendarInviteService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=user_id,
        )
        try:
            result = await service.create_invite(request)
        except CalendarInviteError as exc:
            return {"status": "error", "message": exc.message, "code": exc.code}

    return {
        "status": "sent",
        "message": "Calendar invite sent.",
        "provider_event_id": result.provider_event_id,
        "event_id": str(result.event_id) if result.event_id else None,
        "conference_link": result.conference_link,
    }


def create_send_calendar_invite(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the send_calendar_invite tool bound to a context."""

    async def send_calendar_invite_tool(
        title: str,
        start_time: str,
        end_time: str,
        attendees: list[str] | list[dict[str, Any]],
        body: str | None = None,
        location: str | None = None,
        deal_id: str | None = None,
        timezone: str | None = None,
        sender_email: str | None = None,
    ) -> dict[str, Any]:
        """Send a calendar invite with a Teams meeting link.

        Use when the user asks to schedule a meeting, create a calendar invite,
        or send an invite. This creates a real Outlook calendar event from the
        connected user account and sends attendee invites. If the user has
        multiple connected Outlook Calendar accounts, ask which sender email to
        use and pass it as sender_email. Provide concrete start and end times;
        ask a follow-up if either time or attendee email is missing.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SendCalendarInvite",
            tool_func=send_calendar_invite,
            parameters={
                "context": context,
                "title": title,
                "start_time": start_time,
                "end_time": end_time,
                "attendees": attendees,
                "body": body,
                "location": location,
                "deal_id": deal_id,
                "timezone": timezone,
                "sender_email": sender_email,
            },
            description="Sending calendar invite",
            fallback_value={"status": "error", "message": "Failed to send calendar invite."},
        )
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return send_calendar_invite_tool
