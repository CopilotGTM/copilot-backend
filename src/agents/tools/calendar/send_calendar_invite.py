"""Agent tool for sending user-owned calendar invites."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

from src.agents.tools.base import execute_with_capabilities
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import resolve_timezone_name
from src.service.calendar_invites.attendee_resolver import CalendarInviteAttendeeResolver
from src.service.calendar_invites.models import (
    CalendarInviteCreateRequest,
    CalendarInviteError,
)
from src.service.calendar_invites.resolver import CalendarIntegrationResolver
from src.service.calendar_invites.service import (
    CalendarInviteService,
    derive_idempotency_key,
)
from src.service.crm.utils.internal_domains import get_internal_domains
from src.service.users.calendar_sender_preferences_service import (
    load_preferred_calendar_sender,
)

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
_MERIDIEM_PATTERN = re.compile(r"\b(?:a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)
_TIME_ONLY_PATTERN = re.compile(
    r"^\s*(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)?\s*$",
    re.IGNORECASE,
)
# A single-digit/ordinary 12-hour clock such as "3:30" needs AM/PM. Leading-zero
# and 13–23 hour inputs are treated as deliberate 24-hour notation.
_AMBIGUOUS_CLOCK_PATTERN = re.compile(r"(?<![\d:])(?:[1-9]|1[0-2]):[0-5]\d(?![\d:])")
_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"
# Keep the conversational lead-in shorter than the canonical review it introduces.
_RESPONSE_NOTE_MAX_LENGTH = 500


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
    start_time: str | None,
    end_time: str | None,
    timezone_name: str | None,
    attendees: list[dict[str, Any]],
    body: str | None,
    location: str | None,
    deal_id: str | None,
    sender_email: str | None,
    show_sender: bool,
    sender_selection_reason: str | None,
    confirmation_reasons: list[str],
    inferred_fields: list[str],
    warnings: list[str],
    missing_fields: list[str] | None = None,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    required_attendees = [attendee for attendee in attendees if not attendee.get("optional")]
    optional_attendees = [attendee for attendee in attendees if attendee.get("optional")]
    return {
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "timezone": timezone_name,
        "attendees": attendees,
        "required_attendees": required_attendees,
        "optional_attendees": optional_attendees,
        "body": body,
        "location": location,
        "deal_id": deal_id,
        "sender_email": sender_email,
        "show_sender": show_sender,
        "sender_selection_reason": sender_selection_reason,
        "confirmation_reasons": confirmation_reasons,
        "inferred_fields": inferred_fields,
        "warnings": warnings,
        "missing_fields": missing_fields or [],
        "confirmation_token": confirmation_token,
    }


def _looks_like_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(_EMAIL_PATTERN.fullmatch(value.strip()))


async def _current_turn_clarification_email(
    session: Any,
    context: AgentContext,
) -> str | None:
    """Return one explicit email from the persisted current user message.

    The tool reads the persisted turn instead of trusting reconstructed model
    arguments. Only an email by itself or behind a small affirmative wrapper is
    accepted, so negated or multi-address messages still require clarification.
    """
    raw_message_id = (context.metadata or {}).get("current_user_message_id")
    if not raw_message_id:
        return None
    try:
        message_id = UUID(str(raw_message_id))
    except ValueError:
        return None

    from src.domain.models.organisation_tables.chat_message_models import ChatMessage

    message = await session.get(ChatMessage, message_id)
    if not message or message.organisation_id != context.organisation_id:
        return None
    content = message.content
    text = str(content.get("text") or "") if isinstance(content, dict) else str(content or "")
    matches = {match.lower() for match in _EMAIL_PATTERN.findall(text)}
    if len(matches) != 1:
        return None
    email = matches.pop()
    remainder = _EMAIL_PATTERN.sub("", text).strip().lower().strip(" .,:!?")
    safe_wrappers = {"", "use", "use this", "email", "email is", "his email is", "her email is"}
    return email if remainder in safe_wrappers else None


def _replace_one_unresolved_attendee(
    attendees: list[str] | list[dict[str, Any]],
    *,
    unresolved_input: Any,
    email: str,
) -> list[str] | list[dict[str, Any]] | None:
    """Replace one unresolved model argument while preserving its attendee role."""
    unresolved = str(unresolved_input or "").strip().lower()
    replacement: list[Any] = []
    replaced = False
    for attendee in attendees:
        if isinstance(attendee, str):
            if not replaced and attendee.strip().lower() == unresolved:
                replacement.append(email)
                replaced = True
            else:
                replacement.append(attendee)
            continue

        value = str(
            attendee.get("value") or attendee.get("email") or attendee.get("name") or ""
        ).strip()
        if not replaced and value.lower() == unresolved:
            replacement.append(
                {
                    "email": email,
                    "name": attendee.get("name") or value or None,
                    "optional": bool(attendee.get("optional", False)),
                }
            )
            replaced = True
        else:
            replacement.append(attendee)
    return cast(list[str] | list[dict[str, Any]], replacement) if replaced else None


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
    """Return a safe display title without rewriting intentional user casing."""
    cleaned = str(title or "").strip()
    if not cleaned:
        return "Meeting"

    if _EMAIL_PATTERN.search(cleaned):
        lowered = cleaned.lower()
        if any(lowered.startswith(prefix) for prefix in _GENERIC_EMAIL_TITLE_PREFIXES):
            return "Meeting"

        cleaned = _EMAIL_PATTERN.sub("the attendee", cleaned).strip() or "Meeting"

    # Uppercase only the leading character; str.capitalize/title would damage
    # intentional casing in names and acronyms such as eBay, QBR, or API.
    return f"{cleaned[:1].upper()}{cleaned[1:]}"


def _resolve_invite_timezone(
    *,
    timezone: str | None,
    metadata: dict[str, Any],
) -> str:
    metadata_timezone = metadata.get("user_timezone")
    context_timezone = resolve_timezone_name(
        metadata_timezone if isinstance(metadata_timezone, str) else None,
        default=None,
    )
    requested_timezone = resolve_timezone_name(timezone, default=None)

    # An explicitly named timezone is intentional input, not a conflict. The
    # captured app timezone is only the default when the prompt omits one.
    return requested_timezone or context_timezone or "UTC"


def _has_ambiguous_clock_time(value: str | None) -> bool:
    """Return whether natural-language clock input needs an AM/PM clarification."""
    if not value or _MERIDIEM_PATTERN.search(value):
        return False
    # ISO and leading-zero inputs already express a 24-hour clock, even when
    # their hour happens to be within the 1–12 range.
    if re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        return False
    return bool(_AMBIGUOUS_CLOCK_PATTERN.search(value))


def _parse_end_datetime(
    value: str,
    *,
    start_at: datetime | None,
    timezone_name: str,
    metadata: dict[str, Any],
) -> datetime | None:
    """Parse an end time, borrowing the reviewed start date when only a clock is given."""
    candidate = value
    if start_at and _TIME_ONLY_PATTERN.fullmatch(value):
        local_date = start_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        candidate = f"{local_date} {value}"
    parsed = _parse_datetime(candidate, timezone_name, metadata)
    if start_at and parsed and parsed <= start_at:
        # A weekday such as "Friday 6 PM" can resolve to today while the start
        # resolved to next Friday. Re-anchor the clock to the reviewed start date.
        clock_text = re.sub(
            r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        local_date = start_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        parsed = _parse_datetime(f"{local_date} {clock_text}", timezone_name, metadata)
    return parsed


def _derive_invite_title(
    title: str | None,
    *,
    resolved_attendees: list[dict[str, Any]],
    raw_attendees: list[str] | list[dict[str, Any]],
) -> tuple[str, bool]:
    """Return a safe meeting title and whether the tool inferred or replaced it."""
    supplied = str(title or "").strip()
    if supplied and not _title_needs_clarification(title=supplied, attendees=raw_attendees):
        return _normalize_invite_title(supplied), False

    names = [
        str(attendee.get("name") or "").strip()
        for attendee in resolved_attendees
        if str(attendee.get("name") or "").strip()
    ]
    if not names:
        names = [
            str(attendee if isinstance(attendee, str) else attendee.get("name") or "").strip()
            for attendee in raw_attendees
            if "@"
            not in str(attendee if isinstance(attendee, str) else attendee.get("email") or "")
        ]
        names = [name for name in names if name]
    if len(names) == 1:
        return f"Meeting with {names[0]}", True
    if len(names) > 1:
        return f"Meeting with {names[0]} and {len(names) - 1} others", True
    return "Meeting", True


def _review_clock(value: datetime) -> tuple[str, str]:
    """Return a compact clock and meridiem pair for deterministic review text."""
    rendered = value.strftime("%I:%M %p")
    clock, meridiem = rendered.rsplit(" ", 1)
    return clock.lstrip("0"), meridiem


def _review_time_line(review_payload: dict[str, Any]) -> str:
    """Format the exact reviewed interval and duration in the selected local timezone."""
    reviewed_timezone = ZoneInfo(str(review_payload["timezone"]))
    start_at = datetime.fromisoformat(str(review_payload["start_time"])).astimezone(
        reviewed_timezone
    )
    end_at = datetime.fromisoformat(str(review_payload["end_time"])).astimezone(reviewed_timezone)
    start_clock, start_meridiem = _review_clock(start_at)
    end_clock, end_meridiem = _review_clock(end_at)
    timezone_label = start_at.tzname() or str(review_payload["timezone"])
    duration_minutes = int((end_at - start_at).total_seconds() // 60)
    duration = f"{duration_minutes} minutes"

    start_date = f"{start_at.day} {start_at.strftime('%B')}"
    if start_at.date() == end_at.date():
        clocks = (
            f"{start_clock}–{end_clock} {end_meridiem}"
            if start_meridiem == end_meridiem
            else f"{start_clock} {start_meridiem}–{end_clock} {end_meridiem}"
        )
        return f"{start_date}, {clocks} {timezone_label}, {duration}"

    end_date = f"{end_at.day} {end_at.strftime('%B')}"
    return (
        f"{start_date}, {start_clock} {start_meridiem}–"
        f"{end_date}, {end_clock} {end_meridiem} {timezone_label}, {duration}"
    )


def _review_attendee_emails(attendees: list[dict[str, Any]]) -> str:
    """Return the exact normalized addresses included in one attendee role."""
    return ", ".join(str(attendee["email"]) for attendee in attendees)


def _channel_label(context: AgentContext, label: str) -> str:
    """Bold a field label with the markup understood by the active channel."""
    channel = (context.channel or "").lower()
    if channel in {"whatsapp", "slack", "google_chat"}:
        return f"*{label}:*"
    if channel in {"teams", "web"}:
        return f"**{label}:**"
    return f"{label}:"


def _response_note_text(response_note: str | None) -> str:
    """Keep a model-supplied query answer on one bounded line before the fixed review."""
    return " ".join((response_note or "").split())[:_RESPONSE_NOTE_MAX_LENGTH].strip()


def _build_review_channel_payload(
    context: AgentContext,
    review_payload: dict[str, Any],
    response_note: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build one canonical meeting review for every supported conversation channel."""
    lines = [
        f"{_channel_label(context, 'Title')} {review_payload['title']}",
        f"{_channel_label(context, 'When')} {_review_time_line(review_payload)}",
    ]
    if review_payload["show_sender"]:
        lines.append(f"{_channel_label(context, 'From')} {review_payload['sender_email']}")
    lines.append(
        f"{_channel_label(context, 'To')} "
        f"{_review_attendee_emails(review_payload['required_attendees'])}"
    )
    if review_payload["optional_attendees"]:
        lines.append(
            f"{_channel_label(context, 'Optional')} "
            f"{_review_attendee_emails(review_payload['optional_attendees'])}"
        )
    send_word = _channel_label(context, "send").replace(":", "")
    lines.extend(["", f"Reply {send_word} to confirm."])
    review_text = "\n".join(lines)
    note = _response_note_text(response_note)
    if note:
        review_text = f"{note}\n\n{review_text}"

    channel = (context.channel or "").lower()
    channel_payload: dict[str, Any]
    if channel in {"web", "voice"}:
        channel_payload = {
            "type": "ui_card",
            "content": {
                "card": {
                    "kind": "calendar_invite_review",
                    "version": "1.0",
                    "tone": "confirmation",
                    "title": "Meeting invite ready for confirmation",
                    "message": review_text,
                    "meeting": review_payload,
                    "actions": [],
                }
            },
            "text_fallback": review_text,
            "attachments": None,
            "source_text": review_text,
            "force_override": True,
        }
    else:
        channel_payload = {
            "type": "message",
            "content": {"text": review_text},
            "text_fallback": review_text,
            "attachments": None,
            "source_text": review_text,
            "force_override": True,
        }
    return review_text, channel_payload


