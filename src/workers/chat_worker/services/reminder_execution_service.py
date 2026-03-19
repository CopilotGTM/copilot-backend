"""Service for executing due reminders and scheduled alerts.

Handles both static (fixed-message) and agentic (AI-powered) reminders.
Called by the Tier 3 Celery task (execute_reminder).

Static reminders: Send the stored message_content via the configured bot channel.
Agentic reminders: Run GeneralAgent with the stored prompt, render the response
with the matching channel adapter, and send the result via proactive messaging.

Follows the fresh session pattern: DB sessions are acquired/released quickly
and never held during LLM calls or external API calls.
"""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlmodel import col

from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderRunStatus,
    ReminderStatus,
    ReminderType,
    UserReminder,
    UserReminderRun,
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
        run_id = await self._create_run_record(reminder_data)

        try:
            # Step 2: Execute based on type (no DB session held)
            frames: list = []
            if reminder_type == ReminderType.STATIC:
                success, result_text = await self._execute_static(reminder_data)
            else:
                success, result_text, frames = await self._execute_agentic(reminder_data)

            # Extract token data from agent frames (agentic runs only)
            token_data = self._extract_token_data(frames) if frames else None

            # Step 3: Update status
            if success:
                await self._mark_success(
                    reminder_id, run_id, reminder_data, result_text, token_data
                )
                return {"status": "success", "title": title}
            else:
                await self._mark_failure(
                    reminder_id,
                    run_id,
                    "Delivery failed",
                    run_status=ReminderRunStatus.DELIVERY_FAILED,
                    result_text=result_text,
                    token_data=token_data,
                )
                return {"status": "delivery_failed", "title": title}

        except Exception as exc:
            logger.exception(
                "Reminder execution failed org=%s reminder=%s: %s",
                self.organisation_id,
                reminder_id,
                exc,
            )
            await self._mark_failure(
                reminder_id,
                run_id,
                str(exc)[:500],
                run_status=ReminderRunStatus.FAILED,
            )
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
                "delivery_channel": reminder.delivery_channel,
                "delivery_context": reminder.delivery_context or {},
                "next_trigger_at": reminder.next_trigger_at,
                "user_timezone": reminder.user_timezone,
                "recurrence": ReminderRecurrence(reminder.recurrence),
                "recurrence_end_at": reminder.recurrence_end_at,
                "trigger_count": reminder.trigger_count,
                "consecutive_failures": reminder.consecutive_failures,
            }

        return None

    async def _create_run_record(self, data: dict[str, Any]) -> UUID:
        """Create an in-progress reminder run row before external work begins."""
        from sqlalchemy import func, select

        agent_prompt = data["agent_config"].get("prompt")
        input_snapshot = data.get("message_content") or agent_prompt or ""

        async for session in acquire_org_async_session(self.organisation_id):
            attempt_number_stmt = (
                select(func.count())
                .select_from(UserReminderRun)
                .where(
                    col(UserReminderRun.organisation_id) == self.organisation_id,
                    col(UserReminderRun.reminder_id) == data["reminder_id"],
                )
            )
            existing_runs = (await session.execute(attempt_number_stmt)).scalar_one()

            run = UserReminderRun(
                organisation_id=self.organisation_id,
                reminder_id=data["reminder_id"],
                user_id=data["user_id"],
                attempt_number=existing_runs + 1,
                triggered_for_at=data["next_trigger_at"],
                input_snapshot=input_snapshot,
            )
            session.add(run)
            await session.commit()
            return run.run_id

        msg = f"Failed to create reminder run record for reminder {data['reminder_id']}"
        raise RuntimeError(msg)

    async def _execute_static(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        """Send a static reminder message via the configured delivery channel."""
        text = f"**Reminder:** {data['title']}"
        if data.get("message_content"):
            text += f"\n\n{data['message_content']}"

        sent = await self._send_text(
            delivery_channel=data["delivery_channel"],
            delivery_context=data["delivery_context"],
            text=text,
        )
        return sent, text

    async def _execute_agentic(self, data: dict[str, Any]) -> tuple[bool, str | None, list]:
        """Run GeneralAgent with the stored prompt and send the result."""
        from src.adapters import AgentResponse, get_adapter
        from src.agents.context import AgentContext
        from src.agents.orchestrator import run_agent_for_chat
        from src.infra.database.sessions import acquire_master_async_session

        delivery_channel = data["delivery_channel"]
        delivery_context = data["delivery_context"]

        if not self._delivery_context_is_ready(delivery_channel, delivery_context):
            logger.error(
                "Agentic reminder missing delivery context: %s channel=%s",
                data["reminder_id"],
                delivery_channel,
            )
            return False, None, []

        prompt = data["agent_config"].get("prompt", "")
        if not prompt:
            logger.error("Agentic reminder has no prompt: %s", data["reminder_id"])
            return False, None

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

        metadata = dict(delivery_context)
        metadata["source"] = "scheduled_alert"
        if (
            delivery_channel == "teams"
            and delivery_context.get("conversation_id")
            and not metadata.get("conversation_id")
        ):
            metadata["conversation_id"] = delivery_context["conversation_id"]
        if (
            metadata.get("conversation_type") == "groupChat"
            and not metadata.get("teams_conversation_id")
            and delivery_context.get("conversation_id")
        ):
            metadata["teams_conversation_id"] = delivery_context["conversation_id"]

        # Build AgentContext (no session held — tools acquire on demand)
        context = AgentContext(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            org_name=org_name,
            user_id=data["user_id"],
            user_email=user_email,
            channel=delivery_channel,
            metadata=metadata,
        )

        messages = [
            {
                "role": "user",
                "content": (
                    "This is a trigger that has been given by the user.\n"
                    "This is the prompt from the trigger, so that you understand this is a "
                    f"reminder.\n\n{prompt}"
                ),
            }
        ]

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
            raise

        if not frames:
            logger.warning("Agent returned no frames for reminder %s", data["reminder_id"])
            return False, None, []

        # Render response via adapter
        adapter = get_adapter(delivery_channel)
        response = AgentResponse.from_frames(frames)
        payload = adapter.render(response)

        sent = await self._send_payload(
            delivery_channel=delivery_channel,
            delivery_context=delivery_context,
            payload=payload,
            title=data["title"],
            response_text=response.text,
        )

        return sent, response.text or None, frames

    @staticmethod
    def _extract_token_data(frames: list) -> dict[str, Any] | None:
        """Extract token usage from agent frames into a dict for run persistence."""
        from src.agents.common.token_tracking import extract_token_usage_from_frames

        token_usage, model_name, cost, duration = extract_token_usage_from_frames(frames)
        if not token_usage:
            return None
        return {
            "token_usage": token_usage,
            "model_name": model_name,
            "cost": cost,
            "latency_ms": int(duration * 1000) if duration is not None else None,
        }

    @staticmethod
    def _apply_token_data(run: UserReminderRun, token_data: dict[str, Any] | None) -> None:
        """Write token tracking fields onto a run record."""
        if not token_data:
            return
        from decimal import Decimal

        if token_data.get("token_usage"):
            run.token_usage = token_data["token_usage"]
        if token_data.get("model_name"):
            run.model_name = token_data["model_name"]
        cost = token_data.get("cost")
        if cost is not None:
            run.cost = Decimal(str(cost))
        if token_data.get("latency_ms") is not None:
            run.latency_ms = token_data["latency_ms"]

    @staticmethod
    def _delivery_context_is_ready(delivery_channel: str, delivery_context: dict[str, Any]) -> bool:
        """Validate the minimum delivery context required for reminder execution."""
        required_keys = {
            "teams": ("service_url", "conversation_id"),
            "slack": ("team_id", "channel_id"),
        }.get(delivery_channel)
        if not required_keys:
            return False

        return all(bool(str(delivery_context.get(key, "")).strip()) for key in required_keys)

    async def _send_text(
        self,
        *,
        delivery_channel: str,
        delivery_context: dict[str, Any],
        text: str,
    ) -> bool:
        """Send a plain text reminder through the configured bot provider."""
        if delivery_channel == "teams":
            from src.service.bots.teams_bot_service import TeamsBotService

            service_url = delivery_context.get("service_url", "")
            conversation_id = delivery_context.get("conversation_id", "")
            if not service_url or not conversation_id:
                logger.error("Teams reminder missing delivery context")
                return False

            bot_service = TeamsBotService()
            return await bot_service.send_proactive_message(
                service_url=service_url,
                conversation_id=conversation_id,
                message=text,
            )

        if delivery_channel == "slack":
            from src.service.bots.slack_bot_service import SlackBotService

            team_id = delivery_context.get("team_id", "")
            channel_id = delivery_context.get("channel_id", "")
            if not team_id or not channel_id:
                logger.error("Slack reminder missing delivery context")
                return False

            bot_service = SlackBotService()
            return await bot_service._send_message(team_id, channel_id, text)

        logger.error("Unsupported reminder delivery channel: %s", delivery_channel)
        return False

    async def _send_payload(
        self,
        *,
        delivery_channel: str,
        delivery_context: dict[str, Any],
        payload: Any,
        title: str,
        response_text: str | None,
    ) -> bool:
        """Send a rendered agent response through the configured bot provider."""
        if delivery_channel == "teams":
            from src.service.bots.teams_bot_service import TeamsBotService

            service_url = delivery_context.get("service_url", "")
            conversation_id = delivery_context.get("conversation_id", "")
            if not service_url or not conversation_id:
                logger.error("Teams reminder missing delivery context")
                return False

            bot_service = TeamsBotService()
            title_header = f"**Scheduled Alert: {title}**\n\n"
            return await bot_service.send_proactive_payload(
                service_url=service_url,
                conversation_id=conversation_id,
                payload=payload.content,
                text_fallback=title_header + (response_text[:200] if response_text else ""),
            )

        if delivery_channel == "slack":
            from src.service.bots.slack_bot_service import SlackBotService

            team_id = delivery_context.get("team_id", "")
            channel_id = delivery_context.get("channel_id", "")
            if not team_id or not channel_id:
                logger.error("Slack reminder missing delivery context")
                return False

            bot_service = SlackBotService()
            blocks = payload.content.get("blocks")
            text = payload.content.get("text") or payload.text_fallback or response_text or title
            if blocks:
                return await bot_service.send_block_kit_message(
                    team_id=team_id,
                    channel_id=channel_id,
                    blocks=blocks,
                    text_fallback=text,
                )

            return await bot_service._send_message(team_id, channel_id, text)

        logger.error("Unsupported reminder delivery channel: %s", delivery_channel)
        return False

    async def _mark_success(
        self,
        reminder_id: UUID,
        run_id: UUID,
        data: dict[str, Any],
        result_text: str | None,
        token_data: dict[str, Any] | None = None,
    ) -> None:
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

            run_stmt = select(UserReminderRun).where(
                col(UserReminderRun.run_id) == run_id,
                col(UserReminderRun.organisation_id) == self.organisation_id,
            )
            run = (await session.execute(run_stmt)).scalars().first()

            completed_at = datetime.now(UTC)

            reminder.last_triggered_at = completed_at
            reminder.trigger_count = data["trigger_count"] + 1
            reminder.consecutive_failures = 0
            reminder.last_error = None

            if run:
                run.status = ReminderRunStatus.SUCCEEDED
                run.completed_at = completed_at
                run.result_text = result_text
                run.error_message = None
                self._apply_token_data(run, token_data)
                session.add(run)

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

    async def _mark_failure(
        self,
        reminder_id: UUID,
        run_id: UUID,
        error_msg: str,
        *,
        run_status: ReminderRunStatus,
        result_text: str | None = None,
        token_data: dict[str, Any] | None = None,
    ) -> None:
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

            run_stmt = select(UserReminderRun).where(
                col(UserReminderRun.run_id) == run_id,
                col(UserReminderRun.organisation_id) == self.organisation_id,
            )
            run = (await session.execute(run_stmt)).scalars().first()

            reminder.consecutive_failures += 1
            reminder.last_error = error_msg

            if run:
                run.status = run_status
                run.completed_at = datetime.now(UTC)
                run.result_text = result_text
                run.error_message = error_msg
                self._apply_token_data(run, token_data)
                session.add(run)

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
            local_time = ReminderExecutionService._add_one_month(local_time)

        return local_time.astimezone(UTC)

    @staticmethod
    def _add_one_month(local_time: datetime) -> datetime:
        """Advance one calendar month while preserving time-of-day when possible."""
        if local_time.month == 12:
            next_year = local_time.year + 1
            next_month = 1
        else:
            next_year = local_time.year
            next_month = local_time.month + 1

        last_day = calendar.monthrange(next_year, next_month)[1]
        next_day = min(local_time.day, last_day)
        return local_time.replace(year=next_year, month=next_month, day=next_day)
