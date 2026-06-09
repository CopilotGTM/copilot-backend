"""Agent tool for sending user-owned calendar invites."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import resolve_timezone_name
from src.service.calendar_invites.attendee_resolver import CalendarInviteAttendeeResolver
from src.service.calendar_invites.models import (
    CalendarInviteCreateRequest,
    CalendarInviteError,
)
from src.service.calendar_invites.service import CalendarInviteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities


_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_GENERIC_EMAIL_TITLE_PREFIXES = (
    "meeting with",
    "call with",
    "sync with",
    "meet with",
)
_GENERIC_EMAIL_ONLY_TITLES = {
    "meeting",
    "call",
    "sync",
}


def _parse_datetime(
    value: str,
    timezone_name: str,
    metadata: dict[str, Any] | None,
) -> datetime | None:
    return parse_agent_datetime(
        value,
        timezone_name=timezone_name,
        metadata=metadata,
        require_time=True,
    )


def _build_confirmation_payload(
    *,
    title: str,
    start_time: str,
    end_time: str,
    timezone_name: str,
    attendees: list[dict[str, Any]],
    body: str | None,
    location: str | None,
    deal_id: str | None,
    sender_email: str | None,
    confirmation_reasons: list[str],
) -> dict[str, Any]:
    return {
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "timezone": timezone_name,
        "attendees": attendees,
        "body": body,
        "location": location,
        "deal_id": deal_id,
        "sender_email": sender_email,
        "confirmation_reasons": confirmation_reasons,
    }


def _available_sender_accounts(message: str) -> list[str]:
    """Extract human-facing sender emails from resolver guidance."""
    marker = "Available sender accounts:"
    if marker not in message:
        return []
    account_text = message.split(marker, 1)[1]
    return [match.group(0).rstrip(".") for match in _EMAIL_PATTERN.finditer(account_text)]


def _looks_like_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(_EMAIL_PATTERN.fullmatch(value.strip()))


def _attendees_are_only_direct_emails(attendees: list[str] | list[dict[str, Any]]) -> bool:
    if not attendees:
        return False

    attendee_values: list[str] = []
    for attendee in attendees:
        if isinstance(attendee, str):
            attendee_values.append(attendee)
            continue
        email = attendee.get("email")
        if isinstance(email, str):
            attendee_values.append(email)
            continue
        return False

    return bool(attendee_values) and all(_looks_like_email(value) for value in attendee_values)


def _title_needs_clarification(
    *,
    title: str,
    attendees: list[str] | list[dict[str, Any]],
) -> bool:
    cleaned = str(title or "").strip()
    if not cleaned:
        return True
    if _EMAIL_PATTERN.search(cleaned):
        return True
    return cleaned.lower() in _GENERIC_EMAIL_ONLY_TITLES and _attendees_are_only_direct_emails(
        attendees
    )


def _normalize_invite_title(title: str) -> str:
    cleaned = str(title or "").strip()
    if not cleaned:
        return "Meeting"

    if not _EMAIL_PATTERN.search(cleaned):
        return cleaned

    lowered = cleaned.lower()
    if any(lowered.startswith(prefix) for prefix in _GENERIC_EMAIL_TITLE_PREFIXES):
        return "Meeting"

    without_email = _EMAIL_PATTERN.sub("the attendee", cleaned).strip()
    return without_email or "Meeting"


def _resolve_invite_timezone(
    *,
    timezone: str | None,
    metadata: dict[str, Any],
    confirmed: bool,
) -> tuple[str, dict[str, Any] | None]:
    metadata_timezone = metadata.get("user_timezone")
    context_timezone = resolve_timezone_name(
        metadata_timezone if isinstance(metadata_timezone, str) else None,
        default=None,
    )
    requested_timezone = resolve_timezone_name(timezone, default=None)

    if context_timezone and requested_timezone and context_timezone != requested_timezone:
        if not confirmed:
            return context_timezone, {
                "requested_timezone": requested_timezone,
                "context_timezone": context_timezone,
            }
        return requested_timezone, None

    return context_timezone or requested_timezone or "UTC", None


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
    confirmed: bool = False,
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

    if sender_email and not _looks_like_email(sender_email):
        return {
            "status": "needs_clarification",
            "code": "calendar_sender_email_invalid",
            "message": "I still need a valid sender email address for this invite.",
            "missing_fields": ["sender_email"],
        }

    missing_detail_fields: list[str] = []
    if _title_needs_clarification(title=title, attendees=attendees):
        missing_detail_fields.append("title")
    if not sender_email and not confirmed:
        missing_detail_fields.append("sender_email")
    if missing_detail_fields:
        if missing_detail_fields == ["sender_email"]:
            return {
                "status": "needs_clarification",
                "code": "calendar_sender_email_required",
                "message": "I still need the sender email address for this invite.",
                "missing_fields": missing_detail_fields,
            }
        if missing_detail_fields == ["title"]:
            return {
                "status": "needs_clarification",
                "code": "calendar_invite_title_required",
                "message": "I still need a real meeting title before I can create this invite.",
                "missing_fields": missing_detail_fields,
            }
        return {
            "status": "needs_clarification",
            "code": "calendar_invite_details_required",
            "message": "I still need the meeting title and sender email address for this invite.",
            "missing_fields": missing_detail_fields,
        }
    title = _normalize_invite_title(title)

    metadata = context.metadata or {}
    timezone_name, timezone_conflict = _resolve_invite_timezone(
        timezone=timezone,
        metadata=metadata,
        confirmed=confirmed,
    )
    if timezone_conflict:
        confirmation_reasons = ["timezone_conflicts_with_user_timezone"]
        confirmation_payload = _build_confirmation_payload(
            title=title,
            start_time=start_time,
            end_time=end_time,
            timezone_name=timezone_name,
            attendees=[],
            body=body,
            location=location,
            deal_id=deal_id,
            sender_email=sender_email,
            confirmation_reasons=confirmation_reasons,
        )
        confirmation_payload["requested_timezone"] = timezone_conflict["requested_timezone"]
        confirmation_payload["context_timezone"] = timezone_conflict["context_timezone"]
        return {
            "status": "confirmation_required",
            "code": "calendar_invite_timezone_confirmation_required",
            "message": (
                "The requested timezone differs from your app timezone. Please confirm "
                "which timezone to use before I send the calendar invite."
            ),
            "resolved_attendees": [],
            "confirmation_payload": confirmation_payload,
            "confirmation_reasons": confirmation_reasons,
        }

    start_at = _parse_datetime(start_time, timezone_name, metadata)
    end_at = _parse_datetime(end_time, timezone_name, metadata)
    if not start_at or not end_at:
        missing_fields = []
        if not start_at:
            missing_fields.append("start_time")
        if not end_at:
            missing_fields.append("end_time")
        return {
            "status": "needs_clarification",
            "code": "calendar_invite_time_required",
            "message": "Could not understand the meeting start or end time.",
            "missing_fields": missing_fields,
        }
    if end_at <= start_at:
        return {"status": "error", "message": "Meeting end time must be after start time."}

    async with context.acquire_session() as session:
        resolver = CalendarInviteAttendeeResolver(
            session,
            organisation_id=context.organisation_id,
        )
        resolution = await resolver.resolve(attendees, deal_id=deal_id)

        if resolution.has_blockers:
            return {
                "status": "needs_clarification",
                "code": "calendar_attendees_need_clarification",
                "message": (
                    "I need the exact attendee emails before I can send this calendar invite."
                ),
                "missing_fields": ["attendees"],
                "resolved_attendees": resolution.public_resolved(),
                "ambiguous_attendees": resolution.ambiguous,
                "missing_attendees": resolution.missing,
                "confirmation_reasons": resolution.confirmation_reasons,
            }

        confirmation_reasons = list(resolution.confirmation_reasons)
        if not sender_email:
            confirmation_reasons.append("sender_not_explicit")
        if "final_send_confirmation" not in confirmation_reasons:
            confirmation_reasons.append("final_send_confirmation")

        if not confirmed:
            resolved_attendees = resolution.public_resolved()
            return {
                "status": "confirmation_required",
                "code": "calendar_invite_confirmation_required",
                "message": (
                    "Please confirm the sender, time, timezone, and resolved attendees before "
                    "I send the calendar invite."
                ),
                "resolved_attendees": resolved_attendees,
                "confirmation_payload": _build_confirmation_payload(
                    title=title,
                    start_time=start_time,
                    end_time=end_time,
                    timezone_name=timezone_name,
                    attendees=resolved_attendees,
                    body=body,
                    location=location,
                    deal_id=deal_id,
                    sender_email=sender_email,
                    confirmation_reasons=confirmation_reasons,
                ),
                "confirmation_reasons": confirmation_reasons,
            }

        try:
            # Build and validate the DTO before side effects. This catches bad
            # tool calls early and keeps bad payloads from creating audit rows.
            request = CalendarInviteCreateRequest(
                organisation_id=context.organisation_id,
                requested_by_user_id=user_id,
                title=title,
                body=body,
                location=location,
                start_at=start_at,
                end_at=end_at,
                timezone=timezone_name,
                attendees=resolution.service_attendees(),
                deal_id=deal_id,
                sender_email=sender_email,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

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
            if exc.code == "calendar_sender_selection_required":
                return {
                    "status": "needs_clarification",
                    "code": exc.code,
                    "message": exc.message,
                    "missing_fields": ["sender_email"],
                    "available_sender_accounts": _available_sender_accounts(exc.message),
                }
            return {"status": "error", "message": exc.message, "code": exc.code}

    return {
        "status": "sent",
        "message": "Calendar invite sent.",
        "request_id": str(result.request_id) if result.request_id else None,
        "activity_request_id": str(result.request_id) if result.request_id else None,
        "activity_type": "send_calendar_invite",
        "linked_record_type": "calendar_event" if result.event_id else None,
        "linked_record_id": str(result.event_id) if result.event_id else None,
        "provider_resource_id": result.provider_event_id,
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
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Send a calendar invite with a meeting link.

        Use when the user asks to schedule a meeting, create a calendar invite,
        or send an invite. This creates a real calendar event from the connected
        user account and sends attendee invites.

        Pass exact attendee emails when known. For names like "Saksham Bhutani"
        or group phrases like "all stakeholders", pass the user's raw attendee
        text and deal_id when available instead of asking the user for email
        first. The tool searches deal stakeholders, CRM contacts, CRM users when
        synced, and active approved internal users; it will return
        confirmation_required or needs_clarification instead of sending if there
        is any identity doubt. If the user confirms a confirmation_payload, call
        again with those resolved attendee emails and confirmed=True.

        Only pass timezone when the user explicitly names a timezone different
        from their app timezone. Otherwise omit timezone; the backend uses the
        captured app timezone from context. If the user has multiple connected
        Calendar accounts, the tool returns missing_fields=["sender_email"] and
        available_sender_accounts; ask for sender together with any other missing
        fields. Provide concrete start and end times; ask one bundled follow-up
        if any required title, time, attendee, or sender field is missing.
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
                "confirmed": confirmed,
            },
            description="Sending calendar invite",
            fallback_value={"status": "error", "message": "Failed to send calendar invite."},
        )
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return send_calendar_invite_tool
