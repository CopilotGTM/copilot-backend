"""Create Task tool — lets the GeneralAgent create a CRM task from natural language.

The LLM extracts the user's intent (title, description, priority, due date, deal)
and passes it to this tool. The tool resolves the due date server-side, optionally
resolves a deal by name, and sends a preview card for the user to confirm or cancel.

Actual task creation happens in ConfirmTaskFormHandler when the user clicks Confirm.
No database row is created until confirmation — all state lives in the card payload.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- user_id: Scopes task ownership

This tool supports Teams and Slack chats.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# Priority display labels and emoji indicators for preview cards
_PRIORITY_LABELS = {
    "LOW": "Low",
    "MEDIUM": "Medium",
    "HIGH": "High",
    "URGENT": "Urgent",
}

# Slack emoji shortcodes for priority indicators
_PRIORITY_EMOJI = {
    "LOW": ":white_circle:",
    "MEDIUM": ":large_yellow_circle:",
    "HIGH": ":large_orange_circle:",
    "URGENT": ":red_circle:",
}

_VALID_PRIORITIES = set(_PRIORITY_LABELS.keys())

_VALID_REMINDER_OPTIONS = {"at_due", "15m_before", "1h_before", "1d_before"}

_REMINDER_LABELS = {
    "at_due": "At due time",
    "15m_before": "15 minutes before",
    "1h_before": "1 hour before",
    "1d_before": "1 day before",
}


def _escape_slack_mrkdwn(value: str) -> str:
    """Escape special characters for Slack mrkdwn."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MONTH_TYPOS: dict[str, str] = {
    "jan": "january",
    "feb": "february",
    "mrach": "march",
    "mach": "march",
    "aprl": "april",
    "apr": "april",
    "jne": "june",
    "jun": "june",
    "jly": "july",
    "jul": "july",
    "agust": "august",
    "aug": "august",
    "sept": "september",
    "sep": "september",
    "octber": "october",
    "oct": "october",
    "novmber": "november",
    "nov": "november",
    "decmber": "december",
    "dec": "december",
    "janury": "january",
    "febuary": "february",
    "feburary": "february",
    "marh": "march",
    "marcg": "march",
    "apri": "april",
    "agst": "august",
    "ocotber": "october",
    "noveber": "november",
    "deceber": "december",
    "septmber": "september",
}


def _fix_date_typos(expr: str) -> str:
    """Fix common month name misspellings in a date expression."""
    import re

    words = re.split(r"(\s+)", expr)
    for i, word in enumerate(words):
        replacement = _MONTH_TYPOS.get(word.lower().rstrip(".,;:"))
        if replacement:
            # Preserve original casing of first letter
            if word[0].isupper():
                replacement = replacement.capitalize()
            words[i] = replacement
    return "".join(words)


def _resolve_due_date(
    due_date_expr: str,
    timezone_name: str,
) -> datetime | None:
    """Parse a due date expression into a UTC datetime.

    Unlike reminder trigger times, due dates CAN be in the past (overdue tasks).
    Handles common typos like "mrach" → "march" before parsing.
    """
    from zoneinfo import ZoneInfo

    # Fix common month misspellings
    due_date_expr = _fix_date_typos(due_date_expr)

    # Fast path: ISO 8601
    try:
        dt = datetime.fromisoformat(due_date_expr)
        if dt.tzinfo is None:
            try:
                tz = ZoneInfo(timezone_name)
            except (KeyError, ValueError):
                tz = UTC
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        pass

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

        parsed = dateparser.parse(due_date_expr, settings=settings)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    except Exception:
        logger.debug("dateparser failed for due_date: %s", due_date_expr, exc_info=True)

    return None


