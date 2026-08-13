"""Update static reminders from GeneralAgent chat workflows.

Ownership comes from ``AgentContext`` and the service enforces the static type
boundary so legacy scheduled-agent rows are not exposed through this tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.reminders.create_reminder import (
    _clean_text,
    _resolve_effective_timezone,
    _resolve_trigger_time,
)
from src.domain.models.organisation_tables.reminder_models import ReminderRecurrence, ReminderType
from src.service.reminders.reminder_service import ReminderService, ReminderServiceError

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def update_reminder(
    *,
    context: AgentContext,
    reminder_id: str,
    title: str | None = None,
    trigger_time: str | None = None,
    message: str | None = None,
    recurrence: str | None = None,
) -> dict[str, Any]:
    """Update a static reminder owned by the user in ``context``.

    Ownership is immutable in ``context``. The reminder ID must come from
    ``list_reminders`` rather than model inference.
    """
    if not context.user_id:
        return {"status": "error", "message": "Cannot update reminder: user not identified."}
    try:
        parsed_reminder_id = UUID(reminder_id)
    except ValueError:
        return {"status": "error", "message": f"Invalid reminder_id '{reminder_id}'."}

    updates: dict[str, Any] = {}
    if title is not None:
        clean_title = _clean_text(title)
        if not clean_title:
            return {"status": "error", "message": "Reminder title cannot be blank."}
        updates["title"] = clean_title
    if recurrence is not None:
        try:
            updates["recurrence"] = ReminderRecurrence(recurrence.lower())
        except ValueError:
            return {"status": "error", "message": f"Invalid recurrence '{recurrence}'."}
    if trigger_time is not None:
        timezone_name = _resolve_effective_timezone(context, None)
        parsed_trigger = _resolve_trigger_time(trigger_time, timezone_name, context.metadata)
        if not parsed_trigger:
            return {"status": "error", "message": f"Could not understand '{trigger_time}'."}
        updates["trigger_time"] = parsed_trigger

    async with context.acquire_session() as session:
        service = ReminderService(session, context.organisation_id, context.user_id)
        try:
            await service.get(parsed_reminder_id, reminder_type=ReminderType.STATIC)
            if message is not None:
                updates["message_content"] = _clean_text(message)
            if not updates:
                return {"status": "error", "message": "Provide a reminder field to update."}
            detail = await service.update(
                parsed_reminder_id,
                updates,
                reminder_type=ReminderType.STATIC,
            )
        except ReminderServiceError as exc:
            await session.rollback()
            return {"status": "error", "message": exc.message}
        except Exception:
            await session.rollback()
            logger.exception("update_reminder: unexpected error")
            return {"status": "error", "message": "Could not update the reminder."}

    result = {
        "status": "updated",
        "reminder_id": str(detail.reminder_id),
        "title": detail.title,
        "reminder_type": detail.reminder_type.value,
        "trigger_time": detail.next_trigger_at.isoformat(),
        "recurrence": detail.recurrence.value,
        "message": f"Updated reminder **{detail.title}**. Reminder ID: {detail.reminder_id}.",
    }
    return result


def create_update_reminder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind the user-owned reminder update contract to one agent context."""

    async def update_reminder_tool(
        reminder_id: str,
        title: str | None = None,
        trigger_time: str | None = None,
        message: str | None = None,
        recurrence: str | None = None,
    ) -> dict[str, Any]:
        """Update a static reminder resolved with ``list_reminders``.

        Send only fields the user explicitly requested.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateReminder",
            tool_func=update_reminder,
            parameters={
                "context": context,
                "reminder_id": reminder_id,
                "title": title,
                "trigger_time": trigger_time,
                "message": message,
                "recurrence": recurrence,
            },
            description="Updating reminder",
            fallback_value={"status": "error", "message": "Could not update the reminder."},
        )
        return dict(result)

    return update_reminder_tool


__all__ = ["create_update_reminder", "update_reminder"]
