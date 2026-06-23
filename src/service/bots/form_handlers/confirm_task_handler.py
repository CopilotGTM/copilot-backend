"""Form handler for confirming a task from a preview card.

When create_task sends a preview card with Confirm/Cancel buttons, the Confirm
button carries the full task payload as a JSON string in its value field. This
handler deserializes the payload and creates the CRMActivity via TaskWriteService.

The Cancel button is handled by a simple CancelFormHandler (no DB cleanup needed
since no row exists until confirmation).

Security: Resolves user from bot identity headers, not form metadata.
The organisation_id in the payload is verified against the resolved user's org.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.identity import resolve_user_from_activity
from src.service.crm.tasks.write_service import TaskWriteService

logger = get_logger(__name__)

_REMINDER_OFFSETS = {
    "at_due": timedelta(0),
    "15m_before": timedelta(minutes=15),
    "1h_before": timedelta(hours=1),
    "1d_before": timedelta(days=1),
}


class ConfirmTaskFormHandler(BaseFormHandler):
    """Handle confirm_task_submit — create the CRM task from preview payload."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        # Resolve the actual user from the bot identity — not from form data
        user, _ = await resolve_user_from_activity(activity)
        if not user:
            return FormHandlerResult(
                success=False,
                message="Could not identify your account.",
            )

        # Extract task payload. Three possible shapes:
        # 1. Teams: form_data has "task_payload" key with JSON string
        # 2. Slack: button value was JSON-parsed by _handle_block_action,
        #    so task fields (title, organisation_id, ...) are directly in form_data
        task_data: dict[str, Any]
        raw_payload = form_data.get("task_payload")
        if raw_payload:
            # Teams path — task_payload is a JSON string
            if isinstance(raw_payload, str):
                try:
                    task_data = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Invalid task payload: %s", raw_payload[:200])
                    return FormHandlerResult(
                        success=False,
                        message="Invalid task data. Please try creating the task again.",
                    )
            elif isinstance(raw_payload, dict):
                task_data = raw_payload
            else:
                return FormHandlerResult(
                    success=False,
                    message="Missing task data. Please try creating the task again.",
                )
        elif "title" in form_data:
            # Slack path — button value was parsed directly into form_data
            task_data = form_data
        else:
            return FormHandlerResult(
                success=False,
                message="Missing task data. Please try creating the task again.",
            )

        # Validate one-time nonce to prevent duplicate submissions
        from src.service.bots.form_handlers.nonce import validate_and_consume_nonce

        nonce = task_data.get("nonce", "")
        if not nonce or not validate_and_consume_nonce("task_confirm", nonce):
            return FormHandlerResult(
                success=False,
                message="This task was already created or the card has expired. Please create a new one.",
            )

        # Verify org ownership — the resolved user's org must match the payload
        payload_org_id = task_data.get("organisation_id", "")
        if str(user.organisation_id) != payload_org_id:
            logger.warning(
                "Org mismatch: user=%s, payload=%s",
                user.organisation_id,
                payload_org_id,
            )
            return FormHandlerResult(
                success=False,
                message="Organisation mismatch. Please try again.",
            )

        title = task_data.get("title", "").strip()
        if not title:
            return FormHandlerResult(success=False, message="Task title is missing.")

        # Parse due date back to datetime
        due_date: datetime | None = None
        due_date_str = task_data.get("due_date")
        if due_date_str:
            try:
                due_date = datetime.fromisoformat(due_date_str)
            except (ValueError, TypeError):
                logger.warning("Invalid due_date in task payload: %s", due_date_str)

        # Parse deal_id
        deal_id: UUID | None = None
        deal_id_str = task_data.get("deal_id")
        if deal_id_str:
            try:
                deal_id = UUID(deal_id_str)
            except ValueError:
                logger.warning("Invalid deal_id in task payload: %s", deal_id_str)

        # Parse assignee — if specified, use their user_id as owner
        assign_to_user_id: UUID | None = None
        assign_to_str = task_data.get("assign_to_user_id")
        if assign_to_str:
            try:
                assign_to_user_id = UUID(assign_to_str)
            except ValueError:
                logger.warning("Invalid assign_to_user_id: %s", assign_to_str)

        # Build the payload for TaskWriteService
        write_payload: dict[str, Any] = {
            "subject": title,
        }

        if assign_to_user_id:
            write_payload["owner_user_id"] = assign_to_user_id

        description = task_data.get("description")
        if description:
            write_payload["body"] = description

        priority = task_data.get("priority", "MEDIUM")
        write_payload["priority"] = priority

        if due_date:
            write_payload["due_date"] = due_date

        # Build reminder config if requested
        reminder_option = task_data.get("reminder")
        if reminder_option and due_date:
            offset = _REMINDER_OFFSETS.get(reminder_option, timedelta(0))
            trigger_time = due_date - offset

            # Only create reminder if trigger time is in the future
            if trigger_time > datetime.now(UTC):
                delivery_channel = task_data.get("delivery_channel", "teams")

                delivery_context: dict[str, str] = {}
                if delivery_channel == "slack":
                    delivery_context = {
                        "team_id": task_data.get("delivery_team_id", ""),
                        "channel_id": task_data.get("delivery_channel_id", ""),
                    }

                write_payload["reminder"] = {
                    "enabled": True,
                    "trigger_time": trigger_time,
                    "recurrence": "NONE",
                    "delivery_channel": delivery_channel,
                    "delivery_context": delivery_context,
                }
            else:
                logger.info(
                    "Skipping reminder — trigger time %s is in the past",
                    trigger_time,
                )

        # Create the task
        org_id = user.organisation_id
        user_id = user.user_id

        try:
            async for session in acquire_org_async_session(org_id):
                service = TaskWriteService(session, org_id, user_id)
                task = await service.create_task(write_payload, deal_id=deal_id)
                await session.commit()
                await service.send_pending_assignment_notifications()

                task_title = task.subject or title
        except Exception:
            logger.exception("Failed to create task from Slack/Teams confirmation")
            return FormHandlerResult(
                success=False,
                message="Failed to create the task. Please try again.",
            )

        assign_to_name = task_data.get("assign_to_name")
        assigned_suffix = f" (assigned to {assign_to_name})" if assign_to_name else ""

        return FormHandlerResult(
            success=True,
            message=f"Task **{task_title}** created{assigned_suffix}!",
        )
