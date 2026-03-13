"""Service for managing user reminders via API.

Thin layer over the existing UserReminder model and Redis ZSET scheduling.
Handles CRUD, state transitions (pause/resume/cancel), and re-scheduling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.reminder_models import (
    ReminderDetail,
    ReminderListItem,
    ReminderRunListItem,
)
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderRunStatus,
    ReminderStatus,
    ReminderType,
    UserReminder,
    UserReminderRun,
)
from src.infra.messaging.reminder_schedule import schedule_reminder, unschedule_reminder
from src.infra.utils.logger import get_logger

logger = get_logger("reminder_service")

# States from which a reminder can be cancelled
_CANCELLABLE_STATES = {
    ReminderStatus.ACTIVE,
    ReminderStatus.PAUSED,
    ReminderStatus.PENDING_CONFIRMATION,
}

# States in which a reminder can be edited via the API
_EDITABLE_STATES = {ReminderStatus.ACTIVE, ReminderStatus.PAUSED}


class ReminderServiceError(Exception):
    """Base exception for reminder service errors."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ReminderNotFoundError(ReminderServiceError):
    """Raised when reminder is not found or user doesn't own it."""

    def __init__(self, reminder_id: UUID):
        super().__init__(f"Reminder {reminder_id} not found", status_code=404)


class InvalidStatusTransitionError(ReminderServiceError):
    """Raised when a status transition is invalid."""

    def __init__(self, current: str, target: str):
        super().__init__(
            f"Cannot transition from {current} to {target}",
            status_code=400,
        )


