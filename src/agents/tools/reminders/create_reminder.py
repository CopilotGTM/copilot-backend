"""Create Reminder tool — lets the GeneralAgent create a scheduled reminder or agentic alert.

The LLM extracts the user's intent (title, time expression, type) and passes it
to this tool. The tool resolves the time server-side (hybrid: ISO 8601 first,
then dateparser for natural language), validates that the user provided a real
reminder intent, creates an ACTIVE DB row, and schedules it immediately.

The old preview-confirm path remains available as ``create_reminder_form`` for
legacy cards that were already sent before direct reminder creation existed.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- user_id: Scopes reminder ownership

This tool supports Teams, Slack, WhatsApp, Web, and Voice chats.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.adapters import ChannelPayload
from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
    UserReminder,
)
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import resolve_timezone_name
from src.service.channels import OutboundChannelService, OutboundDeliveryTarget
from src.service.reminders.reminder_service import ReminderService, ReminderServiceError

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"
_DELIVERY_FALLBACK_ORDER = ("teams", "slack", "whatsapp")
_GENERIC_REMINDER_TITLES = {
    "reminder",
    "a reminder",
    "the reminder",
    "test reminder",
    "new reminder",
    "set a reminder",
    "create a reminder",
}


def _escape_slack_mrkdwn(value: str) -> str:
    """Escape special characters for Slack mrkdwn."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_field(label: str, value: Any) -> dict[str, str]:
    """Build a Slack section field with label + value."""
    text = _escape_slack_mrkdwn(str(value))
    return {"type": "mrkdwn", "text": f"*{label}*\n{text}"}


def _slack_quote(value: str) -> str:
    """Render multi-line text as a Slack blockquote."""
    escaped = _escape_slack_mrkdwn(value)
    lines = escaped.splitlines() or [escaped]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _clean_text(value: str | None) -> str:
    return (value or "").strip()


