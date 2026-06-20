"""Create Task tool and legacy confirmation-card fallback.

The LLM extracts the user's intent (title, description, priority, due date, deal)
and passes it to this tool. The normal agent path creates the CRM task directly
once the title is known. The preview-card helpers remain for previously sent
Teams and Slack cards, whose submissions are still handled by
ConfirmTaskFormHandler in src/service/bots/form_handlers/.

Legacy preview mode does not create a DB row until confirmation.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- user_id: Scopes task ownership

This tool supports Teams, Slack, WhatsApp, Web, and Voice.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.adapters import ChannelPayload
from src.agents.tools.base import execute_with_capabilities
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import get_context_anchor_local, resolve_timezone_name
from src.service.channels import OutboundChannelService, OutboundDeliveryTarget

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"

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

_PLACEHOLDER_TASK_TITLES = {
    "task",
    "todo",
    "to-do",
    "new task",
    "a task",
    "the task",
    "create task",
    "create a task",
    "add task",
    "add a task",
    "assign it to me",
    "assign this to me",
    "create a task and assign it to me",
    "test task",
}

_REMINDER_OFFSETS = {
    "at_due": timedelta(0),
    "15m_before": timedelta(minutes=15),
    "1h_before": timedelta(hours=1),
    "1d_before": timedelta(days=1),
}


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _load_tasks_write_service() -> tuple[type[Any], type[Exception]]:
    from src.service.crm.tasks.contracts import TaskWriteServiceError
    from src.service.crm.tasks.write_service import TaskWriteService

    return TaskWriteService, TaskWriteServiceError


def _task_title_missing_message() -> str:
    return (
        "The task title is too generic. What should the task be about? "
        "Please provide a meaningful task title before I create it."
    )


def _is_placeholder_task_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title.strip().lower()).strip(" .!?")
    return normalized in _PLACEHOLDER_TASK_TITLES


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
    metadata: dict[str, Any] | None = None,
) -> datetime | None:
    """Parse a due date expression into a UTC datetime.

    Unlike reminder trigger times, due dates CAN be in the past (overdue tasks).
    Handles common typos like "mrach" → "march" before parsing.
    """
    # Fix common month misspellings
    due_date_expr = _fix_date_typos(due_date_expr)
    parsed = parse_agent_datetime(
        due_date_expr,
        timezone_name=timezone_name,
        metadata=metadata,
    )
    if parsed is None:
        logger.debug("dateparser failed for due_date: %s", due_date_expr)
    return parsed


async def _resolve_user_by_name(
    organisation_id: UUID,
    name_or_email: str,
) -> tuple[UUID, str] | None:
    """Find an org member by name or email.

    Returns (user_id, display_name) for the best match, or None.
    Queries the public.users table filtered by organisation_id.
    """
    from sqlalchemy import func, or_, select
    from sqlmodel import col

    from src.domain.models.master_tables.user_models import User
    from src.infra.database.sessions import acquire_master_async_session
    from src.service.crm.access.crm_user_service import build_user_display_name

    search = name_or_email.strip()
    if not search:
        return None

    def same_org(user: Any) -> bool:
        return getattr(user, "organisation_id", None) == organisation_id

    def display_name(user: Any) -> str:
        return build_user_display_name(user) or str(getattr(user, "email", search))

    def normalized_full_name(user: Any) -> str:
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        return f"{first} {last}".strip().lower()

    async for session in acquire_master_async_session():
        # Try exact email match first
        if "@" in search:
            stmt = select(User).where(
                col(User.organisation_id) == organisation_id,
                col(User.email) == search.lower(),
            )
            result = await session.execute(stmt)
            user = result.scalars().first()
            if user and same_org(user):
                return (user.user_id, display_name(user))
            return None

        # Name search is token-aware so "Nikhil Singhal" can match separate
        # first_name/last_name columns instead of searching the full phrase in
        # each column independently.
        lower_search = search.lower()
        tokens = [token for token in re.split(r"\s+", lower_search) if token]
        full_name_expr = func.concat(col(User.first_name), " ", col(User.last_name))
        conditions: list[Any] = [
            full_name_expr.ilike(f"%{lower_search}%"),
            col(User.first_name).ilike(f"%{lower_search}%"),
            col(User.last_name).ilike(f"%{lower_search}%"),
            col(User.email).ilike(f"%{lower_search}%"),
        ]
        for token in tokens:
            conditions.extend(
                [
                    col(User.first_name).ilike(f"%{token}%"),
                    col(User.last_name).ilike(f"%{token}%"),
                    col(User.email).ilike(f"%{token}%"),
                ]
            )

        stmt = (
            select(User)
            .where(
                col(User.organisation_id) == organisation_id,
                or_(*conditions),
            )
            .limit(10)
        )
        result = await session.execute(stmt)
        users = [user for user in result.scalars().all() if same_org(user)]

    if not users:
        return None

    # Full names are unambiguous when the user provides both first and last name.
    for u in users:
        if normalized_full_name(u) == lower_search:
            return (u.user_id, display_name(u))

    if len(tokens) >= 2:
        first_token = tokens[0]
        last_token = tokens[-1]
        ordered_matches = [
            u
            for u in users
            if (getattr(u, "first_name", "") or "").lower().startswith(first_token)
            and (getattr(u, "last_name", "") or "").lower().startswith(last_token)
        ]
        if len(ordered_matches) == 1:
            match = ordered_matches[0]
            return (match.user_id, display_name(match))
        return None

    if len(users) == 1:
        return (users[0].user_id, display_name(users[0]))

    # A partial name with multiple candidates is unsafe for assignment. Ask for
    # an email or full name instead of silently assigning to the first result.
    for u in users:
        if (getattr(u, "email", "") or "").lower() == lower_search:
            return (u.user_id, display_name(u))

    return None


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


def _task_created_text_fallback(task: Any) -> str:
    return f"Created task {task.subject or 'Untitled task'}."


def _format_status(value: str | None) -> str:
    return (value or "NOT_STARTED").replace("_", " ").capitalize()


def _task_success_facts(task: Any) -> list[dict[str, str]]:
    facts = [
        {"title": "Title", "value": str(task.subject or "Untitled task")},
        {"title": "Priority", "value": _PRIORITY_LABELS.get(str(task.priority), "Medium")},
        {"title": "Status", "value": _format_status(getattr(task, "status", None))},
    ]
    if getattr(task, "due_date", None):
        facts.append({"title": "Due", "value": task.due_date.isoformat()})
    if getattr(task, "related_name", None):
        facts.append({"title": "Deal", "value": str(task.related_name)})
    return facts


def _task_success_ui_facts(task: Any) -> list[dict[str, str]]:
    return [{"label": fact["title"], "value": fact["value"]} for fact in _task_success_facts(task)]


def _build_task_created_ui_card(task: Any) -> dict[str, Any]:
    return {
        "kind": "task_created_confirmation",
        "version": "1.0",
        "tone": "success",
        "title": "Task created",
        "message": _task_created_text_fallback(task),
        "entity": {
            "type": "task",
            "id": str(task.activity_id),
            "name": str(task.subject or "Untitled task"),
        },
        "facts": _task_success_ui_facts(task),
        "actions": [],
    }


def _build_teams_success_payload(task: Any) -> dict[str, Any]:
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Task created",
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
                "color": "Good",
            },
            {"type": "FactSet", "facts": _task_success_facts(task)},
        ],
    }
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
        "text_fallback": _task_created_text_fallback(task),
        "attachments": None,
        "source_text": None,
    }


def _build_slack_success_payload(task: Any) -> dict[str, Any]:
    fields = [
        {
            "type": "mrkdwn",
            "text": f"*{fact['title']}*\n{_escape_slack_mrkdwn(fact['value'])}",
        }
        for fact in _task_success_facts(task)
    ]
    return {
        "type": "message",
        "content": {
            "text": _task_created_text_fallback(task),
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Task created"},
                },
                {"type": "section", "fields": fields},
            ],
        },
        "text_fallback": _task_created_text_fallback(task),
        "attachments": None,
        "source_text": None,
    }


def _build_whatsapp_success_payload(task: Any) -> dict[str, Any]:
    lines = ["Task created"]
    lines.extend(f"{fact['title']}: {fact['value']}" for fact in _task_success_facts(task))
    return {
        "type": "message",
        "content": {"text": "\n".join(lines)},
        "text_fallback": _task_created_text_fallback(task),
        "attachments": None,
        "source_text": None,
    }


def _build_ui_card_success_payload(task: Any) -> dict[str, Any]:
    return {
        "type": "ui_card",
        "content": {"card": _build_task_created_ui_card(task)},
        "text_fallback": _task_created_text_fallback(task),
        "attachments": None,
        "source_text": None,
    }


def _build_task_created_channel_payload(context: AgentContext, task: Any) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    if channel == "slack":
        return _build_slack_success_payload(task)
    if channel == "teams":
        return _build_teams_success_payload(task)
    if channel == "whatsapp":
        return _build_whatsapp_success_payload(task)
    if channel in {"web", "voice"}:
        return _build_ui_card_success_payload(task)
    return None


def _created_task_result(task: Any, *, assignee_name: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "created",
        "task_id": str(task.activity_id),
        "title": task.subject,
        "priority": task.priority,
        "message": f"Created task **{task.subject}**.",
    }
    if getattr(task, "due_date", None):
        result["due_date"] = task.due_date.isoformat()
    if getattr(task, "related_name", None):
        result["deal_name"] = task.related_name
    if assignee_name:
        result["assignee_name"] = assignee_name
    return result


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
    """Create a CRM task directly through TaskWriteService."""
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not user_id:
        return {"status": "error", "message": "Cannot create task: user not identified."}

    if not title or not title.strip():
        return {"status": "error", "message": "Task title is required."}
    title = title.strip()

    if _is_placeholder_task_title(title):
        return {
            "status": "error",
            "message": _task_title_missing_message(),
        }

    priority_upper = priority.upper() if priority else "MEDIUM"
    if priority_upper not in _VALID_PRIORITIES:
        return {
            "status": "error",
            "message": (f"Invalid priority '{priority}'. Use: LOW, MEDIUM, HIGH, or URGENT."),
        }

    if reminder and reminder not in _VALID_REMINDER_OPTIONS:
        return {
            "status": "error",
            "message": (
                f"Invalid reminder option '{reminder}'. "
                "Use: at_due, 15m_before, 1h_before, or 1d_before."
            ),
        }
    if reminder and not due_date:
        return {
            "status": "error",
            "message": "A due date is required before creating a task-linked reminder.",
        }

    metadata = context.metadata or {}
    metadata_timezone = metadata.get("user_timezone")
    timezone_name = (
        resolve_timezone_name(
            metadata_timezone if isinstance(metadata_timezone, str) else None,
            default="UTC",
        )
        or "UTC"
    )
    due_dt: datetime | None = None
    if due_date:
        due_dt = _resolve_due_date(due_date, timezone_name, metadata)
        if not due_dt:
            return {
                "status": "error",
                "message": (
                    f"Could not understand the date '{due_date}'. "
                    "Try something like 'Friday', 'March 20', or '2026-03-20T17:00:00'."
                ),
            }

    resolved_deal_id: UUID | None = None
    if deal_id:
        try:
            resolved_deal_id = UUID(deal_id)
        except ValueError:
            return {"status": "error", "message": f"Invalid deal_id '{deal_id}'."}
    elif deal_name:
        match = await _resolve_deal_by_name(context, deal_name.strip())
        if match:
            resolved_deal_id = match[0]
        else:
            return {
                "status": "error",
                "message": (
                    f"Could not find a deal matching '{deal_name}'. "
                    "Please check the deal name and try again, or provide the exact deal name."
                ),
            }

    owner_user_id = user_id
    assignee_name: str | None = None
    if assign_to:
        match = await _resolve_user_by_name(organisation_id, assign_to.strip())
        if match:
            owner_user_id = match[0]
            assignee_name = match[1]
        else:
            return {
                "status": "error",
                "message": (
                    f"Could not find a team member matching '{assign_to}'. "
                    "Please provide their full name or email address so I can look them up."
                ),
            }

    write_payload: dict[str, Any] = {
        "subject": title,
        "owner_user_id": owner_user_id,
        "priority": priority_upper,
        "status": "NOT_STARTED",
        "created_via": "agent_create_task",
    }
    if description := _clean_optional(description):
        write_payload["body"] = description
    if due_dt:
        write_payload["due_date"] = due_dt
    if reminder and due_dt:
        trigger_time = due_dt - _REMINDER_OFFSETS[reminder]
        if trigger_time > datetime.now(UTC):
            reminder_cfg: dict[str, Any] = {
                "enabled": True,
                "trigger_time": trigger_time,
                "recurrence": "NONE",
                "timezone": timezone_name,
            }
            channel = (context.channel or "").lower()
            if channel in {"teams", "slack", "whatsapp"}:
                reminder_cfg["delivery_channel"] = channel
            write_payload["reminder"] = reminder_cfg

    service_cls, service_error_cls = _load_tasks_write_service()

    async with context.acquire_session() as session:
        service = service_cls(session, organisation_id, user_id)
        try:
            task = await service.create_task(write_payload, deal_id=resolved_deal_id)
            await session.commit()
            result = _created_task_result(task, assignee_name=assignee_name)
            channel_payload = _build_task_created_channel_payload(context, task)
            if channel_payload:
                if channel_payload.get("type") == "ui_card":
                    result["ui_card"] = channel_payload["content"]["card"]
                result[_BOT_CHANNEL_PAYLOAD_KEY] = channel_payload
            return result
        except service_error_cls as exc:
            await session.rollback()
            message = str(getattr(exc, "message", str(exc)))
            logger.warning("create_task: TasksWriteServiceError: %s", message)
            return {
                "status": "error",
                "message": f"Could not create task: {message}",
            }
        except Exception:
            await session.rollback()
            logger.exception("create_task: unexpected error")
            return {
                "status": "error",
                "message": "An unexpected error occurred while creating the task.",
            }


async def create_task_form(
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
    if _is_placeholder_task_title(title):
        return {
            "status": "error",
            "message": _task_title_missing_message(),
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
    metadata = context.metadata or {}
    metadata_timezone = metadata.get("user_timezone")
    timezone_name = (
        resolve_timezone_name(
            metadata_timezone if isinstance(metadata_timezone, str) else None,
            default="UTC",
        )
        or "UTC"
    )

    # Resolve due date — default to tomorrow if not mentioned
    due_dt: datetime | None = None
    if due_date:
        due_dt = _resolve_due_date(due_date, timezone_name, metadata)
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
        tomorrow_local = get_context_anchor_local(
            {**metadata, "user_timezone": timezone_name}
        ).replace(
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

        result = await OutboundChannelService().send_payload(
            OutboundDeliveryTarget(
                channel="slack",
                mapping_id=None,
                delivery_context={
                    "team_id": context.metadata.get("team_id", ""),
                    "channel_id": context.metadata.get("channel_id", ""),
                },
            ),
            ChannelPayload(
                type="card",
                content={"blocks": blocks},
                text_fallback=f"Task preview: {title}",
            ),
        )
        sent = result.success
    else:
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

        teams_service_url = context.metadata.get("service_url", "")
        teams_conversation_id = context.metadata.get("conversation_id", "")
        logger.info(
            "Sending Teams task preview: service_url=%s, conversation_id=%s",
            bool(teams_service_url),
            bool(teams_conversation_id),
        )
        result = await OutboundChannelService().send_payload(
            OutboundDeliveryTarget(
                channel="teams",
                mapping_id=None,
                delivery_context={
                    "service_url": teams_service_url,
                    "conversation_id": teams_conversation_id,
                },
            ),
            ChannelPayload(
                type="card",
                content=card_payload,
                text_fallback=f"Task preview: {title}",
            ),
        )
        sent = result.success

    if not sent:
        logger.error("Failed to send task preview card (channel=%s)", channel)
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
        """Create a task or to-do item directly.

        Call this tool IMMEDIATELY when the user asks to create a task, add a
        to-do, or track an action item. Do NOT ask the user for missing fields —
        extract what you can from their message and let the defaults handle the rest.
        The task is created directly once a meaningful title is known.

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
        return (
            _public_tool_result(dict(result))
            if result
            else {"status": "error", "message": "Unknown error."}
        )

    return create_task_tool
