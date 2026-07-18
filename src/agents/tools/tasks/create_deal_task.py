"""Deal-scoped direct task creation tool for post-meeting automation agents.

This file is the write path used when agents need to persist a concrete action item
immediately from meeting output. It stays scoped to the immutable context and uses
DealTaskAutomationService for idempotent, tenant-safe creation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm import CRMActivityPriority
from src.service.crm.internal.deal_task_automation_service import (
    DealTaskAutomationError,
    DealTaskAutomationService,
)
from src.service.crm.tasks.contracts import TaskWriteServiceError
from src.service.crm.tasks.reminder_service import TaskReminderService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

MAX_TASK_CREATIONS_PER_RUN = 10


def _metadata_uuid(metadata: dict, *keys: str) -> UUID | None:
    """Read the first valid UUID-like value from a metadata key chain.

    Metadata shape is not uniform across upstream pipelines, so this utility accepts
    alternate keys and returns the first valid UUID it finds.
    """
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, UUID):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return UUID(value)
            except ValueError:
                # Upstream payloads can send malformed ids during partial event
                # propagation; treat them as missing rather than raising.
                return None
    return None


def _parse_due_date(value: str | datetime | None) -> datetime | None:
    """Parse due_date values only when they are already in an acceptable ISO format."""
    # We deliberately avoid ambiguous fuzzy parsing here to keep the automation
    # writer deterministic.
    if value is None or isinstance(value, datetime):
        return value
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def create_deal_task(
    *,
    context: AgentContext,
    title: str,
    body: str | None = None,
    assignee: str | None = None,
    fallback_assignees: list[str] | None = None,
    priority: str = CRMActivityPriority.NONE.value,
    due_date: str | datetime | None = None,
) -> dict[str, Any]:
    """Create an internal AI meeting task for the immutable deal context.

    The function enforces minimal validation (context + title + due-date shape),
    then delegates persistence to DealTaskAutomationService so all idempotency and
    external-task guards stay in one place.
    """
    # --- Step 1: harden execution context ---
    if not context.organisation_id or not context.deal_id:
        return {"status": "error", "message": "Deal context is required."}
    if not context.metadata.get("deal_task_list_checked"):
        return {"status": "error", "message": "ListDealTasks must be called before task writes."}
    if context.metadata.get("deal_task_list_truncated"):
        return {
            "status": "error",
            "message": "Task list is too large for safe duplicate reconciliation.",
        }

    cleaned_title = title.strip()
    if not cleaned_title:
        return {"status": "error", "message": "title is required"}

    # --- Step 2: normalize optional due date input ---
    parsed_due_date = _parse_due_date(due_date)
    if due_date and parsed_due_date is None:
        return {"status": "error", "message": "due_date must be an ISO datetime"}

    # --- Step 3: resolve source metadata and persist once per deal-context ---
    try:
        async with context.acquire_session() as session:
            service = DealTaskAutomationService(session, context.organisation_id)
            result = await service.create_task(
                deal_id=context.deal_id,
                subject=cleaned_title,
                body=body,
                assignee_name_or_email=assignee,
                fallback_assignee_name_or_emails=fallback_assignees,
                source_event_id=_metadata_uuid(
                    context.metadata, "source_event_id", "meeting_event_id", "event_id"
                ),
                source_association_id=_metadata_uuid(
                    context.metadata,
                    "source_association_id",
                    "meeting_deal_association_id",
                    "association_id",
                ),
                source_summary_job_id=_metadata_uuid(
                    context.metadata, "source_summary_job_id", "summary_job_id", "job_id"
                ),
                priority=priority,
                due_date=parsed_due_date,
            )
            reminder = None
            reminder_status = "not_created"
            reminder_reason = None
            if result.assignee_public_user_id and result.reminder_at:
                reminders = TaskReminderService(
                    session,
                    context.organisation_id,
                    result.assignee_public_user_id,
                )
                reminder = await reminders.get_active_reminder(result.task.activity_id)
                if reminder:
                    reminder_status = "existing"
                else:
                    try:
                        reminder = await reminders.create_task_reminder(
                            result.task,
                            {
                                "trigger_time": result.reminder_at,
                                "recurrence": "NONE",
                                "timezone": result.assignee_timezone,
                            },
                        )
                        reminder_status = "created"
                    except TaskWriteServiceError as exc:
                        # A missing channel must not roll back the task. The tool
                        # reports the reason so the UI can prompt the owner to connect one.
                        reminder_reason = exc.message
            elif not result.assignee_public_user_id:
                reminder_reason = "No resolvable internal owner; task created without reminder."
            else:
                reminder_reason = "No future reminder time is available for this task."

            # Agent tools own their short transaction. Without this commit, the
            # context manager closes the session and silently rolls back the task.
            await session.commit()
    except DealTaskAutomationError as exc:
        # Keep tool output structured even on service-level contract failures.
        # This keeps agents consistent when they retry after transient platform errors.
        return {"status": "error", "message": exc.message, "status_code": exc.status_code}

    return {
        "status": "created" if result.created else "duplicate",
        "deal_id": str(context.deal_id),
        "task_id": str(result.task.activity_id),
        "subject": result.task.subject,
        "created": result.created,
        "reason": result.reason,
        "assignee": result.task.owner_email,
        "reminder_id": str(reminder.reminder_id) if reminder else None,
        "reminder_status": reminder_status,
        "reminder_channel": reminder.delivery_channel if reminder else None,
        "reminder_reason": reminder_reason,
    }


def create_create_deal_task(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build a context-bound creation tool and enforce capability gating."""
    creation_attempts = 0

    async def create_deal_task_tool(
        title: str,
        body: str | None = None,
        assignee: str | None = None,
        fallback_assignees: list[str] | None = None,
        priority: str = CRMActivityPriority.NONE.value,
        due_date: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Create a concrete follow-up task for this deal.

        Use this for meeting action items. Create the task even when no owner is
        known. Pass `assignee` as the explicit meeting owner and
        `fallback_assignees` in deal-owner then organiser order.
        """
        nonlocal creation_attempts
        if creation_attempts >= MAX_TASK_CREATIONS_PER_RUN:
            return {
                "status": "error",
                "message": f"Maximum {MAX_TASK_CREATIONS_PER_RUN} task creations per meeting.",
            }
        creation_attempts += 1

        # Capabilities can block tool execution on unsupported channels; this call
        # remains side-effect-free until `execute_with_capabilities` grants run.
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateDealTask",
            tool_func=create_deal_task,
            parameters={
                "context": context,
                "title": title,
                "body": body,
                "assignee": assignee,
                "fallback_assignees": fallback_assignees,
                "priority": priority,
                "due_date": due_date,
            },
            description="Creating an internal AI meeting task on the current deal",
            fallback_value={"status": "error", "message": "Could not create deal task."},
        )
        return dict(result)

    return create_deal_task_tool


__all__ = ["create_create_deal_task", "create_deal_task"]