def _normalized_label(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _is_generic_reminder_title(title: str) -> bool:
    normalized = _normalized_label(title)
    return normalized in _GENERIC_REMINDER_TITLES


def _missing_fields_error(message: str, missing_fields: list[str]) -> dict[str, Any]:
    return {"status": "error", "message": message, "missing_fields": missing_fields}


def _delivery_context_is_ready(delivery_channel: str, delivery_context: dict[str, Any]) -> bool:
    return OutboundChannelService.delivery_context_is_ready(delivery_channel, delivery_context)


def _normalize_delivery_context(
    delivery_channel: str,
    conversation_context: dict[str, Any] | None,
    external_tenant_id: str | None = None,
) -> dict[str, Any]:
    return OutboundChannelService.normalize_delivery_context(
        delivery_channel,
        conversation_context,
        external_tenant_id,
    )


def _resolve_current_reminder_delivery_target(
    context: AgentContext,
) -> tuple[str, dict[str, Any]] | None:
    channel = (context.channel or "").lower()
    if channel not in _DELIVERY_FALLBACK_ORDER:
        return None

    metadata = context.metadata or {}
    if channel == "teams":
        conversation_id = metadata.get("conversation_id", "")
        delivery_context = {
            "service_url": metadata.get("service_url", ""),
            "conversation_id": conversation_id,
            "conversation_type": metadata.get("conversation_type", ""),
            "teams_conversation_id": metadata.get("teams_conversation_id") or conversation_id,
        }
    elif channel == "slack":
        delivery_context = {
            "team_id": metadata.get("team_id", ""),
            "channel_id": metadata.get("channel_id", ""),
            "conversation_type": metadata.get("conversation_type") or "personal",
        }
    else:
        delivery_context = {
            "phone_number_id": metadata.get("phone_number_id", ""),
            "recipient_wa_id": metadata.get("recipient_wa_id", ""),
        }

    if not _delivery_context_is_ready(channel, delivery_context):
        return None
    return channel, delivery_context


async def _resolve_mapping_delivery_context(
    context: AgentContext,
    delivery_channel: str,
) -> dict[str, Any] | None:
    if not context.user_id:
        return None

    if delivery_channel not in _DELIVERY_FALLBACK_ORDER:
        return None

    async for session in acquire_master_async_session():
        target = await OutboundChannelService().resolve_user_channel(
            session,
            context.organisation_id,
            context.user_id,
            delivery_channel,
        )
        return target.delivery_context if target else None
    return None


async def _resolve_reminder_delivery_target(
    context: AgentContext,
) -> tuple[str, dict[str, Any]] | None:
    current_target = _resolve_current_reminder_delivery_target(context)
    if current_target:
        return current_target

    channel = (context.channel or "").lower()
    channels_to_try = (channel,) if channel in _DELIVERY_FALLBACK_ORDER else _DELIVERY_FALLBACK_ORDER

    for delivery_channel in channels_to_try:
        delivery_context = await _resolve_mapping_delivery_context(context, delivery_channel)
        if delivery_context:
            return delivery_channel, delivery_context

    return None


def _strip_recurrence_prefix(expr: str) -> str:
    """Strip recurrence-related prefixes that confuse dateparser.

    "every day at 10 AM" → "at 10 AM"
    "daily at 10 AM IST" → "at 10 AM IST"
    "Every Monday at 9pm IST" → "at 9pm IST"
    "weekly at 3 PM" → "at 3 PM"
    """
    import re

    # Day names pattern for "every Monday", "every Tuesday", etc.
    days = r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"

    return re.sub(
        rf"^(?:"
        rf"every\s+(?:other\s+)?(?:day|week|month|{days})\s+"
        rf"|daily\s+|weekly\s+|biweekly\s+|monthly\s+"
        rf"|each\s+(?:day|week|{days})\s+"
        rf"|every\s+(?:morning|evening|weekday)\s+"
        rf")",
        "",
        expr.strip(),
        flags=re.IGNORECASE,
    ).strip()


def _resolve_trigger_time(
    trigger_time_expr: str,
    timezone_name: str,
    metadata: dict[str, Any] | None = None,
) -> datetime | None:
    """Parse a time expression into a UTC datetime.

    Tries ISO 8601 first (fast path when LLM sends structured time),
    then falls back to dateparser for natural language ("in 5 minutes",
    "at 3 PM", "tomorrow 9 AM").
    """
    cleaned_expr = _strip_recurrence_prefix(trigger_time_expr)
    parsed = parse_agent_datetime(
        cleaned_expr,
        timezone_name=timezone_name,
        metadata=metadata,
        require_time=True,
    )
    if parsed is None:
        logger.debug("dateparser failed for: %s", trigger_time_expr)
    return parsed


def _resolve_effective_timezone(context: AgentContext, timezone_name: str | None) -> str:
    metadata = context.metadata or {}
    metadata_timezone = metadata.get("user_timezone")
    return (
        resolve_timezone_name(
            metadata_timezone if isinstance(metadata_timezone, str) else None,
            timezone_name,
            default="UTC",
        )
        or "UTC"
    )


def _build_preview_card(
    reminder_id: str,
    organisation_id: str,
    title: str,
    reminder_type: str,
    trigger_dt: datetime,
    timezone_name: str,
    recurrence: str,
    message: str | None,
    agent_prompt: str | None,
) -> dict[str, Any]:
    """Build a read-only Adaptive Card for the user to confirm or cancel."""
    from zoneinfo import ZoneInfo

    # Format trigger time in user's local timezone
    try:
        tz = ZoneInfo(timezone_name)
        local_time = trigger_dt.astimezone(tz)
    except (KeyError, ValueError):
        local_time = trigger_dt

    time_display = local_time.strftime("%b %d, %Y at %I:%M %p %Z")
    type_label = "Reminder" if reminder_type == "static" else "Scheduled Alert"
    recurrence_display = recurrence if recurrence != "none" else "One-time"

    # Build card body
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": f"📋 {type_label} Preview",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Title", "value": title},
                {"title": "When", "value": time_display},
                {"title": "Repeats", "value": recurrence_display},
                {"title": "Type", "value": type_label},
            ],
        },
    ]

    # Show content preview
    if reminder_type == "static" and message:
        body.append(
            {
                "type": "TextBlock",
                "text": f"**Message:** {message}",
                "wrap": True,
                "spacing": "Small",
            }
        )
    elif reminder_type == "agentic" and agent_prompt:
        body.append(
            {
                "type": "TextBlock",
                "text": f"**AI Prompt:** {agent_prompt}",
                "wrap": True,
                "spacing": "Small",
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": "Please confirm or cancel this reminder.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        }
    )

    # Metadata carried in both action paths
    metadata = {
        "reminder_id": reminder_id,
        "organisation_id": organisation_id,
    }

    # Build actions with Action.Execute + Action.Submit fallback
    confirm_fallback = {"action": "confirm_reminder_submit", **metadata}
    confirm_execute = dict(metadata)

    cancel_fallback = {"action": "confirm_reminder_cancel", **metadata}
    cancel_execute = dict(metadata)

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "confirm_reminder_submit",
            "data": confirm_execute,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "confirm_reminder_cancel",
            "data": cancel_execute,
            "fallback": {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": cancel_fallback,
            },
        },
    ]

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }

    return {
        "text": " ",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }


