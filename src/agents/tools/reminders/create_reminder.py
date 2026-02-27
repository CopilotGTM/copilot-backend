"""Create Reminder tool — lets the GeneralAgent create a scheduled reminder or agentic alert.

The LLM extracts the user's intent (title, time expression, type) and passes it
to this tool. The tool resolves the time server-side (hybrid: ISO 8601 first,
then dateparser for natural language), creates a DB row in pending_confirmation
status, and sends a preview Adaptive Card for the user to confirm or cancel.

Confirmation is handled by ConfirmReminderFormHandler, which flips the status
to active and ZADD's it into the Redis sorted set.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- user_id: Scopes reminder ownership

This is a Teams-channel-only tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
    UserReminder,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


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
) -> datetime | None:
    """Parse a time expression into a UTC datetime.

    Tries ISO 8601 first (fast path when LLM sends structured time),
    then falls back to dateparser for natural language ("in 5 minutes",
    "at 3 PM", "tomorrow 9 AM").
    """
    from zoneinfo import ZoneInfo

    # Fast path: ISO 8601
    try:
        dt = datetime.fromisoformat(trigger_time_expr)
        if dt.tzinfo is None:
            try:
                tz = ZoneInfo(timezone_name)
            except (KeyError, ValueError):
                tz = UTC
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        pass

    # Strip recurrence prefixes that confuse dateparser
    cleaned_expr = _strip_recurrence_prefix(trigger_time_expr)

    # Slow path: natural language via dateparser
    try:
        import dateparser

        settings: dict[str, Any] = {
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        }
        if timezone_name and timezone_name != "UTC":
            settings["TIMEZONE"] = timezone_name
            settings["TO_TIMEZONE"] = "UTC"

        parsed = dateparser.parse(cleaned_expr, settings=settings)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    except Exception:
        logger.debug("dateparser failed for: %s", trigger_time_expr, exc_info=True)

    return None


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
    timezone_name: str = "UTC",
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
    trigger_dt = _resolve_trigger_time(trigger_time, timezone_name)
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
                trigger_dt, r_recurrence, timezone_name
            )
        else:
            return {"status": "error", "message": "Trigger time must be in the future."}

    # Validate content based on type
    if r_type == ReminderType.STATIC and not message:
        return {"status": "error", "message": "Static reminders require a message."}
    if r_type == ReminderType.AGENTIC and not agent_prompt:
        return {"status": "error", "message": "Agentic alerts require an agent_prompt."}

    # Extract delivery context from bot metadata
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    if not service_url or not conversation_id:
        return {
            "status": "error",
            "message": "Cannot create reminder: missing Teams delivery context.",
        }

    # Build agent_config for agentic reminders
    agent_cfg: dict[str, Any] | None = None
    if r_type == ReminderType.AGENTIC:
        agent_cfg = {"prompt": agent_prompt}
        if deal_id:
            agent_cfg["deal_id"] = deal_id

    delivery_context = {
        "service_url": service_url,
        "conversation_id": conversation_id,
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
        user_timezone=timezone_name,
        recurrence=r_recurrence,
        delivery_channel="teams",
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
        timezone_name=timezone_name,
        recurrence=r_recurrence.value,
        message=message,
        agent_prompt=agent_prompt,
    )

    from src.service.bots.teams_bot_service import TeamsBotService

    bot_service = TeamsBotService()
    sent = await bot_service.send_proactive_payload(
        service_url=service_url,
        conversation_id=conversation_id,
        payload=card_payload,
        text_fallback=f"Reminder preview: {title}",
    )

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

        Pass the user's time expression exactly as they said it in trigger_time.
        The server resolves the time automatically. Examples:
        - "in 5 minutes"
        - "at 3 PM"
        - "tomorrow at 9 AM"
        - "2026-02-24T15:00:00"

        The tool sends a preview card to the user for confirmation.

        Args:
            title: Short label (e.g. "Follow up with Acme Corp")
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

        # Resolve timezone from context metadata (Teams provides it)
        tz = context.metadata.get("user_timezone") or "UTC"

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
                "timezone_name": tz,
            },
            description="Creating reminder",
            fallback_value={"status": "error", "message": "Failed to create reminder."},
        )

        logger.info("[TOOL RESULT] create_reminder status=%s", result.get("status"))
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return create_reminder_tool
