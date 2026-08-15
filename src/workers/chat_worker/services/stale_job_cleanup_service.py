"""Delayed-work recovery for attachments and reminders owned by the chat worker.

Three passes run with fresh organization sessions:

1. ATTACHMENTS - republish post-upload work left in PROCESSING after the
   producer or worker lost the original task.

2. STUCK TRIGGERED REMINDERS - reset recoverable reminders to ACTIVE or fail
   them after the retry ceiling. This covers crashes after the TRIGGERED commit.

3. STALE REMINDER RUNS - mark runs stuck IN_PROGRESS for 30 minutes as FAILED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlmodel import col

from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
)
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRunStatus,
    ReminderStatus,
    UserReminder,
    UserReminderRun,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.reminder_schedule import schedule_reminder
from src.infra.messaging.task_dispatcher import DEFAULT_TASK_PUBLISH_TIMEOUT_SECONDS
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("chat_stale_job_cleanup_service")

# Thresholds
REMINDER_TRIGGERED_THRESHOLD_MIN = 30
REMINDER_RUN_THRESHOLD_MIN = 30
# Post-upload tasks have a 10-minute hard limit. Waiting two extra minutes keeps
# legitimate long processing from being duplicated while still repairing lost work.
ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES = 12

# Scheduled sweeps normally recover 20 rows across all tenants. The hard cap
# protects Redis and downstream workers even when a manual caller asks for more.
DEFAULT_STALE_RECOVERY_LIMIT = 20
MAX_STALE_RECOVERY_LIMIT = 30
ATTACHMENT_RECOVERY_BATCH_SIZE = MAX_STALE_RECOVERY_LIMIT

# Max consecutive failures before permanently failing a reminder
MAX_CONSECUTIVE_FAILURES = 3


async def enqueue_attachment_post_upload(
    organisation_id: UUID,
    attachment_id: UUID,
) -> str:
    """Republish one attachment through the existing post-upload queue."""
    from src.workers.chat_worker.client import send_chat_task_async

    return await send_chat_task_async(
        "attachments.post_upload",
        args=[str(organisation_id), str(attachment_id)],
        queue="attachments",
        routing_key="attachments",
        timeout_seconds=DEFAULT_TASK_PUBLISH_TIMEOUT_SECONDS,
    )


class ChatStaleJobCleanupService:
    """Recovers delayed attachment and reminder work for one organisation.

    Called by the per-org Tier 2 Celery task (cleanup_delayed_work_org_task).
    Each pass uses a fresh DB session so connections aren't held across passes.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    def cleanup_stale_jobs(self, *, limit: int = DEFAULT_STALE_RECOVERY_LIMIT) -> dict:
        """Recover at most ``limit`` stale rows, clamped to the hard ceiling."""
        return run_async(self.cleanup_stale_jobs_async(limit=limit))

    async def cleanup_stale_jobs_async(
        self,
        *,
        limit: int = DEFAULT_STALE_RECOVERY_LIMIT,
    ) -> dict:
        """Provide the bounded cleanup contract to async cross-tenant coordinators."""
        return await self._run(limit=limit)

    async def _run(self, *, limit: int = DEFAULT_STALE_RECOVERY_LIMIT) -> dict:
        """Spend one shared row budget across attachment and reminder passes."""
        effective_limit = min(max(limit, 0), MAX_STALE_RECOVERY_LIMIT)
        now = datetime.now(UTC)
        counts: dict[str, int] = {
            "attachments_requeued": 0,
            "attachment_requeue_failed": 0,
            "stuck_triggered_reminders": 0,
            "stale_reminder_runs": 0,
        }

        attachment_counts = await self._recover_stale_attachments(now, effective_limit)
        counts["attachments_requeued"] = attachment_counts["requeued"]
        counts["attachment_requeue_failed"] = attachment_counts["requeue_failed"]
        processed_count = attachment_counts["requeued"] + attachment_counts["requeue_failed"]

        remaining = effective_limit - processed_count
        if remaining:
            counts["stuck_triggered_reminders"] = await self._cleanup_stuck_reminders(
                now, remaining
            )
            processed_count += counts["stuck_triggered_reminders"]

        remaining = effective_limit - processed_count
        if remaining:
            counts["stale_reminder_runs"] = await self._cleanup_stale_reminder_runs(now, remaining)
            processed_count += counts["stale_reminder_runs"]

        logger.info(
            "Cleanup done for org=%s: attachments_requeued=%d, "
            "attachment_requeue_failed=%d, stuck_reminders=%d, stale_runs=%d",
            self.organisation_id,
            counts["attachments_requeued"],
            counts["attachment_requeue_failed"],
            counts["stuck_triggered_reminders"],
            counts["stale_reminder_runs"],
        )

        return {
            "organisation_id": str(self.organisation_id),
            "processed_count": processed_count,
            **counts,
        }

    async def _recover_stale_attachments(
        self,
        now: datetime,
        limit: int = ATTACHMENT_RECOVERY_BATCH_SIZE,
    ) -> dict[str, int]:
        """Lease and republish attachments whose post-upload work disappeared."""
        attachment_ids = await self._lease_stale_attachments(now, limit=limit)
        counts = {"requeued": 0, "requeue_failed": 0}

        for attachment_id in attachment_ids:
            try:
                await enqueue_attachment_post_upload(self.organisation_id, attachment_id)
            except Exception:
                # The lease expires through updated_at, so a broker outage retries
                # on a later sweep without holding this cleanup task open.
                counts["requeue_failed"] += 1
                logger.error(
                    "Failed to republish attachment post-upload task attachment=%s",
                    attachment_id,
                    exc_info=True,
                )
                continue
            counts["requeued"] += 1

        return counts

    async def _lease_stale_attachments(
        self,
        now: datetime,
        *,
        limit: int = ATTACHMENT_RECOVERY_BATCH_SIZE,
    ) -> list[UUID]:
        """Claim one bounded batch before broker I/O so cleanup workers cannot race."""
        cutoff = now - timedelta(minutes=ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES)

        async for session in acquire_org_async_session(self.organisation_id):
            statement = (
                select(Attachment)
                .where(
                    col(Attachment.status) == AttachmentStatus.PROCESSING,
                    col(Attachment.is_deleted).is_(False),
                    col(Attachment.storage_path) != "",
                    col(Attachment.updated_at) < cutoff,
                )
                .order_by(col(Attachment.updated_at))
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            attachments = list((await session.execute(statement)).scalars().all())
            if not attachments:
                return []

            # Commit the lease before touching Redis. A failed publication becomes
            # eligible again after the same bounded recovery interval.
            for attachment in attachments:
                attachment.updated_at = now
                session.add(attachment)
            await session.commit()
            return [attachment.attachment_id for attachment in attachments]

        return []

    async def _cleanup_stuck_reminders(self, now: datetime, limit: int) -> int:
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
                    )
                    .where(
                        col(UserReminder.status) == ReminderStatus.TRIGGERED,
                        col(UserReminder.updated_at) < cutoff,
                    )
                    .order_by(col(UserReminder.updated_at))
                    .limit(limit)
                    .with_for_update(skip_locked=True)
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

    async def _cleanup_stale_reminder_runs(self, now: datetime, limit: int) -> int:
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
                        select(col(UserReminderRun.run_id))
                        .where(
                            col(UserReminderRun.status) == ReminderRunStatus.IN_PROGRESS,
                            col(UserReminderRun.started_at) < cutoff,
                        )
                        .order_by(col(UserReminderRun.started_at))
                        .limit(limit)
                        .with_for_update(skip_locked=True)
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