def _format_trigger_time(trigger_dt: datetime, timezone_name: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        local_dt = trigger_dt.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local_dt = trigger_dt
    return local_dt.strftime("%b %d, %Y at %I:%M %p %Z")


def _reminder_type_label(reminder_type: ReminderType) -> str:
    return "Reminder" if reminder_type == ReminderType.STATIC else "Scheduled Alert"


def _reminder_created_text(
    *,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
) -> str:
    type_label = _reminder_type_label(reminder_type)
    recurrence_display = recurrence.value if recurrence != ReminderRecurrence.NONE else "One-time"
    return (
        f"{type_label} created: {title}\n"
        f"When: {_format_trigger_time(trigger_time, timezone_name)}\n"
        f"Repeats: {recurrence_display}"
    )


def _reminder_success_facts(
    *,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
    delivery_channel: str,
) -> list[dict[str, str]]:
    return [
        {"title": "Title", "value": title},
        {"title": "When", "value": _format_trigger_time(trigger_time, timezone_name)},
        {
            "title": "Repeats",
            "value": recurrence.value if recurrence != ReminderRecurrence.NONE else "One-time",
        },
        {"title": "Type", "value": _reminder_type_label(reminder_type)},
        {"title": "Delivery", "value": delivery_channel.title()},
    ]


def _build_teams_success_payload(
    *,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
    delivery_channel: str,
) -> dict[str, Any]:
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{_reminder_type_label(reminder_type)} created",
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": _reminder_success_facts(
                    title=title,
                    reminder_type=reminder_type,
                    trigger_time=trigger_time,
                    timezone_name=timezone_name,
                    recurrence=recurrence,
                    delivery_channel=delivery_channel,
                ),
            },
        ],
    }
    text_fallback = _reminder_created_text(
        title=title,
        reminder_type=reminder_type,
        trigger_time=trigger_time,
        timezone_name=timezone_name,
        recurrence=recurrence,
    )
    return {
        "type": "message",
        "content": {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        },
        "text_fallback": text_fallback,
        "attachments": None,
        "source_text": None,
    }


def _build_slack_success_payload(
    *,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
    delivery_channel: str,
) -> dict[str, Any]:
    fields = [
        {"type": "mrkdwn", "text": f"*{fact['title']}*\n{_escape_slack_mrkdwn(fact['value'])}"}
        for fact in _reminder_success_facts(
            title=title,
            reminder_type=reminder_type,
            trigger_time=trigger_time,
            timezone_name=timezone_name,
            recurrence=recurrence,
            delivery_channel=delivery_channel,
        )
    ]
    text_fallback = _reminder_created_text(
        title=title,
        reminder_type=reminder_type,
        trigger_time=trigger_time,
        timezone_name=timezone_name,
        recurrence=recurrence,
    )
    return {
        "type": "message",
        "content": {
            "text": text_fallback,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "Reminder created"}},
                {"type": "section", "fields": fields},
            ],
        },
        "text_fallback": text_fallback,
        "attachments": None,
        "source_text": None,
    }


def _build_whatsapp_success_payload(
    *,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
    delivery_channel: str,
) -> dict[str, Any]:
    lines = ["Reminder created"]
    lines.extend(
        f"{fact['title']}: {fact['value']}"
        for fact in _reminder_success_facts(
            title=title,
            reminder_type=reminder_type,
            trigger_time=trigger_time,
            timezone_name=timezone_name,
            recurrence=recurrence,
            delivery_channel=delivery_channel,
        )
    )
    text = "\n".join(lines)
    return {
        "type": "message",
        "content": {"text": text},
        "text_fallback": text,
        "attachments": None,
        "source_text": None,
    }


