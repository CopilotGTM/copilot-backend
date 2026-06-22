"""Reminder lifecycle service for task-linked reminders."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
    UserReminder,
)
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.infra.database.sessions import master_async_session_scope
from src.infra.messaging.reminder_schedule import schedule_reminder, unschedule_reminder
from src.infra.utils.logger import get_logger
from src.service.channels import OutboundChannelService
from src.service.crm.tasks.contracts import TaskWriteServiceError

logger = get_logger("task_reminder_service")

_TASK_REMINDER_CHANNELS = ("teams", "slack")


class TaskReminderService:
    """Create, update, cancel, and schedule reminders linked to CRM tasks."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def create_task_reminder(self, task: CRMTask, cfg: dict[str, Any]) -> UserReminder:
        """Create a static reminder linked to a task via task_id."""
        trigger_time = cfg["trigger_time"]
        recurrence_str = cfg.get("recurrence", "NONE").lower()
        recurrence = ReminderRecurrence(recurrence_str)
        delivery_channel, delivery_context = await self.resolve_task_reminder_target(
            cfg.get("delivery_channel")
        )

        reminder_message = f"Reminder for task: {task.title or '(no subject)'}"
        if task.description:
            reminder_message = f"{reminder_message}\n\n{task.description}"

        reminder = UserReminder(
            organisation_id=self.organisation_id,
            user_id=self.actor_user_id,
            reminder_type=ReminderType.STATIC,
            title=task.title or "Task reminder",
            message_content=reminder_message,
            next_trigger_at=trigger_time,
            user_timezone=cfg.get("timezone") or "UTC",
            recurrence=recurrence,
            delivery_channel=delivery_channel,
            delivery_context=delivery_context,
            status=ReminderStatus.ACTIVE,
            activity_id=task.task_id,
        )

        self.session.add(reminder)
        await self.session.flush()

        self.safe_schedule(reminder.reminder_id, trigger_time.timestamp())

        task.reminder_at = trigger_time
        await self.session.flush()

        return reminder

    async def update_task_reminder(self, reminder: UserReminder, cfg: dict[str, Any]) -> None:
        """Update trigger time, delivery target, and recurrence on an existing task reminder."""
        trigger_time = cfg.get("trigger_time")
        if trigger_time:
            reminder.next_trigger_at = trigger_time
        recurrence_str = cfg.get("recurrence")
        if recurrence_str:
            reminder.recurrence = ReminderRecurrence(recurrence_str.lower())
        if cfg.get("timezone"):
            reminder.user_timezone = cfg["timezone"]
        if cfg.get("delivery_channel"):
            delivery_channel, delivery_context = await self.resolve_task_reminder_target(
                cfg["delivery_channel"]
            )
            reminder.delivery_channel = delivery_channel
            reminder.delivery_context = delivery_context

        await self.session.flush()

        if reminder.activity_id:
            task = await self.session.get(CRMTask, reminder.activity_id)
            if task:
                task.reminder_at = reminder.next_trigger_at
                await self.session.flush()

        self.safe_unschedule(reminder.reminder_id)
        self.safe_schedule(reminder.reminder_id, reminder.next_trigger_at.timestamp())

    async def cancel_task_reminders(self, activity_id: UUID) -> None:
        """Cancel all active/paused reminders linked to a task."""
        cancellable = {ReminderStatus.ACTIVE, ReminderStatus.PAUSED}
        stmt = select(UserReminder).where(
            col(UserReminder.activity_id) == activity_id,
            col(UserReminder.organisation_id) == self.organisation_id,
            col(UserReminder.status).in_([s.value for s in cancellable]),
        )
        result = await self.session.execute(stmt)
        reminders = result.scalars().all()

        for reminder in reminders:
            reminder.status = ReminderStatus.CANCELLED
            self.safe_unschedule(reminder.reminder_id)

        if reminders:
            task = await self.session.get(CRMTask, activity_id)
            if task:
                task.reminder_at = None
            await self.session.flush()

    async def get_active_reminder(self, activity_id: UUID) -> UserReminder | None:
        """Get the first active reminder linked to a task."""
        stmt = (
            select(UserReminder)
            .where(
                col(UserReminder.activity_id) == activity_id,
                col(UserReminder.organisation_id) == self.organisation_id,
                col(UserReminder.status) == ReminderStatus.ACTIVE.value,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def resolve_task_reminder_target(
        self, requested_channel: str | None
    ) -> tuple[str, dict[str, Any]]:
        if requested_channel:
            resolved = await self.resolve_delivery_target_for_channel(requested_channel)
            if not resolved:
                raise TaskWriteServiceError(
                    f"Channel '{requested_channel}' is not set up. Please connect it first before enabling task reminders.",
                    400,
                )
            return resolved

        for fallback_channel in ("teams", "slack"):
            resolved = await self.resolve_delivery_target_for_channel(fallback_channel)
            if resolved:
                return resolved

        raise TaskWriteServiceError(
            "No delivery channels available. Please connect Teams or Slack before enabling task reminders.",
            400,
        )

    async def resolve_delivery_target_for_channel(
        self, delivery_channel: str
    ) -> tuple[str, dict[str, Any]] | None:
        if delivery_channel not in _TASK_REMINDER_CHANNELS:
            raise TaskWriteServiceError("Invalid reminder delivery channel", 400)

        async with master_async_session_scope() as master_session:
            target = await OutboundChannelService().resolve_user_channel(
                master_session,
                self.organisation_id,
                self.actor_user_id,
                delivery_channel,
            )
        if target is None:
            return None
        return target.channel, target.delivery_context

    @staticmethod
    def normalize_delivery_context(
        delivery_channel: str,
        conversation_context: dict[str, Any] | None,
        external_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        return OutboundChannelService.normalize_delivery_context(
            delivery_channel,
            conversation_context,
            external_tenant_id,
        )

    @staticmethod
    def delivery_context_is_ready(
        delivery_channel: str,
        delivery_context: dict[str, Any] | None,
    ) -> bool:
        return OutboundChannelService.delivery_context_is_ready(
            delivery_channel,
            delivery_context,
        )

    def safe_schedule(self, reminder_id: UUID, trigger_epoch: float) -> None:
        try:
            schedule_reminder(self.organisation_id, reminder_id, trigger_epoch)
        except Exception:
            logger.warning("Failed to schedule reminder %s in Redis", reminder_id, exc_info=True)

    def safe_unschedule(self, reminder_id: UUID) -> None:
        try:
            unschedule_reminder(self.organisation_id, reminder_id)
        except Exception:
            logger.warning(
                "Failed to unschedule reminder %s from Redis",
                reminder_id,
                exc_info=True,
            )