async def _resolve_user_by_name(
    organisation_id: UUID,
    name_or_email: str,
) -> tuple[UUID, str] | None:
    """Find an org member by name or email.

    Returns (user_id, display_name) for the best match, or None.
    Queries the public.users table filtered by organisation_id.
    """
    from sqlalchemy import or_, select
    from sqlmodel import col

    from src.domain.models.master_tables.user_models import User
    from src.infra.database.sessions import acquire_master_async_session
    from src.service.crm.access.crm_user_service import build_user_display_name

    search = name_or_email.strip()
    if not search:
        return None

    async for session in acquire_master_async_session():
        # Try exact email match first
        if "@" in search:
            stmt = select(User).where(
                col(User.organisation_id) == organisation_id,
                col(User.email) == search.lower(),
            )
            result = await session.execute(stmt)
            user = result.scalars().first()
            if user:
                return (user.user_id, build_user_display_name(user) or search)
            return None

        # Name search — case-insensitive on first_name, last_name, or concat
        lower_search = search.lower()
        stmt = (
            select(User)
            .where(
                col(User.organisation_id) == organisation_id,
                or_(
                    col(User.first_name).ilike(f"%{lower_search}%"),
                    col(User.last_name).ilike(f"%{lower_search}%"),
                    col(User.email).ilike(f"%{lower_search}%"),
                ),
            )
            .limit(5)
        )
        result = await session.execute(stmt)
        users = result.scalars().all()

    if not users:
        return None

    if len(users) == 1:
        return (users[0].user_id, build_user_display_name(users[0]) or search)

    # Multiple matches — prefer exact first_name match, then first result
    for u in users:
        full = f"{u.first_name or ''} {u.last_name or ''}".strip().lower()
        if full == lower_search:
            return (u.user_id, build_user_display_name(u) or search)

    for u in users:
        if (u.first_name or "").lower() == lower_search:
            return (u.user_id, build_user_display_name(u) or search)

    return (users[0].user_id, build_user_display_name(users[0]) or search)


async def _resolve_deal_by_name(
    context: AgentContext,
    deal_name: str,
) -> tuple[UUID, str] | None:
    """Find a deal by fuzzy name match within the user's org.

    Returns (deal_id, deal_name) for the best match, or None.
    """
    from sqlalchemy import select
    from sqlmodel import col

    from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

    async with context.acquire_session() as session:
        # Case-insensitive search, limit to 5 candidates
        stmt = (
            select(CRMDeal)
            .where(
                col(CRMDeal.organisation_id) == context.organisation_id,
                col(CRMDeal.is_deleted) == False,  # noqa: E712
                col(CRMDeal.name).ilike(f"%{deal_name}%"),
            )
            .limit(5)
        )
        result = await session.execute(stmt)
        deals = result.scalars().all()

    if not deals:
        return None

    if len(deals) == 1:
        return (deals[0].deal_id, deals[0].name or deal_name)

    # Multiple matches — prefer exact (case-insensitive), then starts-with, then first
    lower_name = deal_name.lower()
    for deal in deals:
        if deal.name and deal.name.lower() == lower_name:
            return (deal.deal_id, deal.name)
    for deal in deals:
        if deal.name and deal.name.lower().startswith(lower_name):
            return (deal.deal_id, deal.name)

    return (deals[0].deal_id, deals[0].name or deal_name)


def _build_slack_preview(
    payload_json: str,
    title: str,
    priority_upper: str,
    priority_label: str,
    due_display: str | None,
    deal_display: str | None,
    assignee_display: str | None,
    description: str | None,
    reminder_label: str | None,
) -> list[dict[str, Any]]:
    """Build Slack Block Kit card — Option A "Clean Fields" layout.

    Layout:
      New Task                                      [header]
      *Create PPT for Plivo deal*                   [section — bold title]
      *Priority*        *Due Date*                  [section fields]
      🟠 High           Mar 15, 2026
      *Assigned To*     *Deal*                      [section fields]
      Aadish Sharma     Plivo -B2B Solutions
      ──────────                                    [divider]
      [Create Task] [Cancel]                        [actions]
    """
    priority_emoji = _PRIORITY_EMOJI.get(priority_upper, ":white_circle:")

    blocks: list[dict[str, Any]] = [
        # Header
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "New Task"},
        },
        # Task title — bold, normal-sized section text
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{_escape_slack_mrkdwn(title)}*",
            },
        },
        # Row 1: Priority + Due Date
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Priority*\n{priority_emoji} {priority_label}",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Due Date*\n{_escape_slack_mrkdwn(due_display)}"
                        if due_display
                        else "*Due Date*\nNot set"
                    ),
                },
            ],
        },
        # Row 2: Assigned To + Deal
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Assigned To*\n{_escape_slack_mrkdwn(assignee_display)}"
                        if assignee_display
                        else "*Assigned To*\nYou"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Deal*\n{_escape_slack_mrkdwn(deal_display)}"
                        if deal_display
                        else "*Deal*\nNone"
                    ),
                },
            ],
        },
    ]

    # Optional: Reminder + Description row
    if reminder_label or description:
        extra_fields = []
        if reminder_label:
            extra_fields.append({"type": "mrkdwn", "text": f"*Reminder*\n{reminder_label}"})
        if description:
            truncated = description[:200] + ("..." if len(description) > 200 else "")
            escaped = _escape_slack_mrkdwn(truncated)
            extra_fields.append({"type": "mrkdwn", "text": f"*Description*\n{escaped}"})
        blocks.append({"type": "section", "fields": extra_fields})

    # Divider + action buttons
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Create Task"},
                    "style": "primary",
                    "action_id": "confirm_task_submit",
                    "value": payload_json,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "confirm_task_cancel",
                    "value": payload_json,
                },
            ],
        }
    )

    return blocks