def _build_ui_card_success_payload(
    *,
    reminder_id: str,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
    delivery_channel: str,
) -> dict[str, Any]:
    card = {
        "kind": "reminder_created_confirmation",
        "reminder_id": reminder_id,
        "title": title,
        "reminder_type": reminder_type.value,
        "trigger_time": trigger_time.isoformat(),
        "trigger_time_display": _format_trigger_time(trigger_time, timezone_name),
        "recurrence": recurrence.value,
        "delivery_channel": delivery_channel,
    }
    text_fallback = _reminder_created_text(
        title=title,
        reminder_type=reminder_type,
        trigger_time=trigger_time,
        timezone_name=timezone_name,
        recurrence=recurrence,
    )
    return {
        "type": "ui_card",
        "content": {"card": card},
        "text_fallback": text_fallback,
        "attachments": None,
        "source_text": None,
    }


def _build_reminder_created_channel_payload(
    *,
    context: AgentContext,
    reminder_id: str,
    title: str,
    reminder_type: ReminderType,
    trigger_time: datetime,
    timezone_name: str,
    recurrence: ReminderRecurrence,
    delivery_channel: str,
) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    kwargs = {
        "title": title,
        "reminder_type": reminder_type,
        "trigger_time": trigger_time,
        "timezone_name": timezone_name,
        "recurrence": recurrence,
        "delivery_channel": delivery_channel,
    }
    if channel == "teams":
        return _build_teams_success_payload(**kwargs)
    if channel == "slack":
        return _build_slack_success_payload(**kwargs)
    if channel == "whatsapp":
        return _build_whatsapp_success_payload(**kwargs)
    if channel in {"web", "voice"}:
        return _build_ui_card_success_payload(reminder_id=reminder_id, **kwargs)
    return None


