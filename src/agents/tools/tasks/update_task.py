"""Update user-owned CRM tasks from GeneralAgent chat workflows.

The adapter reuses task creation's date and assignee resolvers, then delegates
all ownership, field, and reminder invariants to ``TaskWriteService``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.agents.tools.tasks.create_task import _resolve_due_date, _resolve_user_by_name
from src.infra.utils.timezone import resolve_timezone_name
from src.service.crm.tasks.contracts import TaskWriteServiceError
from src.service.crm.tasks.write_service import TaskWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_VALID_PRIORITIES = {"NONE", "LOW", "MEDIUM", "HIGH", "URGENT"}
_VALID_STATUSES = {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}


async def update_task(
    *,
    context: AgentContext,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
    status: str | None = None,
    assign_to: str | None = None,
) -> dict[str, Any]:
    """Update only the requested fields on one user-owned CRM task.

    Args:
        context: Immutable organisation and current-user context.
        task_id: Resolved task UUID from a task listing result.
        title: Replacement task title.
        description: Replacement task description.
        priority: New NONE, LOW, MEDIUM, HIGH, or URGENT priority.
        due_date: Natural-language or ISO due date in the user's timezone.
        status: New NOT_STARTED, IN_PROGRESS, COMPLETED, or CANCELLED status.
        assign_to: Organisation member name or email to resolve and assign.

    Returns:
        A structured update result or a user-safe validation error.
    """
    if not context.user_id:
        return {"status": "error", "message": "Cannot update task: user not identified."}
    try:
        parsed_task_id = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": f"Invalid task_id '{task_id}'."}

    payload: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            return {"status": "error", "message": "Task title cannot be blank."}
        payload["subject"] = title.strip()
    if description is not None:
        payload["body"] = description.strip()
    if priority is not None:
        normalized_priority = priority.upper()
        if normalized_priority not in _VALID_PRIORITIES:
            return {"status": "error", "message": "Invalid task priority."}
        payload["priority"] = normalized_priority
    if status is not None:
        normalized_status = status.upper()
        if normalized_status not in _VALID_STATUSES:
            return {"status": "error", "message": "Invalid task status."}
        payload["status"] = normalized_status

    metadata = context.metadata or {}
    timezone_name = resolve_timezone_name(metadata.get("user_timezone"), default="UTC") or "UTC"
    if due_date is not None:
        parsed_due_date = _resolve_due_date(due_date, timezone_name, metadata)
        if not parsed_due_date:
            return {"status": "error", "message": f"Could not understand the date '{due_date}'."}
        payload["due_date"] = parsed_due_date
    if assign_to is not None:
        assignee = await _resolve_user_by_name(context.organisation_id, assign_to.strip())
        if not assignee:
            return {"status": "error", "message": f"Could not find team member '{assign_to}'."}
        payload["owner_user_id"] = assignee[0]
    if not payload:
        return {"status": "error", "message": "Provide at least one task field to update."}

    async with context.acquire_session() as session:
        try:
            access_actor = await resolve_agent_access_actor(context)
        except AgentCRMAccessError as exc:
            return {"status": "error", "message": str(exc)}
        service = TaskWriteService(
            session,
            context.organisation_id,
            context.user_id,
            access_actor=access_actor,
        )
        try:
            task = await service.update_task(parsed_task_id, payload)
            # Assignment notifications describe committed state, so never send before commit.
            await session.commit()
            await service.send_pending_assignment_notifications()
        except TaskWriteServiceError as exc:
            await session.rollback()
            return {"status": "error", "message": str(exc)}
        except Exception:
            await session.rollback()
            logger.exception("update_task: unexpected error")
            return {"status": "error", "message": "Could not update the task."}

    task_id_value = str(task.activity_id)
    deal_id_value = getattr(task, "deal_id", None)
    result = {
        "status": "updated",
        "task_id": task_id_value,
        "title": task.subject,
        "priority": task.priority,
        "task_status": task.status,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "message": f"Updated task **{task.subject}**. Task ID: {task_id_value}.",
    }
    if deal_id_value:
        result["deal_id"] = str(deal_id_value)
        result["message"] += f" Deal ID: {deal_id_value}."
    if getattr(task, "related_name", None):
        result["deal_name"] = task.related_name
    return result


def create_update_task(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind the org- and user-scoped task update contract to one agent context."""

    async def update_task_tool(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        status: str | None = None,
        assign_to: str | None = None,
    ) -> dict[str, Any]:
        """Update a resolved task, sending only fields the user requested.

        Resolve the task with ``list_deal_tasks`` before calling this tool. Never
        guess a task ID; provider-synced tasks are read-only.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateTask",
            tool_func=update_task,
            parameters={
                "context": context,
                "task_id": task_id,
                "title": title,
                "description": description,
                "priority": priority,
                "due_date": due_date,
                "status": status,
                "assign_to": assign_to,
            },
            description="Updating task",
            fallback_value={"status": "error", "message": "Could not update the task."},
        )
        return dict(result)

    return update_task_tool


__all__ = ["create_update_task", "update_task"]