def _build_teams_preview(
    payload_json: str,
    title: str,
    priority_label: str,
    due_display: str | None,
    deal_display: str | None,
    assignee_display: str | None,
    description: str | None,
    reminder_label: str | None,
) -> dict[str, Any]:
    """Build a compact Teams Adaptive Card for task preview."""
    # Compact metadata line
    meta_parts = [priority_label]
    meta_parts.append(due_display if due_display else "No due date")
    meta_parts.append(assignee_display if assignee_display else "You")
    if deal_display:
        meta_parts.append(deal_display)
    if reminder_label:
        meta_parts.append(reminder_label)

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": title,
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Priority", "value": priority_label},
                                {"title": "Due", "value": due_display or "No due date"},
                                {"title": "Assigned To", "value": assignee_display or "You"},
                            ],
                        }
                    ],
                },
            ],
        },
    ]

    if deal_display:
        body[1]["columns"][0]["items"][0]["facts"].append({"title": "Deal", "value": deal_display})
    if reminder_label:
        body[1]["columns"][0]["items"][0]["facts"].append(
            {"title": "Reminder", "value": reminder_label}
        )

    if description:
        truncated = description[:300] + ("..." if len(description) > 300 else "")
        body.append(
            {
                "type": "TextBlock",
                "text": truncated,
                "wrap": True,
                "spacing": "Small",
                "isSubtle": True,
            }
        )

    # Both Confirm and Cancel carry the same payload; the handler
    # distinguishes them by action verb / action_id.
    confirm_data = {"action": "confirm_task_submit", "task_payload": payload_json}
    cancel_data = {"action": "confirm_task_cancel", "task_payload": payload_json}

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "confirm_task_submit",
            "data": confirm_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_data,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "confirm_task_cancel",
            "data": cancel_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": cancel_data,
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