async def create_reminder(
    *,
    context: AgentContext,
    title: str,
    trigger_time: str,
    reminder_type: str = "static",
    message: str | None = None,
    agent_prompt: str | None = None,
    deal_id: str | None = None,
    recurrence: str = "none",
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Create an ACTIVE UserReminder and schedule it immediately."""
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not user_id:
        return {"status": "error", "message": "Cannot create reminder: user not identified."}

    clean_title = _clean_text(title)
    if not clean_title or _is_generic_reminder_title(clean_title):
        return _missing_fields_error(
            "Please tell me what this reminder is about before I create it.",
            ["title"],
        )

    if not _clean_text(trigger_time):
        return _missing_fields_error(
            "Please tell me when this reminder should run.",
            ["trigger_time"],
        )

    try:
        r_type = ReminderType((reminder_type or "static").lower())
    except ValueError:
        return {
            "status": "error",
            "message": f"Invalid reminder type '{reminder_type}'. Use 'static' or 'agentic'.",
        }

    try:
        r_recurrence = ReminderRecurrence((recurrence or "none").lower())
    except ValueError:
        return {
            "status": "error",
            "message": (
                f"Invalid recurrence '{recurrence}'. "
                "Use: none, daily, weekdays, weekly, biweekly, monthly."
            ),
        }

    clean_message = _clean_text(message)
    clean_agent_prompt = _clean_text(agent_prompt)
    if r_type == ReminderType.STATIC and not clean_message:
        return _missing_fields_error(
            "Please tell me what message to send when the reminder fires.",
            ["message"],
        )
    if r_type == ReminderType.AGENTIC and not clean_agent_prompt:
        return _missing_fields_error(
            "Please provide the full instruction the agent should run at reminder time.",
            ["agent_prompt"],
        )

    effective_timezone = _resolve_effective_timezone(context, timezone_name)
    trigger_dt = _resolve_trigger_time(trigger_time, effective_timezone, context.metadata)
    if not trigger_dt:
        return {
            "status": "error",
            "message": (
                f"Could not understand the time '{trigger_time}'. "
                "Try something like 'in 10 minutes', 'at 3 PM', or '2026-02-24T15:00:00'."
            ),
            "missing_fields": ["trigger_time"],
        }

    if trigger_dt <= datetime.now(UTC):
        if r_recurrence != ReminderRecurrence.NONE:
            from src.workers.chat_worker.services.reminder_execution_service import (
                ReminderExecutionService,
            )

            trigger_dt = ReminderExecutionService._compute_next_trigger(
                trigger_dt, r_recurrence, effective_timezone
            )
        else:
            return {"status": "error", "message": "Trigger time must be in the future."}

    parsed_deal_id: UUID | None = None
    if deal_id:
        try:
            parsed_deal_id = UUID(deal_id)
        except ValueError:
            return {"status": "error", "message": f"Invalid deal_id '{deal_id}'."}

    delivery_target = await _resolve_reminder_delivery_target(context)
    if not delivery_target:
        return {
            "status": "error",
            "message": (
                "I couldn't find a delivery target for future reminders. "
                "Please connect Teams, Slack, or WhatsApp before creating reminders."
            ),
        }
    delivery_channel, delivery_context = delivery_target

    async with context.acquire_session() as session:
        service = ReminderService(session, organisation_id, user_id)
        try:
            detail = await service.create(
                title=clean_title,
                reminder_type=r_type,
                message_content=clean_message if r_type == ReminderType.STATIC else None,
                agent_prompt=clean_agent_prompt if r_type == ReminderType.AGENTIC else None,
                deal_id=parsed_deal_id,
                trigger_time=trigger_dt,
                timezone=effective_timezone,
                recurrence=r_recurrence,
                recurrence_end_at=None,
                delivery_channel=delivery_channel,
                delivery_context=delivery_context,
            )
        except ReminderServiceError as exc:
            await session.rollback()
            return {"status": "error", "message": exc.message}
        except Exception:
            await session.rollback()
            logger.exception("create_reminder: unexpected error")
            return {
                "status": "error",
                "message": "An unexpected error occurred while creating the reminder.",
            }

    result: dict[str, Any] = {
        "status": "created",
        "reminder_id": str(detail.reminder_id),
        "title": detail.title,
        "reminder_type": detail.reminder_type.value,
        "trigger_time": detail.next_trigger_at.isoformat(),
        "recurrence": detail.recurrence.value,
        "delivery_channel": detail.delivery_channel,
        "message": f"Created reminder **{detail.title}**.",
    }
    channel_payload = _build_reminder_created_channel_payload(
        context=context,
        reminder_id=str(detail.reminder_id),
        title=detail.title,
        reminder_type=detail.reminder_type,
        trigger_time=detail.next_trigger_at,
        timezone_name=detail.timezone,
        recurrence=detail.recurrence,
        delivery_channel=detail.delivery_channel,
    )
    if channel_payload:
        if channel_payload.get("type") == "ui_card":
            result["ui_card"] = channel_payload["content"]["card"]
        result[_BOT_CHANNEL_PAYLOAD_KEY] = channel_payload
    return result


async def create_reminder_form(
    *,
    context: AgentContext,
    title: str,
    trigger_time: str,
    reminder_type: str = "static",
    message: str | None = None,
    agent_prompt: str | None = None,
    deal_id: str | None = None,
    recurrence: str = "none",
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Create a UserReminder in pending_confirmation status and send a preview card.

    The reminder is NOT active until the user clicks Confirm on the preview card.
    """
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not user_id:
        return {"status": "error", "message": "Cannot create reminder: user not identified."}

    # Validate reminder_type
    try:
        r_type = ReminderType(reminder_type.lower())
    except ValueError:
        return {
            "status": "error",
            "message": f"Invalid reminder type '{reminder_type}'. Use 'static' or 'agentic'.",
        }

    # Validate recurrence
    try:
        r_recurrence = ReminderRecurrence(recurrence.lower())
    except ValueError:
        return {
            "status": "error",
            "message": (
                f"Invalid recurrence '{recurrence}'. "
                "Use: none, daily, weekdays, weekly, biweekly, monthly."
            ),
        }

    # Resolve trigger time server-side (hybrid: ISO 8601 → dateparser)
    effective_timezone = _resolve_effective_timezone(context, timezone_name)
    trigger_dt = _resolve_trigger_time(trigger_time, effective_timezone, context.metadata)
    if not trigger_dt:
        return {
            "status": "error",
            "message": (
                f"Could not understand the time '{trigger_time}'. "
                "Try something like 'in 10 minutes', 'at 3 PM', or '2026-02-24T15:00:00'."
            ),
        }

    # For recurring reminders, if resolved time is in the past, advance to next occurrence.
    # E.g. "daily at 10 AM" created at 6 PM → advance to tomorrow 10 AM.
    if trigger_dt <= datetime.now(UTC):
        if r_recurrence != ReminderRecurrence.NONE:
            from src.workers.chat_worker.services.reminder_execution_service import (
                ReminderExecutionService,
            )

            trigger_dt = ReminderExecutionService._compute_next_trigger(
                trigger_dt, r_recurrence, effective_timezone
            )
        else:
            return {"status": "error", "message": "Trigger time must be in the future."}

    # Validate content based on type
    if r_type == ReminderType.STATIC and not message:
        return {"status": "error", "message": "Static reminders require a message."}
    if r_type == ReminderType.AGENTIC and not agent_prompt:
        return {"status": "error", "message": "Agentic alerts require an agent_prompt."}

    channel = (context.channel or "teams").lower()

    # Extract delivery context from bot metadata
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")
    if channel == "slack":
        if not team_id or not channel_id:
            return {
                "status": "error",
                "message": (
                    "I couldn’t create the reminder because delivery context isn’t "
                    "available in this chat right now, so the system can’t send the "
                    "confirmation card."
                ),
            }
    elif not service_url or not conversation_id:
        return {
            "status": "error",
            "message": (
                "I couldn’t create the reminder because delivery context isn’t available "
                "in this chat right now, so the system can’t send the confirmation card."
            ),
        }

    # Build agent_config for agentic reminders
    agent_cfg: dict[str, Any] | None = None
    if r_type == ReminderType.AGENTIC:
        agent_cfg = {"prompt": agent_prompt}
        if deal_id:
            agent_cfg["deal_id"] = deal_id

    conversation_type = context.metadata.get("conversation_type", "")
    if channel == "slack":
        delivery_context = {
            "team_id": team_id,
            "channel_id": channel_id,
            "conversation_type": conversation_type or "personal",
        }
    else:
        teams_conversation_id = context.metadata.get("teams_conversation_id") or conversation_id
        delivery_context = {
            "service_url": service_url,
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "teams_conversation_id": teams_conversation_id,
        }

    # Create reminder in pending_confirmation — not active, not in ZSET
    reminder = UserReminder(
        organisation_id=organisation_id,
        user_id=user_id,
        reminder_type=r_type,
        title=title,
        message_content=message if r_type == ReminderType.STATIC else None,
        agent_config=agent_cfg,
        next_trigger_at=trigger_dt,
        user_timezone=effective_timezone,
        recurrence=r_recurrence,
        delivery_channel=channel,
        delivery_context=delivery_context,
        status=ReminderStatus.PENDING_CONFIRMATION,
    )

    async with context.acquire_session() as session:
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)

    # Build and send preview card
    card_payload = _build_preview_card(
        reminder_id=str(reminder.reminder_id),
        organisation_id=str(organisation_id),
        title=title,
        reminder_type=r_type.value,
        trigger_dt=trigger_dt,
        timezone_name=effective_timezone,
        recurrence=r_recurrence.value,
        message=message,
        agent_prompt=agent_prompt,
    )

    if channel == "slack":
        from zoneinfo import ZoneInfo

        try:
            local_dt = trigger_dt.astimezone(ZoneInfo(effective_timezone))
        except Exception:
            local_dt = trigger_dt

        type_label = "Reminder" if r_type == ReminderType.STATIC else "Scheduled Alert"
        recurrence_display = r_recurrence.value if r_recurrence.value != "none" else "One-time"
        content_label = "Message" if r_type == ReminderType.STATIC else "AI Prompt"
        content_value = message if r_type == ReminderType.STATIC else agent_prompt
        action_value = json.dumps(
            {
                "reminder_id": str(reminder.reminder_id),
                "organisation_id": str(organisation_id),
            },
            separators=(",", ":"),
        )

        slack_blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{type_label} Preview"},
            },
            {
                "type": "section",
                "fields": [
                    _slack_field("Title", title),
                    _slack_field("When", local_dt.strftime("%b %d, %Y at %I:%M %p %Z")),
                    _slack_field("Repeats", recurrence_display),
                    _slack_field("Type", type_label),
                ],
            },
        ]
        if content_value:
            slack_blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{content_label}*\n{_slack_quote(content_value)}",
                    },
                }
            )
        slack_blocks.extend(
            [
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Please confirm or cancel this reminder.",
                        }
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Confirm"},
                            "style": "primary",
                            "action_id": "confirm_reminder_submit",
                            "value": action_value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Cancel"},
                            "style": "danger",
                            "action_id": "confirm_reminder_cancel",
                            "value": action_value,
                        },
                    ],
                },
            ]
        )

        result = await OutboundChannelService().send_payload(
            OutboundDeliveryTarget(
                channel="slack",
                mapping_id=None,
                delivery_context={"team_id": team_id, "channel_id": channel_id},
            ),
            ChannelPayload(
                type="card",
                content={"blocks": slack_blocks},
                text_fallback=f"Reminder preview: {title}",
            ),
        )
        sent = result.success
    else:
        result = await OutboundChannelService().send_payload(
            OutboundDeliveryTarget(
                channel="teams",
                mapping_id=None,
                delivery_context={"service_url": service_url, "conversation_id": conversation_id},
            ),
            ChannelPayload(
                type="card",
                content=card_payload,
                text_fallback=f"Reminder preview: {title}",
            ),
        )
        sent = result.success

    if not sent:
        logger.error("Failed to send reminder preview card for reminder %s", reminder.reminder_id)
        return {"status": "error", "message": "Failed to send the preview card. Please try again."}

    return {
        "status": "preview_sent",
        "reminder_id": str(reminder.reminder_id),
        "message": (
            "I've sent a preview card for your reminder. "
            "Please review the details and click **Confirm** to activate it."
        ),
    }


