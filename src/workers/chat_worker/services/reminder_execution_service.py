"""Service for executing due reminders and scheduled alerts.

Handles both static (fixed-message) and agentic (AI-powered) reminders.
Called by the Tier 3 Celery task (execute_reminder).

Static reminders: Send the stored message_content via Teams proactive messaging.
Agentic reminders: Run GeneralAgent with the stored prompt, render the response
via TeamsAdapter, and send the result via proactive messaging.

Follows the fresh session pattern: DB sessions are acquired/released quickly
and never held during LLM calls or external API calls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
    UserReminder,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3


class ReminderExecutionService:
    """Executes a single due reminder (static or agentic) and updates its status.

    Called from the Tier 3 Celery task. Each call:
    1. Loads and locks the reminder
    2. Executes it (send message or run agent)
    3. Updates status, trigger count, and computes next trigger for recurring ones
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    def execute(self, reminder_id: UUID) -> dict[str, Any]:
        """Synchronous entry point for Celery tasks."""
        return run_async(self._execute_async(reminder_id))

    async def _execute_async(self, reminder_id: UUID) -> dict[str, Any]:
        """Load the reminder, execute it, and update status."""

        # Step 1: Load reminder and mark as triggered
        reminder_data = await self._load_and_lock(reminder_id)
        if not reminder_data:
            return {"status": "skipped", "reason": "not_found_or_locked"}

        reminder_type = reminder_data["reminder_type"]
        title = reminder_data["title"]

        try:
            # Step 2: Execute based on type (no DB session held)
            if reminder_type == ReminderType.STATIC:
                success = await self._execute_static(reminder_data)
            else:
                success = await self._execute_agentic(reminder_data)

            # Step 3: Update status
            if success:
                await self._mark_success(reminder_id, reminder_data)
                return {"status": "success", "title": title}
            else:
                await self._mark_failure(reminder_id, "Delivery failed")
                return {"status": "delivery_failed", "title": title}

        except Exception as exc:
            logger.exception(
                "Reminder execution failed org=%s reminder=%s: %s",
                self.organisation_id,
                reminder_id,
                exc,
            )
            await self._mark_failure(reminder_id, str(exc)[:500])
            return {"status": "error", "title": title, "error": str(exc)[:200]}

    async def _load_and_lock(self, reminder_id: UUID) -> dict[str, Any] | None:
        """Load reminder and mark as triggered. Returns None if already processed."""
        from sqlalchemy import select
        from sqlmodel import col

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(UserReminder)
                .where(
                    col(UserReminder.reminder_id) == reminder_id,
                    col(UserReminder.organisation_id) == self.organisation_id,
                    col(UserReminder.status) == ReminderStatus.ACTIVE,
                )
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            reminder = result.scalars().first()

            if not reminder:
                return None

            # Dedup: skip if triggered very recently (within 2 minutes)
            if reminder.last_triggered_at:
                since_last = datetime.now(UTC) - reminder.last_triggered_at
                if since_last < timedelta(minutes=2):
                    logger.info(
                        "Skipping recently triggered reminder=%s (last=%s)",
                        reminder_id,
                        reminder.last_triggered_at,
                    )
                    return None

            # Mark as triggered
            reminder.status = ReminderStatus.TRIGGERED
            session.add(reminder)
            await session.commit()

            # Extract everything we need before releasing the session
            return {
                "reminder_id": reminder.reminder_id,
                "user_id": reminder.user_id,
                "reminder_type": ReminderType(reminder.reminder_type),
                "title": reminder.title,
                "message_content": reminder.message_content,
                "agent_config": reminder.agent_config or {},
                "delivery_context": reminder.delivery_context or {},
                "user_timezone": reminder.user_timezone,
                "recurrence": ReminderRecurrence(reminder.recurrence),
                "recurrence_end_at": reminder.recurrence_end_at,
                "trigger_count": reminder.trigger_count,
                "consecutive_failures": reminder.consecutive_failures,
            }

    async def _execute_static(self, data: dict[str, Any]) -> bool:
        """Send a static reminder message via Teams proactive messaging."""
        from src.service.bots.teams_bot_service import TeamsBotService

        service_url = data["delivery_context"].get("service_url", "")
        conversation_id = data["delivery_context"].get("conversation_id", "")

        if not service_url or not conversation_id:
            logger.error("Static reminder missing delivery context: %s", data["reminder_id"])
            return False

        text = f"**Reminder:** {data['title']}"
        if data.get("message_content"):
            text += f"\n\n{data['message_content']}"

        bot_service = TeamsBotService()
        return await bot_service.send_proactive_message(
            service_url=service_url,
            conversation_id=conversation_id,
            message=text,
        )

    async def _execute_agentic(self, data: dict[str, Any]) -> bool:
        """Run GeneralAgent with the stored prompt and send the result."""
        from src.adapters import AgentResponse, get_adapter
        from src.agents.context import AgentContext
        from src.agents.orchestrator import run_agent_for_chat
        from src.infra.database.sessions import acquire_master_async_session
        from src.service.bots.teams_bot_service import TeamsBotService

        service_url = data["delivery_context"].get("service_url", "")
        conversation_id = data["delivery_context"].get("conversation_id", "")

        if not service_url or not conversation_id:
            logger.error("Agentic reminder missing delivery context: %s", data["reminder_id"])
            return False

        prompt = data["agent_config"].get("prompt", "")
        if not prompt:
            logger.error("Agentic reminder has no prompt: %s", data["reminder_id"])
            return False

        deal_id_str = data["agent_config"].get("deal_id")
        deal_id = UUID(deal_id_str) if deal_id_str else None

        # Look up org name and user email for proper agent context
        org_name = "Our Organization"
        user_email: str | None = None
        async for session in acquire_master_async_session():
            from sqlalchemy import select
            from sqlmodel import col

            from src.domain.models.master_tables import Organisation, User

            org_stmt = select(col(Organisation.organisation_name)).where(
                col(Organisation.organisation_id) == self.organisation_id
            )
            org_row = (await session.execute(org_stmt)).scalar_one_or_none()
            if org_row:
                org_name = org_row

            user_stmt = select(col(User.email)).where(col(User.user_id) == data["user_id"])
            user_email = (await session.execute(user_stmt)).scalar_one_or_none()

        # Build AgentContext (no session held — tools acquire on demand)
        context = AgentContext(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            org_name=org_name,
            user_id=data["user_id"],
            user_email=user_email,
            channel="teams",
            metadata={
                "service_url": service_url,
                "conversation_id": conversation_id,
                "source": "scheduled_alert",
            },
        )

        messages = [{"role": "user", "content": prompt}]

        # Run agent (no DB session held during this)
        frames = []
        try:
            async for frame in run_agent_for_chat(
                chat_type="GENERAL_CHAT",
                messages=messages,
                context=context,
            ):
                frames.append(frame)
        except Exception as e:
            logger.exception("Agent execution failed for reminder %s: %s", data["reminder_id"], e)
            return False

        if not frames:
            logger.warning("Agent returned no frames for reminder %s", data["reminder_id"])
            return False

        # Render response via adapter
        adapter = get_adapter("teams")
        response = AgentResponse.from_frames(frames)
        payload = adapter.render(response)

        # Send to Teams
        bot_service = TeamsBotService()
        title_header = f"**Scheduled Alert: {data['title']}**\n\n"

        # For rich payloads (adaptive cards), send as-is
        sent = await bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=payload.content,
            text_fallback=title_header + (response.text[:200] if response.text else ""),
        )

        return sent

    async def _mark_success(self, reminder_id: UUID, data: dict[str, Any]) -> None:
        """Update reminder after successful execution."""
        async for session in acquire_org_async_session(self.organisation_id):
            from sqlalchemy import select
            from sqlmodel import col

            stmt = select(UserReminder).where(
                col(UserReminder.reminder_id) == reminder_id,
                col(UserReminder.organisation_id) == self.organisation_id,
            )
            result = await session.execute(stmt)
            reminder = result.scalars().first()
            if not reminder:
                return

            reminder.last_triggered_at = datetime.now(UTC)
            reminder.trigger_count = data["trigger_count"] + 1
            reminder.consecutive_failures = 0
            reminder.last_error = None

            recurrence = data["recurrence"]
            if recurrence == ReminderRecurrence.NONE:
                reminder.status = ReminderStatus.COMPLETED
            else:
                next_trigger = self._compute_next_trigger(
                    reminder.last_triggered_at,
                    recurrence,
                    data["user_timezone"],
                )
                if data.get("recurrence_end_at") and next_trigger > data["recurrence_end_at"]:
                    reminder.status = ReminderStatus.COMPLETED
                else:
                    reminder.next_trigger_at = next_trigger
                    reminder.status = ReminderStatus.ACTIVE

            session.add(reminder)
            await session.commit()

            # Schedule next occurrence in Redis sorted set
            if reminder.status == ReminderStatus.ACTIVE and reminder.next_trigger_at:
                from src.infra.messaging.reminder_schedule import schedule_reminder

                schedule_reminder(
                    self.organisation_id,
                    reminder_id,
                    reminder.next_trigger_at.timestamp(),
                )

    async def _mark_failure(self, reminder_id: UUID, error_msg: str) -> None:
        """Update reminder after failed execution."""
        next_trigger_ts: float | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            from sqlalchemy import select
            from sqlmodel import col

            stmt = select(UserReminder).where(
                col(UserReminder.reminder_id) == reminder_id,
                col(UserReminder.organisation_id) == self.organisation_id,
            )
            result = await session.execute(stmt)
            reminder = result.scalars().first()
            if not reminder:
                return

            reminder.consecutive_failures += 1
            reminder.last_error = error_msg

            if reminder.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                reminder.status = ReminderStatus.FAILED
                logger.warning(
                    "Reminder %s failed %d times, marking as FAILED",
                    reminder_id,
                    reminder.consecutive_failures,
                )
            else:
                reminder.status = ReminderStatus.ACTIVE

            session.add(reminder)
            await session.commit()

            # Extract trigger time before session closes
            if reminder.status == ReminderStatus.ACTIVE and reminder.next_trigger_at:
                next_trigger_ts = reminder.next_trigger_at.timestamp()

        # Re-add to ZSET so the poller retries without waiting for recovery sweep
        if next_trigger_ts is not None:
            from src.infra.messaging.reminder_schedule import schedule_reminder

            schedule_reminder(self.organisation_id, reminder_id, next_trigger_ts)

    @staticmethod
    def _compute_next_trigger(
        current_trigger: datetime,
        recurrence: ReminderRecurrence,
        user_timezone: str,
    ) -> datetime:
        """Compute the next trigger time in the user's local timezone, then convert to UTC.

        Handles DST transitions by working in the user's local timezone.
        """
        try:
            tz = ZoneInfo(user_timezone)
        except (KeyError, ValueError):
            tz = ZoneInfo("UTC")

        local_time = current_trigger.astimezone(tz)

        if recurrence == ReminderRecurrence.DAILY:
            local_time += timedelta(days=1)
        elif recurrence == ReminderRecurrence.WEEKDAYS:
            local_time += timedelta(days=1)
            # Skip weekends
            while local_time.weekday() >= 5:
                local_time += timedelta(days=1)
        elif recurrence == ReminderRecurrence.WEEKLY:
            local_time += timedelta(weeks=1)
        elif recurrence == ReminderRecurrence.BIWEEKLY:
            local_time += timedelta(weeks=2)
        elif recurrence == ReminderRecurrence.MONTHLY:
            # Use relativedelta for month arithmetic
            from dateutil.relativedelta import relativedelta

            local_time += relativedelta(months=1)

        return local_time.astimezone(UTC)
