"""Deal-scoped task listing tool for post-meeting automation agents.

The listing is used as read-before-write guardrail in task automation flows: it
lets the agent avoid creating duplicates and avoids attempting to complete CRM-
synced records that are intentionally read-only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.internal.deal_task_automation_service import DealTaskAutomationService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities
    from src.domain.models.organisation_tables.sync.crm import CRMTask

MAX_TASKS_FOR_RECONCILIATION = 200


def _format_task(task: CRMTask) -> dict[str, Any]:
    """Build a compact API-safe task shape for tool consumers.

    The returned payload is immutable-friendly: all fields are serializable and
    include a read-only flag derived from source ownership.
    """
    return {
        "task_id": str(task.activity_id),
        "subject": task.subject,
        "body": task.body,
        "status": task.status,
        "priority": task.priority,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "owner_name": task.owner_name,
        "owner_email": task.owner_email,
        "source": task.source,
        "read_only": task.integration_id is not None,
    }


async def list_deal_tasks(*, context: AgentContext) -> dict[str, Any]:
    """List current tasks for the immutable deal in the agent context.

    The method only reads tasks within the current deal/org pair; if either piece
    of context is missing, this returns a clear tool-level error instead of a
    backend failure.
    """
    # Prevent accidental cross-tenant or unscoped reads before opening a session.
    if not context.organisation_id or not context.deal_id:
        return {"status": "error", "message": "Deal context is required."}

    # Keep listing inside the caller-owned session boundary; this is read-only
    # so there is no additional transactional mutation risk in the tool layer.
    async with context.acquire_session() as session:
        service = DealTaskAutomationService(session, context.organisation_id)
        tasks = await service.list_deal_tasks(context.deal_id)

    truncated = len(tasks) > MAX_TASKS_FOR_RECONCILIATION
    # Record the read-before-write guard on the shared run context. Write tools
    # reject calls when the agent skipped this check or could not inspect the full list.
    context.metadata["deal_task_list_checked"] = True
    context.metadata["deal_task_list_truncated"] = truncated

    return {
        "status": "ok",
        "deal_id": str(context.deal_id),
        "task_count": len(tasks),
        "truncated": truncated,
        "tasks": [
            _format_task(task) for task in tasks[:MAX_TASKS_FOR_RECONCILIATION]
        ],
    }


def create_list_deal_tasks(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build a read-only deal-task listing tool bound to immutable agent context."""

    async def list_deal_tasks_tool() -> dict[str, Any]:
        """List current CRM tasks for this deal.

        Use this before creating or completing tasks so existing task context is
        available. CRM-synced tasks are returned as read-only and must not be
        completed by automation.
        """
        # Tool framework handles capability checks and serialization; this function
        # only orchestrates payload wiring and safe context propagation.
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealTasks",
            tool_func=list_deal_tasks,
            parameters={"context": context},
            description="Listing current deal tasks before task automation",
            fallback_value={"status": "error", "message": "Could not list deal tasks."},
        )
        return dict(result)

    return list_deal_tasks_tool


__all__ = ["create_list_deal_tasks", "list_deal_tasks"]
