"""List Reminders tool — shows the user's active reminders and scheduled alerts.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- user_id: Only shows reminders owned by the current user

This is a Teams-channel-only tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

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


async def list_reminders(
    *,
    context: AgentContext,
    include_completed: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List reminders for the current user.

    Args:
        context: AgentContext with org_id and user_id
        include_completed: Also show completed/cancelled reminders
        limit: Max reminders to return
    """
    organisation_id = context.organisation_id
    user_id = context.user_id

    if not user_id:
        return []

    effective_limit = max(1, min(limit, 50))

    async with context.acquire_session() as session:
        stmt = (
            select(UserReminder)
            .where(
                col(UserReminder.organisation_id) == organisation_id,
                col(UserReminder.user_id) == user_id,
            )
            .order_by(col(UserReminder.next_trigger_at).asc())
            .limit(effective_limit)
        )

        if not include_completed:
            stmt = stmt.where(
                col(UserReminder.status).in_(
                    [
                        ReminderStatus.ACTIVE.value,
                        ReminderStatus.TRIGGERED.value,
                    ]
                )
            )

        result = await session.execute(stmt)
        reminders = result.scalars().all()

    output: list[dict[str, Any]] = []
    for r in reminders:
        item: dict[str, Any] = {
            "reminder_id": str(r.reminder_id),
            "title": r.title,
            "type": r.reminder_type,
            "status": r.status,
            "next_trigger_at": r.next_trigger_at.isoformat() if r.next_trigger_at else None,
            "recurrence": r.recurrence,
            "timezone": r.user_timezone,
        }
        if r.reminder_type == "static":
            item["message"] = r.message_content
        elif r.reminder_type == "agentic" and r.agent_config:
            item["agent_prompt"] = r.agent_config.get("prompt", "")
        output.append(item)

    return output


def create_list_reminders(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the list_reminders tool bound to a context."""

    async def list_reminders_tool(
        include_completed: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List your active reminders and scheduled alerts.

        Use this tool when the user asks to see their reminders, scheduled alerts,
        or wants to know what notifications they have set up.

        Returns a list of reminders with their IDs, titles, trigger times,
        recurrence patterns, and status.

        Args:
            include_completed: Also show completed and cancelled reminders (default: False)
            limit: Maximum number of reminders to return (default: 20, max: 50)
        """
        logger.info(
            "[TOOL CALLED] list_reminders - include_completed=%s, limit=%s",
            include_completed,
            limit,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListReminders",
            tool_func=list_reminders,
            parameters={
                "context": context,
                "include_completed": include_completed,
                "limit": limit,
            },
            description="Listing reminders",
            fallback_value=[],
        )

        logger.info("[TOOL RESULT] list_reminders returned %d items", len(result) if result else 0)
        return list(result) if result else []

    return list_reminders_tool
