"""Authenticated user-level task listing for general chat.

The tool derives organisation and user identity exclusively from AgentContext,
then delegates matching and deal visibility to the canonical CRM read service.
It never accepts another user's ID, so prompt text cannot widen the read scope.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.agents.tools.tasks.list_deal_tasks import _format_task
from src.infra.utils.timezone import get_context_anchor_utc, get_context_timezone_name
from src.service.crm.tasks.read_service import TaskReadService, TaskReadServiceError

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

# Keep chat payloads bounded; task_count and truncated tell the agent when the
# user's visible assignment set is larger than the returned result.
MAX_PERSONAL_TASKS = 200


async def list_my_tasks(
    *,
    context: AgentContext,
    due_today: bool = False,
    include_completed: bool = False,
) -> dict[str, Any]:
    """List the context user's visible assignments within the immutable organisation.

    A user identity is required. Deal-linked tasks still pass the same deal
    visibility predicate as the Tasks page, while standalone tasks must belong
    to the authenticated user.
    """
    if context.user_id is None:
        return {"status": "error", "message": "My tasks require an authenticated user."}

    due_from = due_to = None
    due_local_date = None
    if due_today:
        # "Today" follows the chat user's local calendar day. Convert that
        # half-open local day to inclusive UTC bounds expected by TaskReadService.
        timezone_name = get_context_timezone_name(context.metadata)
        anchor_local = get_context_anchor_utc(context.metadata).astimezone(ZoneInfo(timezone_name))
        start_local = anchor_local.replace(hour=0, minute=0, second=0, microsecond=0)
        due_local_date = start_local.date()
        due_from = start_local.astimezone(UTC)
        due_to = (start_local + timedelta(days=1)).astimezone(UTC) - timedelta(microseconds=1)

    try:
        actor = await resolve_agent_access_actor(context)
        async with context.acquire_session() as session:
            service = TaskReadService(session, context.organisation_id, actor)
            records, total_count = await service.list_tasks(
                scope="assigned_to_me",
                current_user_id=context.user_id,
                current_user_email=context.user_email,
                due_from=due_from,
                due_to=due_to,
                due_local_date=due_local_date,
                include_completed=include_completed,
                limit=MAX_PERSONAL_TASKS,
            )
    except (AgentCRMAccessError, TaskReadServiceError) as exc:
        # Authorization and read-service failures are safe tool errors; callers
        # receive no partial task data from a failed access check.
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok",
        "task_count": total_count,
        "truncated": total_count > len(records),
        "tasks": [_format_task(record.task, deal_name=record.deal_name) for record in records],
    }


def create_list_my_tasks(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the authenticated user's task reader."""

    async def list_my_tasks_tool(
        due_today: bool = False,
        include_completed: bool = False,
    ) -> dict[str, Any]:
        """List tasks assigned to the current authenticated user.

        Use this for requests such as "list my tasks" or "my tasks today."
        Identity always comes from the authenticated session.

        Args:
            due_today: Return only tasks due today in the user's timezone.
            include_completed: Include completed and cancelled tasks.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListMyTasks",
            tool_func=list_my_tasks,
            parameters={
                "context": context,
                "due_today": due_today,
                "include_completed": include_completed,
            },
            description="Listing tasks assigned to the authenticated user",
            fallback_value={"status": "error", "message": "Could not list your tasks."},
        )
        return dict(result)

    return list_my_tasks_tool


__all__ = ["create_list_my_tasks", "list_my_tasks"]
