"""Agent tools for organizer-owned calendar meeting updates and cancellation.

The internal event ID comes from model-visible calendar reads, but it is never
trusted by itself. The mutation service joins it to the current user's active
calendar integration and mailbox-specific provider ID before any provider call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import resolve_timezone_name
from src.service.calendar_invites.attendee_resolver import CalendarInviteAttendeeResolver
from src.service.calendar_invites.models import (
    CalendarInviteAttendee,
    CalendarInviteError,
    CalendarMeetingUpdateRequest,
)
from src.service.calendar_invites.mutations import CalendarMeetingMutationService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities


def _meeting_payload(target, request: CalendarMeetingUpdateRequest) -> dict[str, Any]:
    return {
        "event_id": str(request.event_id),
        "sender_email": target.integration.integration_email,
        "title": request.title,
        "start_time": request.start_at.isoformat(),
        "end_time": request.end_at.isoformat(),
        "timezone": request.timezone,
        "attendees": [item.model_dump() for item in request.attendees],
        "body": request.body,
        "location": request.location,
        "expected_version": request.expected_version,
    }


async def update_calendar_meeting(
    *,
    context: AgentContext,
    event_id: str,
    title: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    attendees: list[str] | list[dict[str, Any]] | None = None,
    body: str | None = None,
    location: str | None = None,
    timezone: str | None = None,
    sender_email: str | None = None,
    expected_version: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Preview or update one non-recurring meeting owned by the current user."""

    if not context.user_id:
        return {"status": "error", "message": "Cannot update meeting: user not identified."}
    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        return {"status": "error", "code": "invalid_event_id", "message": "Invalid event ID."}

    async with context.acquire_session() as session:
        service = CalendarMeetingMutationService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
        )
        try:
            target = await service.resolve_target(parsed_event_id, sender_email=sender_email)
        except CalendarInviteError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}

        timezone_name = resolve_timezone_name(
            timezone or target.event.timezone or (context.metadata or {}).get("user_timezone"),
            default="UTC",
        )
        start_at = target.event.start_at
        end_at = target.event.end_at
        if start_time:
            start_at = parse_agent_datetime(
                start_time,
                timezone_name=timezone_name,
                metadata=context.metadata,
                require_time=True,
            )
        if end_time:
            end_at = parse_agent_datetime(
                end_time,
                timezone_name=timezone_name,
                metadata=context.metadata,
                require_time=True,
            )
        if start_at is None or end_at is None:
            return {
                "status": "needs_clarification",
                "code": "calendar_meeting_time_required",
                "message": "I could not understand the new meeting time.",
            }

        if attendees is None:
            desired_attendees = [
                CalendarInviteAttendee(
                    email=item.email,
                    name=item.display_name,
                    optional=bool((item.data_payload or {}).get("optional", False)),
                )
                for item in target.event.attendees
                if not item.is_organiser
            ]
        else:
            resolution = await CalendarInviteAttendeeResolver(
                session,
                organisation_id=context.organisation_id,
            ).resolve(attendees)
            if resolution.has_blockers:
                return {
                    "status": "needs_clarification",
                    "code": "calendar_attendees_need_clarification",
                    "message": "I need exact attendee emails before changing this meeting.",
                    "resolved_attendees": resolution.public_resolved(),
                    "ambiguous_attendees": resolution.ambiguous,
                    "missing_attendees": resolution.missing,
                }
            desired_attendees = resolution.service_attendees()

        current_version = (
            target.event.source_modified_at.isoformat() if target.event.source_modified_at else None
        )
        try:
            provider_body_payload = (
                target.event.data_payload.get("body")
                if isinstance(target.event.data_payload, dict)
                else None
            )
            provider_body_payload = (
                provider_body_payload if isinstance(provider_body_payload, dict) else {}
            )
            request = CalendarMeetingUpdateRequest(
                organisation_id=context.organisation_id,
                requested_by_user_id=context.user_id,
                event_id=parsed_event_id,
                expected_version=expected_version or current_version,
                title=title if title is not None else (target.event.title or "Meeting"),
                start_at=start_at,
                end_at=end_at,
                timezone=timezone_name,
                attendees=desired_attendees,
                body=body if body is not None else target.event.description,
                location=location if location is not None else target.event.location,
                body_changed=body is not None,
                provider_body=provider_body_payload.get("content"),
                provider_body_content_type=provider_body_payload.get("contentType"),
                previous_body=target.event.description,
                provider_etag=(target.event.data_payload or {}).get("etag")
                or (target.event.data_payload or {}).get("@odata.etag"),
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        payload = _meeting_payload(target, request)
        if not confirmed:
            return {
                "status": "confirmation_required",
                "code": "calendar_meeting_update_confirmation_required",
                "message": "Please confirm these exact meeting changes.",
                "current": {
                    "title": target.event.title,
                    "start_time": target.event.start_at.isoformat(),
                    "end_time": target.event.end_at.isoformat(),
                    "attendees": [
                        item.email for item in target.event.attendees if not item.is_organiser
                    ],
                    "body": target.event.description,
                    "location": target.event.location,
                },
                "confirmation_payload": payload,
            }
        try:
            result = await service.update_meeting(request, sender_email=sender_email)
        except CalendarInviteError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}
    return {
        "status": "updated",
        "message": "Meeting updated.",
        "event_id": str(result.event_id),
        "provider_event_id": result.provider_event_id,
        "activity_request_id": str(result.request_id) if result.request_id else None,
    }