def create_create_reminder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create_reminder tool bound to a context."""

    async def create_reminder_tool(
        title: str,
        trigger_time: str,
        reminder_type: str | None = "static",
        message: str | None = None,
        agent_prompt: str | None = None,
        deal_id: str | None = None,
        recurrence: str | None = None,
    ) -> dict[str, Any]:
        """Create a reminder or scheduled alert.

        Use this tool when the user asks you to remind them about something at a
        specific time, or to schedule an automated analysis/task for later.

        For STATIC reminders (simple notifications):
        - Set reminder_type="static"
        - Provide message with the text to deliver

        For AGENTIC alerts (AI tasks that run at the scheduled time):
        - Set reminder_type="agentic"
        - Provide agent_prompt with the instruction (e.g. "analyze deal Acme Corp")
        - Optionally provide deal_id if the analysis is deal-specific

        Pass the user's time expression exactly as spoken or typed in trigger_time.
        Do not convert it to UTC or rewrite local phrases. The server resolves
        the time automatically with the user's timezone. Examples:
        - "in 5 minutes"
        - "10 AM"
        - "12 AM"
        - "tomorrow at 9 AM"
        - "2026-02-24T15:00:00"

        The tool creates the reminder directly once required fields are known.
        Ask a follow-up only when the user has not provided reminder content,
        a trigger time, or a meaningful non-placeholder title.

        Args:
            title: Meaningful short label (e.g. "Follow up with Acme Corp")
            trigger_time: Time expression — natural language or ISO 8601
            reminder_type: "static" for a message, "agentic" for an AI task
            message: Text to deliver (required for static)
            agent_prompt: Instruction for the agent (required for agentic)
            deal_id: Optional deal UUID for deal-scoped agentic alerts
            recurrence: "none", "daily", "weekdays", "weekly", "biweekly", "monthly"
        """
        logger.info(
            "[TOOL CALLED] create_reminder - title='%s', type=%s, trigger=%s",
            title,
            reminder_type,
            trigger_time,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateReminder",
            tool_func=create_reminder,
            parameters={
                "context": context,
                "title": title,
                "trigger_time": trigger_time,
                "reminder_type": reminder_type or "static",
                "message": message,
                "agent_prompt": agent_prompt,
                "deal_id": deal_id,
                "recurrence": recurrence or "none",
            },
            description="Creating reminder",
            fallback_value={"status": "error", "message": "Failed to create reminder."},
        )

        logger.info("[TOOL RESULT] create_reminder status=%s", result.get("status"))
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return create_reminder_tool
