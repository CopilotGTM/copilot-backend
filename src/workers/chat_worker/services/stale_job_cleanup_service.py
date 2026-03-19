"""Stale job cleanup service for chat worker.

Marks stuck chat jobs, triggered reminders, and stale reminder runs as FAILED
so they don't stay frozen forever after agent crashes/timeouts.

Three cleanup passes, each with a fresh DB session:

1. STALE CHAT JOBS — ChatMessageJob records stuck in PENDING (>60min) or
   IN_PROGRESS (>30min). These are agent runs that crashed or timed out.

2. STUCK TRIGGERED REMINDERS — UserReminder records left in TRIGGERED status
   (>15min). _load_and_lock() sets TRIGGERED and commits, but if the process
   crashes before _mark_success()/_mark_failure(), the reminder never fires
   again because _load_and_lock() only accepts ACTIVE status. We reset to
   ACTIVE (with incremented consecutive_failures) or FAILED if max retries
   exceeded, and re-add recoverable ones to the Redis ZSET.

3. STALE REMINDER RUNS — UserReminderRun records stuck IN_PROGRESS (>15min).
   Mark as FAILED with error_message and completed_at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlmodel import col

from src.domain.models.organisation_tables.job_models import (
    ChatMessageJob,
    JobStatus,
)
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRunStatus,
    ReminderStatus,
    UserReminder,
    UserReminderRun,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.reminder_schedule import schedule_reminder
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("chat_stale_job_cleanup_service")

# Thresholds
CHAT_JOB_PENDING_THRESHOLD_MIN = 60
CHAT_JOB_IN_PROGRESS_THRESHOLD_MIN = 30
REMINDER_TRIGGERED_THRESHOLD_MIN = 30
REMINDER_RUN_THRESHOLD_MIN = 30

# Max consecutive failures before permanently failing a reminder
MAX_CONSECUTIVE_FAILURES = 3


class ChatStaleJobCleanupService:
    """Cleans up stuck chat jobs and reminders for one organisation.

    Called by the per-org Tier 2 Celery task (cleanup_stale_chat_jobs_org_task).
    Each pass uses a fresh DB session so connections aren't held across passes.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    def cleanup_stale_jobs(self) -> dict:
        return run_async(self._run())

    async def _run(self) -> dict:
        now = datetime.now(UTC)
        counts: dict[str, int] = {
            "stale_chat_jobs": 0,
            "stuck_triggered_reminders": 0,
            "stale_reminder_runs": 0,
        }

        # ── Pass 1: Stale ChatMessageJobs ─────────────────────────────
        counts["stale_chat_jobs"] = await self._cleanup_stale_chat_jobs(now)

        # ── Pass 2: Stuck TRIGGERED reminders ─────────────────────────
        counts["stuck_triggered_reminders"] = await self._cleanup_stuck_reminders(now)

        # ── Pass 3: Stale UserReminderRuns ────────────────────────────
        counts["stale_reminder_runs"] = await self._cleanup_stale_reminder_runs(now)

        logger.info(
            "Cleanup done for org=%s: stale_chat_jobs=%d, stuck_reminders=%d, stale_runs=%d",
            self.organisation_id,
            counts["stale_chat_jobs"],
            counts["stuck_triggered_reminders"],
            counts["stale_reminder_runs"],
        )

        return {"organisation_id": str(self.organisation_id), **counts}

    async def _cleanup_stale_chat_jobs(self, now: datetime) -> int:
        """Mark PENDING (>60min) and IN_PROGRESS (>30min) chat jobs as FAILED."""
        total = 0

        thresholds = [
            (JobStatus.PENDING, CHAT_JOB_PENDING_THRESHOLD_MIN, "pending"),
            (JobStatus.IN_PROGRESS, CHAT_JOB_IN_PROGRESS_THRESHOLD_MIN, "in-progress"),
        ]

        for status, threshold_min, label in thresholds:
            # updated_at < cutoff ≡ "last touched more than threshold_min ago"
            cutoff = now - timedelta(minutes=threshold_min)
            error = (
                f"Chat job stuck in {label} beyond {threshold_min}min"
                f" (cleanup at {now.isoformat()})"
            )

            async for session in acquire_org_async_session(self.organisation_id):
                job_ids = (
                    (
                        await session.execute(
                            select(col(ChatMessageJob.job_id)).where(
                                col(ChatMessageJob.status) == status,
                                col(ChatMessageJob.updated_at) < cutoff,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                if not job_ids:
                    continue

                await session.execute(
                    update(ChatMessageJob)
                    .where(col(ChatMessageJob.job_id).in_(job_ids))
                    .values(status=JobStatus.FAILED, error_message=error)
                )
                await session.commit()
                total += len(job_ids)

                logger.info(
                    "Marked %d stale %s chat jobs as FAILED (>%dmin) for org=%s",
                    len(job_ids),
                    label,
                    threshold_min,
                    self.organisation_id,
                )

        return total

    async def _cleanup_stuck_reminders(self, now: datetime) -> int:
        """Reset stuck TRIGGERED reminders to ACTIVE or FAILED."""
        # updated_at < cutoff ≡ "stuck in TRIGGERED for longer than threshold"
        cutoff = now - timedelta(minutes=REMINDER_TRIGGERED_THRESHOLD_MIN)
        error = (
            f"Reminder stuck in TRIGGERED beyond {REMINDER_TRIGGERED_THRESHOLD_MIN}min"
            f" (cleanup at {now.isoformat()})"
        )

        # Fetch stuck reminders with their failure counts
        reminder_ids_to_reactivate: list[tuple[UUID, float]] = []
        total = 0

        async for session in acquire_org_async_session(self.organisation_id):
            rows = (
                await session.execute(
                    select(
                        col(UserReminder.reminder_id),
                        col(UserReminder.consecutive_failures),
                        col(UserReminder.next_trigger_at),
                    ).where(
                        col(UserReminder.status) == ReminderStatus.TRIGGERED,
                        col(UserReminder.updated_at) < cutoff,
                    )
                )
            ).all()

            if not rows:
                continue

            # Split by whether they've exceeded max failures
            reactivate_ids: list[UUID] = []
            fail_ids: list[UUID] = []

            for reminder_id, failures, next_trigger_at in rows:
                new_failures = failures + 1
                if new_failures >= MAX_CONSECUTIVE_FAILURES:
                    fail_ids.append(reminder_id)
                else:
                    reactivate_ids.append(reminder_id)
                    reminder_ids_to_reactivate.append((reminder_id, next_trigger_at.timestamp()))

            # Bulk update: reactivate recoverable reminders
            if reactivate_ids:
                await session.execute(
                    update(UserReminder)
                    .where(col(UserReminder.reminder_id).in_(reactivate_ids))
                    .values(
                        status=ReminderStatus.ACTIVE,
                        consecutive_failures=UserReminder.consecutive_failures + 1,
                        last_error=error,
                    )
                )

            # Bulk update: permanently fail exhausted reminders
            if fail_ids:
                await session.execute(
                    update(UserReminder)
                    .where(col(UserReminder.reminder_id).in_(fail_ids))
                    .values(
                        status=ReminderStatus.FAILED,
                        last_error=error,
                    )
                )

            await session.commit()
            total = len(rows)

            logger.info(
                "Stuck TRIGGERED reminders for org=%s: reactivated=%d, failed=%d",
                self.organisation_id,
                len(reactivate_ids),
                len(fail_ids),
            )

        # Session released — re-add reactivated reminders to Redis ZSET
        for reminder_id, trigger_epoch in reminder_ids_to_reactivate:
            try:
                schedule_reminder(self.organisation_id, reminder_id, trigger_epoch)
            except Exception:
                logger.warning(
                    "Failed to re-schedule reminder %s for org=%s",
                    reminder_id,
                    self.organisation_id,
                    exc_info=True,
                )

        return total

    async def _cleanup_stale_reminder_runs(self, now: datetime) -> int:
        """Mark IN_PROGRESS reminder runs older than threshold as FAILED."""
        # started_at < cutoff ≡ "running for longer than threshold"
        cutoff = now - timedelta(minutes=REMINDER_RUN_THRESHOLD_MIN)
        error = (
            f"Reminder run stuck in IN_PROGRESS beyond {REMINDER_RUN_THRESHOLD_MIN}min"
            f" (cleanup at {now.isoformat()})"
        )
        total = 0

        async for session in acquire_org_async_session(self.organisation_id):
            run_ids = (
                (
                    await session.execute(
                        select(col(UserReminderRun.run_id)).where(
                            col(UserReminderRun.status) == ReminderRunStatus.IN_PROGRESS,
                            col(UserReminderRun.started_at) < cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )

            if not run_ids:
                continue

            await session.execute(
                update(UserReminderRun)
                .where(col(UserReminderRun.run_id).in_(run_ids))
                .values(
                    status=ReminderRunStatus.FAILED,
                    error_message=error,
                    completed_at=now,
                )
            )
            await session.commit()
            total = len(run_ids)

            logger.info(
                "Marked %d stale reminder runs as FAILED (>%dmin) for org=%s",
                len(run_ids),
                REMINDER_RUN_THRESHOLD_MIN,
                self.organisation_id,
            )

        return total
