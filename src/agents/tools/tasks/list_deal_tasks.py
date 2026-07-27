"""Deal-scoped task listing tool for post-meeting automation agents.

The listing is used as read-before-write guardrail in task automation flows: it
lets the agent avoid creating duplicates and avoids attempting to complete CRM-
synced records that are intentionally read-only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import (
    AgentCRMAccessError,
    require_agent_deal_access,
    resolve_agent_deal_id,
)
from src.service.crm.internal.deal_task_automation_service import DealTaskAutomationService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities
    from src.domain.models.organisation_tables.sync.crm import CRMTask

# Scheduled reconciliation is capped to keep one automation run bounded. Human
# deal reads are not capped because the tool contract promises the full deal.
MAX_TASKS_FOR_RECONCILIATION = 200


def _format_task(task: CRMTask, *, deal_name: str | None = None) -> dict[str, Any]:
    """Build a compact API-safe task shape for tool consumers.

    The returned payload is immutable-friendly: all fields are serializable and
    include a read-only flag derived from source ownership.
    """
    due_value = getattr(task, "due_at", None) or task.due_date
    return {
        "task_id": str(task.activity_id),
        "subject": task.subject,
        "body": task.body,
        "status": task.status,
        "priority": task.priority,
        "due_date": due_value.isoformat() if due_value else None,
        "owner_name": task.owner_name,
        "owner_email": task.owner_email,
        "deal_id": str(task.deal_id) if getattr(task, "deal_id", None) else None,
        "deal_name": deal_name,
        "source": task.source,
        "read_only": task.integration_id is not None,
    }


async def list_deal_tasks(
    *,
    context: AgentContext,
    deal_id: str | None = None,
) -> dict[str, Any]:
    """List current tasks for an explicit or context-scoped visible deal.

    An omitted deal ID preserves scheduled-agent behavior by using the immutable
    context. Human chat agents may pass a deal returned by deal search and see
    every task for that deal after the normal deal-access check.
    """
    try:
        effective_deal_id = resolve_agent_deal_id(context, deal_id)
    except AgentCRMAccessError as exc:
        return {"status": "error", "message": str(exc)}
    if not context.organisation_id or not effective_deal_id:
        return {"status": "error", "message": "Deal context is required."}

    # Keep listing inside the caller-owned session boundary; this is read-only
    # so there is no additional transactional mutation risk in the tool layer.
    async with context.acquire_session() as session:
        if context.user_id is not None or deal_id is not None:
            try:
                await require_agent_deal_access(context, session, effective_deal_id)
            except AgentCRMAccessError as exc:
                return {"status": "error", "message": str(exc)}
        service = DealTaskAutomationService(session, context.organisation_id)
        tasks = await service.list_deal_tasks(
            effective_deal_id,
            include_completed=context.user_id is not None,
        )

    is_human_read = context.user_id is not None
    truncated = not is_human_read and len(tasks) > MAX_TASKS_FOR_RECONCILIATION
    # Record the read-before-write guard on the shared run context. Write tools
    # reject calls when the agent skipped this check or could not inspect the full list.
    # Only the immutable context deal can satisfy the post-meeting write guard.
    # Listing another visible deal is useful for chat, but must not unlock writes.
    context.metadata["deal_task_list_checked"] = effective_deal_id == context.deal_id
    context.metadata["deal_task_list_checked_deal_id"] = str(effective_deal_id)
    context.metadata["deal_task_list_truncated"] = truncated

    return {
        "status": "ok",
        "deal_id": str(effective_deal_id),
        "task_count": len(tasks),
        "truncated": truncated,
        "tasks": [
            _format_task(task)
            for task in (tasks if is_human_read else tasks[:MAX_TASKS_FOR_RECONCILIATION])
        ],
    }


def create_list_deal_tasks(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build a reader with optional selection and immutable-context fallback."""

    async def list_deal_tasks_tool(deal_id: str | None = None) -> dict[str, Any]:
        """List current CRM tasks for this deal.

        Use this before creating or completing tasks so existing task context is
        available. CRM-synced tasks are returned as read-only and must not be
        completed by automation. For a human caller, return every task across
        every assignee and status after verifying deal access.

        Args:
            deal_id: Visible deal UUID. If omitted, uses the current deal context.
        """
        # Tool framework handles capability checks and serialization; this function
        # only orchestrates payload wiring and safe context propagation.
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealTasks",
            tool_func=list_deal_tasks,
            parameters={
                "context": context,
                "deal_id": deal_id,
            },
            description="Listing current deal tasks before task automation",
            fallback_value={"status": "error", "message": "Could not list deal tasks."},
        )
        return dict(result)

    return list_deal_tasks_tool


__all__ = ["create_list_deal_tasks", "list_deal_tasks"]