def _meeting_location(location: str | None, conference_link: str | None) -> str:
    """Return a provider-labelled, copyable meeting location without inventing a link."""
    if not conference_link:
        return str(location or "Not specified")

    hostname = (urlparse(conference_link).hostname or "").lower()
    if hostname in {"teams.microsoft.com", "teams.live.com"}:
        provider = "Microsoft Teams"
    elif hostname == "meet.google.com":
        provider = "Google Meet"
    else:
        provider = "Online meeting"
    return f"{provider} — {conference_link}"


def _build_receipt_channel_payload(
    context: AgentContext,
    receipt_payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build the canonical receipt from the invite that the provider accepted."""
    lines = [
        "Meeting invite sent.",
        "",
        f"• {_channel_label(context, 'Title')} {receipt_payload['title']}",
        f"• {_channel_label(context, 'When')} {_review_time_line(receipt_payload)}",
        (
            f"• {_channel_label(context, 'To')} "
            f"{_review_attendee_emails(receipt_payload['required_attendees'])}"
        ),
    ]
    if receipt_payload["optional_attendees"]:
        lines.append(
            f"• {_channel_label(context, 'Optional')} "
            f"{_review_attendee_emails(receipt_payload['optional_attendees'])}"
        )
    lines.extend(
        [
            f"• {_channel_label(context, 'From')} {receipt_payload['sender_email']}",
            f"• {_channel_label(context, 'Location')} {receipt_payload['display_location']}",
        ]
    )
    receipt_text = "\n".join(lines)
    channel_payload = {
        "type": "message",
        "content": {"text": receipt_text},
        "text_fallback": receipt_text,
        "attachments": None,
        "source_text": receipt_text,
        "force_override": True,
    }
    return receipt_text, channel_payload


async def send_calendar_invite(
    *,
    context: AgentContext,
    attendees: list[str] | list[dict[str, Any]],
    title: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    body: str | None = None,
    location: str | None = None,
    deal_id: str | None = None,
    timezone: str | None = None,
    sender_email: str | None = None,
    idempotency_key: str | None = None,
    confirmed: bool = False,
    response_note: str | None = None,
) -> dict[str, Any]:
    """Resolve and preview one tenant-scoped invite before its provider write.

    The authenticated user and organization come only from AgentContext. Names,
    sender defaults, and scheduling shortcuts are resolved before confirmation;
    the service receives only the exact reviewed email addresses and timestamps.
    Caller idempotency keys remain accepted for compatibility but do not authorize
    the send or replace the canonical request hash.
    """
    _ = idempotency_key
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

    metadata = context.metadata or {}
    whatsapp_send_authorized = (context.channel or "").lower() != "whatsapp" or metadata.get(
        "whatsapp_action_authority"
    ) in {"direct_current_turn", "pending_action"}
    metadata_timezone = metadata.get("user_timezone")
    context_timezone = resolve_timezone_name(
        metadata_timezone if isinstance(metadata_timezone, str) else None,
        default=None,
    )
    requested_timezone = resolve_timezone_name(timezone, default=None)
    timezone_name = _resolve_invite_timezone(
        timezone=timezone,
        metadata=metadata,
    )
    missing_fields: list[str] = []
    inferred_fields: list[str] = []
    if (timezone and not requested_timezone) or (not timezone and not context_timezone):
        missing_fields.append("timezone")

    ambiguous_start_time = _has_ambiguous_clock_time(start_time)
    start_at = (
        _parse_datetime(start_time, timezone_name, metadata)
        if start_time and not ambiguous_start_time
        else None
    )
    if not start_time:
        missing_fields.extend(["date", "start_time"])
    else:
        if ambiguous_start_time:
            missing_fields.append("start_time_am_pm")
        elif not start_at:
            missing_fields.append("start_time")

    if end_time:
        ambiguous_end_time = _has_ambiguous_clock_time(end_time)
        end_at = (
            _parse_end_datetime(
                end_time,
                start_at=start_at,
                timezone_name=timezone_name,
                metadata=metadata,
            )
            if not ambiguous_end_time
            else None
        )
        if ambiguous_end_time:
            missing_fields.append("end_time_am_pm")
        elif not end_at:
            missing_fields.append("end_time")
    elif start_at:
        end_at = start_at + timedelta(minutes=30)
        inferred_fields.append("duration_30_minutes")
    else:
        end_at = None

    if start_at and end_at and end_at <= start_at:
        return {"status": "error", "message": "Meeting end time must be after start time."}

    preferred_sender = await load_preferred_calendar_sender(
        organisation_id=context.organisation_id,
        user_id=user_id,
    )
    official_domains = await get_internal_domains(context.organisation_id)

    async with context.acquire_session() as session:
        attendee_resolver = CalendarInviteAttendeeResolver(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=user_id,
            user_email=getattr(context, "user_email", None),
        )
        resolution = await attendee_resolver.resolve(attendees, deal_id=deal_id)
        blockers = [*resolution.missing, *resolution.ambiguous]
        if len(blockers) == 1:
            clarification_email = await _current_turn_clarification_email(session, context)
            replacement = (
                _replace_one_unresolved_attendee(
                    attendees,
                    unresolved_input=blockers[0].get("input"),
                    email=clarification_email,
                )
                if clarification_email
                else None
            )
            if replacement is not None:
                # Supplying the missing address completes identity resolution but
                # is not permission to send; always return the reviewed invite.
                attendees = replacement
                confirmed = False
                resolution = await attendee_resolver.resolve(attendees, deal_id=deal_id)

        try:
            sender_resolution = await CalendarIntegrationResolver().resolve(
                session,
                context.organisation_id,
                user_id,
                sender_email=sender_email,
                preferred_sender_email=preferred_sender,
                user_email=getattr(context, "user_email", None),
                official_domains=official_domains,
            )
        except CalendarInviteError as exc:
            return {
                "status": "error",
                "code": exc.code,
                "message": exc.message,
            }

        resolved_attendees = resolution.public_resolved()
        resolved_sender = sender_resolution.integration.integration_email
        effective_title, inferred_title = _derive_invite_title(
            title,
            resolved_attendees=resolved_attendees,
            raw_attendees=attendees,
        )
        if inferred_title:
            inferred_fields.append("title")
        if not timezone and context_timezone:
            inferred_fields.append("timezone")
        warnings = [sender_resolution.warning] if sender_resolution.warning else []

        if resolution.has_blockers:
            missing_fields.append("attendees")
        missing_fields = list(dict.fromkeys(missing_fields))
        confirmation_reasons = list(resolution.confirmation_reasons)
        if not sender_email and "sender_inferred" not in confirmation_reasons:
            confirmation_reasons.append("sender_inferred")
        if "final_send_confirmation" not in confirmation_reasons:
            confirmation_reasons.append("final_send_confirmation")

        show_sender = sender_resolution.connected_account_count > 1
        review_start_time = (
            start_at.astimezone(ZoneInfo(timezone_name)).isoformat() if start_at else start_time
        )
        review_end_time = (
            end_at.astimezone(ZoneInfo(timezone_name)).isoformat() if end_at else end_time
        )
        review_payload = _build_confirmation_payload(
            title=effective_title,
            start_time=review_start_time,
            end_time=review_end_time,
            timezone_name=timezone_name,
            attendees=resolved_attendees,
            body=body,
            location=location,
            deal_id=deal_id,
            sender_email=resolved_sender,
            show_sender=show_sender,
            sender_selection_reason=sender_resolution.selection_reason,
            confirmation_reasons=confirmation_reasons,
            inferred_fields=list(dict.fromkeys(inferred_fields)),
            warnings=warnings,
            missing_fields=missing_fields,
        )

        if missing_fields:
            clarification_code = (
                "calendar_attendees_need_clarification"
                if missing_fields == ["attendees"]
                else "calendar_invite_details_required"
            )
            return {
                "status": "needs_clarification",
                "code": clarification_code,
                "message": (
                    resolution.clarification_message()
                    if missing_fields == ["attendees"]
                    else "I need the remaining meeting details before I can show the final review."
                ),
                "missing_fields": missing_fields,
                "resolved_attendees": resolved_attendees,
                "ambiguous_attendees": resolution.ambiguous,
                "missing_attendees": resolution.missing,
                "review_payload": review_payload,
                "confirmation_reasons": confirmation_reasons,
            }

        try:
            request = CalendarInviteCreateRequest(
                organisation_id=context.organisation_id,
                requested_by_user_id=user_id,
                title=effective_title,
                body=body,
                location=location,
                start_at=start_at,
                end_at=end_at,
                timezone=timezone_name,
                attendees=resolution.service_attendees(),
                deal_id=deal_id,
                sender_email=resolved_sender,
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

        confirmation_token = derive_idempotency_key(
            request,
            integration_id=sender_resolution.integration.integration_id,
        )
        review_payload["confirmation_token"] = confirmation_token
        # Forwarded WhatsApp content is quoted evidence, so a model-supplied
        # confirmation cannot turn it into permission for an external write.
        if not confirmed or not whatsapp_send_authorized:
            review_text, channel_payload = _build_review_channel_payload(
                context,
                review_payload,
                response_note,
            )
            confirmation_result: dict[str, Any] = {
                "status": "confirmation_required",
                "code": "calendar_invite_confirmation_required",
                "message": "Review the meeting details, then confirm this exact invite.",
                "resolved_attendees": resolved_attendees,
                "confirmation_payload": review_payload,
                "confirmation_reasons": confirmation_reasons,
                "review_text": review_text,
                _BOT_CHANNEL_PAYLOAD_KEY: channel_payload,
            }
            if channel_payload["type"] == "ui_card":
                confirmation_result["ui_card"] = channel_payload["content"]["card"]
            return confirmation_result

        # The final request hash remains the provider retry boundary even though
        # user authorization is now represented by the current turn's intent.
        request = request.model_copy(update={"idempotency_key": confirmation_token})
        service = CalendarInviteService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=user_id,
        )
        try:
            invite_result = await service.create_invite(request)
        except CalendarInviteError as exc:
            return {"status": "error", "message": exc.message, "code": exc.code}

    receipt_payload = {
        "title": invite_result.subject or effective_title,
        "start_time": review_payload["start_time"],
        "end_time": review_payload["end_time"],
        "timezone": review_payload["timezone"],
        "required_attendees": review_payload["required_attendees"],
        "optional_attendees": review_payload["optional_attendees"],
        "sender_email": resolved_sender,
        "location": location,
        "display_location": _meeting_location(location, invite_result.conference_link),
        "conference_link": invite_result.conference_link,
    }
    receipt_text, channel_payload = _build_receipt_channel_payload(context, receipt_payload)
    return {
        "status": "sent",
        "message": "Calendar invite sent.",
        "request_id": str(invite_result.request_id) if invite_result.request_id else None,
        "activity_request_id": (
            str(invite_result.request_id) if invite_result.request_id else None
        ),
        "activity_type": "send_calendar_invite",
        "linked_record_type": "calendar_event" if invite_result.event_id else None,
        "linked_record_id": str(invite_result.event_id) if invite_result.event_id else None,
        "provider_resource_id": invite_result.provider_event_id,
        "provider_event_id": invite_result.provider_event_id,
        "event_id": str(invite_result.event_id) if invite_result.event_id else None,
        "conference_link": invite_result.conference_link,
        "receipt_payload": receipt_payload,
        "receipt_text": receipt_text,
        _BOT_CHANNEL_PAYLOAD_KEY: channel_payload,
    }


def create_send_calendar_invite(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the send_calendar_invite tool bound to a context."""

    async def send_calendar_invite_tool(
        attendees: list[str] | list[dict[str, Any]],
        title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        body: str | None = None,
        location: str | None = None,
        deal_id: str | None = None,
        timezone: str | None = None,
        sender_email: str | None = None,
        idempotency_key: str | None = None,
        confirmed: bool = False,
        response_note: str | None = None,
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
        is any identity doubt. Set confirmed=True only when the current user
        message explicitly asks to send the fully resolved invite.

        If lookup suggests a close but non-exact email, repeat the address the
        user supplied and the suggested address, then ask before using it. A
        valid direct email needs no saved contact and may proceed to preview.

        When the user says "from our team", "our colleague", or "our rep", pass
        that attendee as {"name": "<name>", "scope": "team", "optional": <bool>}.
        Team scope searches crm_users only and never falls back to an external contact.
        Carry that scope only through clarification of the same unresolved attendee;
        a later request naming somebody else is unscoped unless it repeats the team cue.

        Omit title to derive "Meeting with <name>". Omit end_time to use the
        30-minute default. Date and start time remain required; the tool returns
        every unresolved field together. Sender selection is automatic. A
        clarification that only selects a person or supplies a detail is not
        confirmation; the same clarification paired with send intent is.
        Pass response_note only when the user's current question needs one short
        answer before a confirmation preview; do not repeat meeting fields in it.
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
                "idempotency_key": idempotency_key,
                "confirmed": confirmed,
                "response_note": response_note,
            },
            description="Sending calendar invite",
            fallback_value={"status": "error", "message": "Failed to send calendar invite."},
        )
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return send_calendar_invite_tool
