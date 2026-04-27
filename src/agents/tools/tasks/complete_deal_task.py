"""Tool wrapper to complete internal deal tasks from post-meeting automation.

This is the direct-write path for task completion: the model passes intent from
meeting summary extraction, and this module converts it into a strict,
org-and-deal-scoped update via DealTaskAutomationService.

The tool is intentionally narrow. It does not perform any CRM sync writes and
relies on service-level validation to protect read-only and external-record cases.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.internal.deal_task_automation_service import (
    DealTaskAutomationError,
    DealTaskAutomationService,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities


async def complete_deal_task(
    *,
    context: AgentContext,
    task_id: str,
    reason: str,
) -> dict[str, Any]:
    """Complete an internal same-deal task after explicit meeting evidence.

    The caller provides only task_id + free-text reason; all tenancy and deal
    identity comes from immutable context to prevent cross-org or cross-deal writes.
    """
    # Immutable context is the trust boundary — reject missing org/deal before any
    # service instantiation so malformed tool calls fail fast and never touch DB.
    if not context.organisation_id or not context.deal_id:
        return {"status": "error", "message": "Deal context is required."}

    cleaned_reason = reason.strip()
    if not cleaned_reason:
        return {"status": "error", "message": "reason is required"}

    # Validate user-facing IDs early. A malformed UUID should become a clean tool
    # error, not a lower-level database error that looks unrelated to input shape.
    try:
        parsed_task_id = UUID(task_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": "task_id must be a valid UUID"}

    try:
        # Open a context-owned session and pass it through to the service so every
        # query remains under the same tenant-scoped transaction lifecycle.
        async with context.acquire_session() as session:
            service = DealTaskAutomationService(session, context.organisation_id)
            task = await service.complete_task(
                parsed_task_id,
                deal_id=context.deal_id,
                completion_reason=cleaned_reason,
            )
    except DealTaskAutomationError as exc:
        # Convert domain exceptions into stable tool output so callers can map
        # "not found / read-only / permission" cases without catching backend
        # exceptions across process boundaries.
        return {"status": "error", "message": exc.message, "status_code": exc.status_code}

    return {
        "status": "completed",
        "deal_id": str(context.deal_id),
        "task_id": str(task.activity_id),
        "task_status": task.status,
    }


def create_complete_deal_task(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build a context-bound tool entry that can execute in a capability-gated agent."""

    async def complete_deal_task_tool(task_id: str, reason: str) -> dict[str, Any]:
        """Complete an existing internal task for the current immutable deal context.

        Use this only when the meeting summary explicitly says the task was
        finished. Do not call this for CRM-synced read-only tasks.
        """
        # Keep user intent local to one tool execution and rely on capability
        # gating to prevent action on unsupported channel capabilities.
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CompleteDealTask",
            tool_func=complete_deal_task,
            parameters={"context": context, "task_id": task_id, "reason": reason},
            description="Completing an internal task on the current deal",
            fallback_value={"status": "error", "message": "Could not complete deal task."},
        )
        return dict(result)

    return complete_deal_task_tool


__all__ = ["complete_deal_task", "create_complete_deal_task"]
