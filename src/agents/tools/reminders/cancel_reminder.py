"""Cancel Reminder tool — allows the GeneralAgent to cancel a user's reminder.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- user_id: Can only cancel own reminders

This is a Teams-channel-only tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.reminder_models import (
    ReminderStatus,
    UserReminder,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def cancel_reminder(
    *,
    context: AgentContext,
    reminder_id: str,
) -> dict[str, Any]:
    """Cancel a reminder by ID. Only the owning user can cancel their reminder."""
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not user_id:
        return {"status": "error", "message": "Cannot cancel reminder: user not identified."}

    try:
        parsed_id = UUID(reminder_id)
    except ValueError:
        return {"status": "error", "message": f"Invalid reminder_id: {reminder_id}"}

    async with context.acquire_session() as session:
        stmt = select(UserReminder).where(
            col(UserReminder.reminder_id) == parsed_id,
            col(UserReminder.organisation_id) == organisation_id,
            col(UserReminder.user_id) == user_id,
        )
        result = await session.execute(stmt)
        reminder = result.scalars().first()

        if not reminder:
            return {"status": "error", "message": "Reminder not found or not owned by you."}

        if reminder.status == ReminderStatus.CANCELLED:
            return {"status": "already_cancelled", "message": "This reminder is already cancelled."}

        reminder.status = ReminderStatus.CANCELLED
        session.add(reminder)
        await session.commit()

    from src.infra.messaging.reminder_schedule import unschedule_reminder

    try:
        unschedule_reminder(organisation_id, parsed_id)
    except Exception:
        logger.warning(
            "Failed to ZREM reminder %s; poller will skip it (DB status is CANCELLED)",
            parsed_id,
            exc_info=True,
        )

    return {
        "status": "cancelled",
        "message": f"Reminder **{reminder.title}** has been cancelled.",
    }


def create_cancel_reminder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the cancel_reminder tool bound to a context."""

    async def cancel_reminder_tool(
        reminder_id: str,
    ) -> dict[str, Any]:
        """Cancel a scheduled reminder or alert by its ID.

        Use this tool when the user wants to cancel or delete a reminder.
        First call list_reminders to show the user their active reminders,
        then use the reminder_id from the list to cancel it.

        Args:
            reminder_id: UUID of the reminder to cancel
        """
        logger.info("[TOOL CALLED] cancel_reminder - reminder_id='%s'", reminder_id)

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CancelReminder",
            tool_func=cancel_reminder,
            parameters={
                "context": context,
                "reminder_id": reminder_id,
            },
            description="Canceling reminder",
            fallback_value={"status": "error", "message": "Failed to cancel reminder."},
        )

        logger.info("[TOOL RESULT] cancel_reminder status=%s", result.get("status"))
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return cancel_reminder_tool