async def cancel_calendar_meeting(
    *,
    context: AgentContext,
    event_id: str,
    sender_email: str | None = None,
    cancellation_message: str | None = None,
    expected_version: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Preview or cancel one non-recurring meeting owned by the current user."""

    if not context.user_id:
        return {"status": "error", "message": "Cannot cancel meeting: user not identified."}
    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        return {"status": "error", "code": "invalid_event_id", "message": "Invalid event ID."}
    async with context.acquire_session() as session:
        service = CalendarMeetingMutationService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
        )
        try:
            target = await service.resolve_target(parsed_event_id, sender_email=sender_email)
        except CalendarInviteError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}
        if target.provider == "google" and cancellation_message:
            return {
                "status": "needs_clarification",
                "code": "google_cancellation_message_unsupported",
                "message": (
                    "Google Calendar cannot attach a custom cancellation message here. "
                    "Confirm cancellation without it, or send the note separately."
                ),
            }
        version = expected_version or (
            target.event.source_modified_at.isoformat() if target.event.source_modified_at else None
        )
        preview = {
            "event_id": event_id,
            "sender_email": target.integration.integration_email,
            "title": target.event.title,
            "start_time": target.event.start_at.isoformat(),
            "cancellation_message": cancellation_message,
            "expected_version": version,
        }
        if not confirmed:
            return {
                "status": "confirmation_required",
                "code": "calendar_meeting_cancel_confirmation_required",
                "message": "Please confirm cancellation of this exact meeting.",
                "confirmation_payload": preview,
            }
        try:
            result = await service.cancel_meeting(
                parsed_event_id,
                sender_email=sender_email,
                cancellation_message=cancellation_message,
                expected_version=version,
            )
        except CalendarInviteError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}
    return {
        "status": "cancelled",
        "message": "Meeting cancelled.",
        "event_id": str(result.event_id),
        "provider_event_id": result.provider_event_id,
        "activity_request_id": str(result.request_id) if result.request_id else None,
    }


def create_update_calendar_meeting(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind organizer-owned meeting updates to immutable agent context."""

    async def update_calendar_meeting_tool(
        event_id: str,
        title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        attendees: list[str] | list[dict[str, Any]] | None = None,
        body: str | None = None,
        location: str | None = None,
        timezone: str | None = None,
        sender_email: str | None = None,
        expected_version: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Preview or update an existing meeting organized by the current user."""

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateCalendarMeeting",
            tool_func=update_calendar_meeting,
            parameters={
                "context": context,
                "event_id": event_id,
                "title": title,
                "start_time": start_time,
                "end_time": end_time,
                "attendees": attendees,
                "body": body,
                "location": location,
                "timezone": timezone,
                "sender_email": sender_email,
                "expected_version": expected_version,
                "confirmed": confirmed,
            },
            description="Updating calendar meeting",
            fallback_value={"status": "error", "message": "Failed to update meeting."},
        )
        return dict(result)

    return update_calendar_meeting_tool


def create_cancel_calendar_meeting(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind organizer-owned meeting cancellation to immutable agent context."""

    async def cancel_calendar_meeting_tool(
        event_id: str,
        sender_email: str | None = None,
        cancellation_message: str | None = None,
        expected_version: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Preview or cancel an existing meeting organized by the current user."""

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CancelCalendarMeeting",
            tool_func=cancel_calendar_meeting,
            parameters={
                "context": context,
                "event_id": event_id,
                "sender_email": sender_email,
                "cancellation_message": cancellation_message,
                "expected_version": expected_version,
                "confirmed": confirmed,
            },
            description="Cancelling calendar meeting",
            fallback_value={"status": "error", "message": "Failed to cancel meeting."},
        )
        return dict(result)

    return cancel_calendar_meeting_tool