class ReminderService:
    """Service for CRUD + state management of user reminders."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        """Release the database session."""
        await self.session.close()

    # ── CREATE ──────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        title: str,
        reminder_type: ReminderType,
        message_content: str | None,
        agent_prompt: str | None,
        deal_id: UUID | None,
        trigger_time: datetime,
        timezone: str,
        recurrence: ReminderRecurrence,
        recurrence_end_at: datetime | None,
        delivery_channel: str,
        delivery_context: dict | None = None,
    ) -> ReminderDetail:
        """Create a reminder and schedule it immediately as ACTIVE."""
        self._validate_timezone(timezone)

        if trigger_time <= datetime.now(UTC):
            raise ReminderServiceError("trigger_time must be in the future")

        agent_config = None
        if reminder_type == ReminderType.AGENTIC and agent_prompt:
            agent_config = {"agent_type": "general", "prompt": agent_prompt}
            if deal_id:
                agent_config["deal_id"] = str(deal_id)

        reminder = UserReminder(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            reminder_type=reminder_type,
            title=title,
            message_content=message_content if reminder_type == ReminderType.STATIC else None,
            agent_config=agent_config,
            next_trigger_at=trigger_time,
            user_timezone=timezone,
            recurrence=recurrence,
            recurrence_end_at=recurrence_end_at,
            delivery_channel=delivery_channel,
            delivery_context=delivery_context or {},
            status=ReminderStatus.ACTIVE,
        )

        self.session.add(reminder)
        await self.session.commit()
        await self.session.refresh(reminder)

        # Redis ZSET is a disposable timing index — recovery sweep re-syncs from DB
        self._safe_schedule(reminder.reminder_id, reminder.next_trigger_at.timestamp())

        return self._to_detail(reminder)

    # ── LIST ────────────────────────────────────────────────────────────

    async def list_reminders(
        self,
        *,
        limit: int = 20,
        page: int = 1,
        statuses: list[ReminderStatus] | None = None,
        reminder_type: ReminderType | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[ReminderListItem], int]:
        """Return paginated list of reminders for the current user."""
        base = select(UserReminder).where(
            col(UserReminder.organisation_id) == self.organisation_id,
            col(UserReminder.user_id) == self.user_id,
        )

        if statuses:
            base = base.where(col(UserReminder.status).in_([s.value for s in statuses]))

        if reminder_type:
            base = base.where(col(UserReminder.reminder_type) == reminder_type.value)

        # Count
        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self.session.execute(count_q)).scalar_one()

        # Sort + paginate
        order_col = col(UserReminder.next_trigger_at)
        base = base.order_by(order_col.asc() if sort_order == "asc" else order_col.desc())
        offset = (page - 1) * limit
        base = base.offset(offset).limit(limit)

        result = await self.session.execute(base)
        reminders = result.scalars().all()

        items = [self._to_list_item(r) for r in reminders]
        return items, total_count

    # ── GET ─────────────────────────────────────────────────────────────

    async def get(self, reminder_id: UUID) -> ReminderDetail:
        """Get a single reminder by ID. Verifies ownership."""
        reminder = await self._fetch_owned(reminder_id)
        return self._to_detail(reminder)

    async def list_history(
        self,
        reminder_id: UUID,
        *,
        limit: int = 20,
        page: int = 1,
    ) -> tuple[list[ReminderRunListItem], int]:
        """Return paginated execution history for a reminder owned by the user."""
        await self._fetch_owned(reminder_id)

        base = select(UserReminderRun).where(
            col(UserReminderRun.organisation_id) == self.organisation_id,
            col(UserReminderRun.user_id) == self.user_id,
            col(UserReminderRun.reminder_id) == reminder_id,
        )

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self.session.execute(count_q)).scalar_one()

        offset = (page - 1) * limit
        base = base.order_by(col(UserReminderRun.started_at).desc()).offset(offset).limit(limit)

        result = await self.session.execute(base)
        runs = result.scalars().all()

        items = [self._to_run_item(r) for r in runs]
        return items, total_count

    # ── UPDATE ──────────────────────────────────────────────────────────

    async def update(
        self,
        reminder_id: UUID,
        updates: dict,
    ) -> ReminderDetail:
        """Update editable fields. Re-schedules in Redis if time/recurrence changed."""
        reminder = await self._fetch_owned(reminder_id)

        if reminder.status not in _EDITABLE_STATES:
            raise ReminderServiceError(
                f"Cannot update a {reminder.status} reminder", status_code=400
            )

        needs_reschedule = False
        is_static = reminder.reminder_type == ReminderType.STATIC.value

        # Cross-validate type vs content fields
        if is_static and ("agent_prompt" in updates or "deal_id" in updates):
            raise ReminderServiceError(
                "agent_prompt and deal_id are not applicable to static reminders"
            )
        if not is_static and "message_content" in updates:
            raise ReminderServiceError("message_content is not applicable to agentic reminders")

        if "title" in updates:
            reminder.title = updates["title"]

        if "message_content" in updates:
            reminder.message_content = updates["message_content"]

        if "agent_prompt" in updates:
            config = reminder.agent_config or {}
            config["prompt"] = updates["agent_prompt"]
            reminder.agent_config = config

        if "deal_id" in updates:
            config = reminder.agent_config or {}
            config["deal_id"] = str(updates["deal_id"]) if updates["deal_id"] else None
            reminder.agent_config = config

        if "trigger_time" in updates:
            new_time = updates["trigger_time"]
            if new_time <= datetime.now(UTC):
                raise ReminderServiceError("trigger_time must be in the future")
            reminder.next_trigger_at = new_time
            needs_reschedule = True

        if "timezone" in updates:
            self._validate_timezone(updates["timezone"])
            reminder.user_timezone = updates["timezone"]

        if "recurrence" in updates:
            reminder.recurrence = updates["recurrence"]
            needs_reschedule = True

        if "recurrence_end_at" in updates:
            reminder.recurrence_end_at = updates["recurrence_end_at"]

        if "delivery_channel" in updates:
            reminder.delivery_channel = updates["delivery_channel"]

        if "delivery_context" in updates:
            reminder.delivery_context = updates["delivery_context"]

        await self.session.commit()
        await self.session.refresh(reminder)

        if needs_reschedule and reminder.status == ReminderStatus.ACTIVE:
            self._safe_unschedule(reminder.reminder_id)
            self._safe_schedule(reminder.reminder_id, reminder.next_trigger_at.timestamp())

        return self._to_detail(reminder)

    # ── STATE TRANSITIONS ───────────────────────────────────────────────

    async def pause(self, reminder_id: UUID) -> ReminderDetail:
        """Pause an ACTIVE reminder. Removes it from the Redis schedule."""
        reminder = await self._fetch_owned(reminder_id)

        if reminder.status != ReminderStatus.ACTIVE:
            raise InvalidStatusTransitionError(reminder.status, ReminderStatus.PAUSED.value)

        reminder.status = ReminderStatus.PAUSED
        await self.session.commit()
        await self.session.refresh(reminder)

        self._safe_unschedule(reminder.reminder_id)

        return self._to_detail(reminder)

    async def resume(self, reminder_id: UUID) -> ReminderDetail:
        """Resume a PAUSED reminder. Re-adds it to the Redis schedule."""
        reminder = await self._fetch_owned(reminder_id)

        if reminder.status != ReminderStatus.PAUSED:
            raise InvalidStatusTransitionError(reminder.status, ReminderStatus.ACTIVE.value)

        if reminder.next_trigger_at <= datetime.now(UTC):
            raise ReminderServiceError(
                "Cannot resume: trigger time is in the past. Update trigger_time first."
            )

        reminder.status = ReminderStatus.ACTIVE
        await self.session.commit()
        await self.session.refresh(reminder)

        self._safe_schedule(reminder.reminder_id, reminder.next_trigger_at.timestamp())

        return self._to_detail(reminder)

    async def cancel(self, reminder_id: UUID) -> ReminderDetail:
        """Cancel a reminder. Terminal state — cannot be undone."""
        reminder = await self._fetch_owned(reminder_id)

        self._ensure_not_running(reminder, action="cancel")

        if reminder.status not in _CANCELLABLE_STATES:
            raise InvalidStatusTransitionError(reminder.status, ReminderStatus.CANCELLED.value)

        reminder.status = ReminderStatus.CANCELLED
        await self.session.commit()
        await self.session.refresh(reminder)

        self._safe_unschedule(reminder.reminder_id)

        return self._to_detail(reminder)

    # ── DELETE ──────────────────────────────────────────────────────────

    async def delete(self, reminder_id: UUID) -> None:
        """Hard-delete a reminder and remove from Redis."""
        reminder = await self._fetch_owned(reminder_id)
        self._ensure_not_running(reminder, action="delete")
        rid = reminder.reminder_id

        await self.session.delete(reminder)
        await self.session.commit()

        self._safe_unschedule(rid)

    # ── HELPERS ─────────────────────────────────────────────────────────

    def _safe_schedule(self, reminder_id: UUID, trigger_epoch: float) -> None:
        """Schedule in Redis ZSET. Logs and continues on failure — recovery sweep re-syncs."""
        try:
            schedule_reminder(self.organisation_id, reminder_id, trigger_epoch)
        except Exception:
            logger.warning("Failed to schedule reminder %s in Redis", reminder_id, exc_info=True)

    def _safe_unschedule(self, reminder_id: UUID) -> None:
        """Remove from Redis ZSET. Logs and continues on failure — poller skips stale entries."""
        try:
            unschedule_reminder(self.organisation_id, reminder_id)
        except Exception:
            logger.warning(
                "Failed to unschedule reminder %s from Redis", reminder_id, exc_info=True
            )

    async def _fetch_owned(self, reminder_id: UUID) -> UserReminder:
        """Fetch a reminder ensuring org + user ownership."""
        stmt = select(UserReminder).where(
            col(UserReminder.reminder_id) == reminder_id,
            col(UserReminder.organisation_id) == self.organisation_id,
            col(UserReminder.user_id) == self.user_id,
        )
        result = await self.session.execute(stmt)
        reminder = result.scalar_one_or_none()

        if not reminder:
            raise ReminderNotFoundError(reminder_id)

        return reminder

    @staticmethod
    def _ensure_not_running(reminder: UserReminder, *, action: str) -> None:
        """Block destructive actions while a reminder execution is in progress."""
        if reminder.status == ReminderStatus.TRIGGERED:
            raise ReminderServiceError(
                f"Cannot {action} a reminder while execution is in progress",
                status_code=409,
            )

    @staticmethod
    def _validate_timezone(tz: str) -> None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, KeyError) as exc:
            raise ReminderServiceError(f"Invalid timezone: {tz}") from exc

    @staticmethod
    def _to_detail(r: UserReminder) -> ReminderDetail:
        agent_prompt = None
        deal_id = None
        if r.agent_config:
            agent_prompt = r.agent_config.get("prompt")
            raw_deal_id = r.agent_config.get("deal_id")
            if raw_deal_id:
                deal_id = UUID(raw_deal_id)

        local_next_trigger, schedule_summary = ReminderService._describe_schedule(
            r.next_trigger_at,
            r.user_timezone,
            ReminderRecurrence(r.recurrence),
        )
        is_running = ReminderService._is_running(r)

        return ReminderDetail(
            reminder_id=r.reminder_id,
            title=r.title,
            reminder_type=ReminderType(r.reminder_type),
            status=ReminderStatus(r.status),
            message_content=r.message_content,
            agent_prompt=agent_prompt,
            deal_id=deal_id,
            next_trigger_at=r.next_trigger_at,
            next_trigger_local_at=local_next_trigger,
            schedule_summary=schedule_summary,
            timezone=r.user_timezone,
            recurrence=ReminderRecurrence(r.recurrence),
            recurrence_end_at=r.recurrence_end_at,
            delivery_channel=r.delivery_channel,
            is_running=is_running,
            current_run_status=ReminderRunStatus.IN_PROGRESS if is_running else None,
            can_pause=ReminderService._can_pause(r),
            can_cancel=ReminderService._can_cancel(r),
            can_delete=ReminderService._can_delete(r),
            last_triggered_at=r.last_triggered_at,
            trigger_count=r.trigger_count,
            consecutive_failures=r.consecutive_failures,
            last_error=r.last_error,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )

    @staticmethod
    def _to_list_item(r: UserReminder) -> ReminderListItem:
        local_next_trigger, schedule_summary = ReminderService._describe_schedule(
            r.next_trigger_at,
            r.user_timezone,
            ReminderRecurrence(r.recurrence),
        )
        is_running = ReminderService._is_running(r)

        return ReminderListItem(
            reminder_id=r.reminder_id,
            title=r.title,
            reminder_type=ReminderType(r.reminder_type),
            status=ReminderStatus(r.status),
            next_trigger_at=r.next_trigger_at,
            next_trigger_local_at=local_next_trigger,
            schedule_summary=schedule_summary,
            timezone=r.user_timezone,
            recurrence=ReminderRecurrence(r.recurrence),
            delivery_channel=r.delivery_channel,
            is_running=is_running,
            current_run_status=ReminderRunStatus.IN_PROGRESS if is_running else None,
            can_pause=ReminderService._can_pause(r),
            can_cancel=ReminderService._can_cancel(r),
            can_delete=ReminderService._can_delete(r),
            last_triggered_at=r.last_triggered_at,
            trigger_count=r.trigger_count,
            created_at=r.created_at,
        )

    @staticmethod
    def _to_run_item(r: UserReminderRun) -> ReminderRunListItem:
        return ReminderRunListItem(
            run_id=r.run_id,
            attempt_number=r.attempt_number,
            status=ReminderRunStatus(r.status),
            triggered_for_at=r.triggered_for_at,
            started_at=r.started_at,
            completed_at=r.completed_at,
            input_snapshot=r.input_snapshot,
            result_text=r.result_text,
            error_message=r.error_message,
        )

    @staticmethod
    def _is_running(reminder: UserReminder) -> bool:
        return ReminderStatus(reminder.status) == ReminderStatus.TRIGGERED

    @staticmethod
    def _can_pause(reminder: UserReminder) -> bool:
        return ReminderStatus(reminder.status) == ReminderStatus.ACTIVE

    @staticmethod
    def _can_cancel(reminder: UserReminder) -> bool:
        return ReminderStatus(reminder.status) in _CANCELLABLE_STATES

    @staticmethod
    def _can_delete(reminder: UserReminder) -> bool:
        return not ReminderService._is_running(reminder)

    @staticmethod
    def _describe_schedule(
        next_trigger_at: datetime,
        timezone_name: str,
        recurrence: ReminderRecurrence,
    ) -> tuple[datetime, str]:
        """Return the next trigger in local time plus a UI-friendly recurrence label."""
        try:
            tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, KeyError):
            tz = ZoneInfo("UTC")

        local_time = next_trigger_at.astimezone(tz)
        time_label = local_time.strftime("%I:%M %p %Z").lstrip("0")
        short_date = local_time.strftime("%a, %b %d")
        weekday = local_time.strftime("%A")

        if recurrence == ReminderRecurrence.NONE:
            summary = f"One-time on {short_date} at {time_label}"
        elif recurrence == ReminderRecurrence.DAILY:
            summary = f"Daily at {time_label} (next: {short_date})"
        elif recurrence == ReminderRecurrence.WEEKDAYS:
            summary = f"Weekdays at {time_label} (next: {weekday}, {short_date})"
        elif recurrence == ReminderRecurrence.WEEKLY:
            summary = f"Weekly on {weekday} at {time_label} (next: {short_date})"
        elif recurrence == ReminderRecurrence.BIWEEKLY:
            summary = f"Every 2 weeks on {weekday} at {time_label} (next: {short_date})"
        else:
            summary = f"Monthly on day {local_time.day} at {time_label} (next: {short_date})"

        return local_time, summary