async def create_task(
    *,
    context: AgentContext,
    title: str,
    description: str | None = None,
    priority: str = "MEDIUM",
    due_date: str | None = None,
    deal_name: str | None = None,
    deal_id: str | None = None,
    assign_to: str | None = None,
    reminder: str | None = None,
) -> dict[str, Any]:
    """Build a task preview card and send it for user confirmation.

    No DB row is created here — all data is embedded in the card payload
    and persisted only when the user clicks Confirm.
    """
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not user_id:
        return {"status": "error", "message": "Cannot create task: user not identified."}

    if not title or not title.strip():
        return {"status": "error", "message": "Task title is required."}
    title = title.strip()

    # Reject generic placeholder titles — the LLM should extract a real title
    generic_titles = {"task", "todo", "to-do", "new task", "a task", "the task"}
    if title.lower() in generic_titles:
        return {
            "status": "error",
            "message": (
                "The title is too generic. Ask the user what the task is about "
                "so you can provide a meaningful title."
            ),
        }

    # Validate priority
    priority_upper = priority.upper() if priority else "MEDIUM"
    if priority_upper not in _VALID_PRIORITIES:
        return {
            "status": "error",
            "message": (f"Invalid priority '{priority}'. Use: LOW, MEDIUM, HIGH, or URGENT."),
        }

    # Validate reminder option
    if reminder and reminder not in _VALID_REMINDER_OPTIONS:
        return {
            "status": "error",
            "message": (
                f"Invalid reminder option '{reminder}'. "
                "Use: at_due, 15m_before, 1h_before, or 1d_before."
            ),
        }

    channel = (context.channel or "teams").lower()
    timezone_name = context.metadata.get("user_timezone") or "UTC"

    # Resolve due date — default to tomorrow if not mentioned
    due_dt: datetime | None = None
    if due_date:
        due_dt = _resolve_due_date(due_date, timezone_name)
        if not due_dt:
            return {
                "status": "error",
                "message": (
                    f"Could not understand the date '{due_date}'. "
                    "Try something like 'Friday', 'March 20', or '2026-03-20T17:00:00'."
                ),
            }
    else:
        # No due date specified — default to tomorrow end of day in user's timezone
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(timezone_name)
        except (KeyError, ValueError):
            tz = UTC
        tomorrow_local = datetime.now(tz).replace(
            hour=17,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        due_dt = tomorrow_local.astimezone(UTC)

    # Resolve deal — explicit deal_id takes precedence over deal_name
    resolved_deal_id: str | None = None
    resolved_deal_name: str | None = None

    if deal_id:
        try:
            UUID(deal_id)
            resolved_deal_id = deal_id
        except ValueError:
            return {"status": "error", "message": f"Invalid deal_id '{deal_id}'."}
    elif deal_name:
        match = await _resolve_deal_by_name(context, deal_name.strip())
        if match:
            resolved_deal_id = str(match[0])
            resolved_deal_name = match[1]
        else:
            return {
                "status": "error",
                "message": (
                    f"Could not find a deal matching '{deal_name}'. "
                    "Please check the deal name and try again, or provide the exact deal name."
                ),
            }

    # Resolve assignee — name or email → user_id within the org
    resolved_assignee_id: str | None = None
    resolved_assignee_name: str | None = None

    if assign_to:
        match = await _resolve_user_by_name(organisation_id, assign_to.strip())
        if match:
            resolved_assignee_id = str(match[0])
            resolved_assignee_name = match[1]
        else:
            return {
                "status": "error",
                "message": (
                    f"Could not find a team member matching '{assign_to}'. "
                    "Please provide their full name or email address so I can look them up."
                ),
            }

    # Nonce prevents duplicate task creation from repeated button clicks
    from src.service.bots.form_handlers.nonce import create_nonce

    nonce = create_nonce("task_confirm")

    # Build the payload that will be embedded in the card button
    task_payload: dict[str, Any] = {
        "nonce": nonce,
        "title": title,
        "organisation_id": str(organisation_id),
    }
    if description:
        task_payload["description"] = description.strip()
    if priority_upper != "MEDIUM":
        task_payload["priority"] = priority_upper
    if due_dt:
        task_payload["due_date"] = due_dt.isoformat()
    if resolved_deal_id:
        task_payload["deal_id"] = resolved_deal_id
    if resolved_assignee_id:
        task_payload["assign_to_user_id"] = resolved_assignee_id
        task_payload["assign_to_name"] = resolved_assignee_name
    if reminder:
        task_payload["reminder"] = reminder
    task_payload["timezone"] = timezone_name

    # Delivery context so the form handler knows where to send the confirmation
    if channel == "slack":
        team_id = context.metadata.get("team_id", "")
        channel_id = context.metadata.get("channel_id", "")
        if not team_id or not channel_id:
            return {
                "status": "error",
                "message": "Delivery context not available in this chat.",
            }
        task_payload["delivery_channel"] = "slack"
        task_payload["delivery_team_id"] = team_id
        task_payload["delivery_channel_id"] = channel_id
    else:
        service_url = context.metadata.get("service_url", "")
        conversation_id = context.metadata.get("conversation_id", "")
        if not service_url or not conversation_id:
            return {
                "status": "error",
                "message": "Delivery context not available in this chat.",
            }
        task_payload["delivery_channel"] = "teams"

    payload_json = json.dumps(task_payload, separators=(",", ":"))

    # Format display values for preview card
    priority_label = _PRIORITY_LABELS.get(priority_upper, "Medium")
    due_display: str | None = None
    if due_dt:
        from zoneinfo import ZoneInfo

        try:
            local_dt = due_dt.astimezone(ZoneInfo(timezone_name))
        except Exception:
            local_dt = due_dt
        # Show date-only when time is midnight (user said "March 15", not "March 15 at 3pm")
        if local_dt.hour == 0 and local_dt.minute == 0:
            due_display = local_dt.strftime("%b %d, %Y")
        else:
            due_display = local_dt.strftime("%b %d, %Y at %I:%M %p %Z").strip()

    deal_display = resolved_deal_name or (f"ID: {resolved_deal_id}" if resolved_deal_id else None)
    assignee_display = resolved_assignee_name
    reminder_label = _REMINDER_LABELS.get(reminder) if reminder else None

    # Send preview card
    if channel == "slack":
        from src.service.bots.slack_bot_service import SlackBotService

        blocks = _build_slack_preview(
            payload_json=payload_json,
            title=title,
            priority_upper=priority_upper,
            priority_label=priority_label,
            due_display=due_display,
            deal_display=deal_display,
            assignee_display=assignee_display,
            description=description,
            reminder_label=reminder_label,
        )

        bot_service = SlackBotService()
        sent = await bot_service.send_block_kit_message(
            team_id=context.metadata.get("team_id", ""),
            channel_id=context.metadata.get("channel_id", ""),
            blocks=blocks,
            text_fallback=f"Task preview: {title}",
        )
    else:
        from src.service.bots.teams_bot_service import TeamsBotService

        card_payload = _build_teams_preview(
            payload_json=payload_json,
            title=title,
            priority_label=priority_label,
            due_display=due_display,
            deal_display=deal_display,
            assignee_display=assignee_display,
            description=description,
            reminder_label=reminder_label,
        )

        bot_service = TeamsBotService()
        sent = await bot_service.send_proactive_payload(
            service_url=context.metadata.get("service_url", ""),
            conversation_id=context.metadata.get("conversation_id", ""),
            payload=card_payload,
            text_fallback=f"Task preview: {title}",
        )

    if not sent:
        logger.error("Failed to send task preview card")
        return {"status": "error", "message": "Failed to send the preview card. Please try again."}

    return {
        "status": "preview_sent",
        "message": (
            "I've sent a preview card for your task. "
            "Please review the details and click **Confirm** to create it."
        ),
    }


def create_create_task(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create_task tool bound to a context."""

    async def create_task_tool(
        title: str,
        description: str | None = None,
        priority: str | None = "MEDIUM",
        due_date: str | None = None,
        deal_name: str | None = None,
        deal_id: str | None = None,
        assign_to: str | None = None,
        reminder: str | None = None,
    ) -> dict[str, Any]:
        """Create a task or to-do item.

        Call this tool IMMEDIATELY when the user asks to create a task, add a
        to-do, or track an action item. Do NOT ask the user for missing fields —
        extract what you can from their message and let the defaults handle the rest.
        The user will see a preview card and can cancel if anything is wrong.

        IMPORTANT: The title must describe the actual work. Never use generic titles
        like "Task" or "New task". If the user's message does not describe what needs
        to be done, ask them before calling this tool.

        Examples:
        - "Create a task to follow up with Acme" → title="Follow up with Acme"
        - "Add a task: send proposal by Friday, high priority" →
          title="Send proposal", due_date="Friday", priority="HIGH"
        - "Todo: update CRM notes" → title="Update CRM notes"
        - "Create a task for the Globex deal to review contract" →
          title="Review contract", deal_name="Globex"
        - "Assign a task to Mrinmoy to review the deck" →
          title="Review the deck", assign_to="Mrinmoy"
        - "Create a task for John to send the proposal by Friday" →
          title="Send the proposal", assign_to="John", due_date="Friday"
        - "Create task due tomorrow assigned to Mrinmoy to create PPT for Plivo deal" →
          title="Create PPT for Plivo deal", due_date="tomorrow",
          assign_to="Mrinmoy", deal_name="Plivo"
        - "Create a task and assign it to Mrinmoy" → DO NOT call this tool yet.
          Ask: "What should the task be about?" (no title to extract)

        Args:
            title: Short description of what needs to be done (required).
                Extract this from the user's message. Be concise.
            description: Additional details if the user provided context beyond
                the title. Omit if not mentioned — do NOT ask for it.
            priority: LOW, MEDIUM (default), HIGH, or URGENT. Only set if the
                user explicitly mentions priority or urgency. Default is MEDIUM.
            due_date: When it's due. Fix any obvious typos/misspellings before
                passing (e.g. "mrach" → "March", "firday" → "Friday").
                Pass a clean date expression. Examples: "Friday", "March 20",
                "tomorrow", "end of week", "2026-03-20T17:00:00".
                Omit if user did not mention a date.
            deal_name: If the user mentions a deal by name, pass it for lookup.
                Only pass if the user explicitly mentions a deal.
            deal_id: If you already know the deal UUID from context (e.g.
                deal-scoped chat), pass it directly. Takes precedence over deal_name.
            assign_to: Name or email of the person to assign this task to.
                Pass exactly what the user said — the system resolves it to an
                org member. If not mentioned, defaults to the current user.
                Examples: "Mrinmoy", "john@company.com", "Sarah Chen".
            reminder: Set only if the user explicitly asks for a reminder.
                Options: "at_due", "15m_before", "1h_before", "1d_before".
                Requires due_date to be set.
        """
        logger.info(
            "[TOOL CALLED] create_task - title='%s', priority=%s, due=%s, assign=%s",
            title,
            priority,
            due_date,
            assign_to,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateTask",
            tool_func=create_task,
            parameters={
                "context": context,
                "title": title,
                "description": description,
                "priority": priority or "MEDIUM",
                "due_date": due_date,
                "deal_name": deal_name,
                "deal_id": deal_id,
                "assign_to": assign_to,
                "reminder": reminder,
            },
            description="Creating task",
            fallback_value={"status": "error", "message": "Failed to create task."},
        )

        logger.info("[TOOL RESULT] create_task status=%s", result.get("status"))
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return create_task_tool
