"""Celery tasks for firing due reminders using a Redis sorted set scheduler.

poll_due_reminders (Beat, every 5s):
  - Pops due items from the ZSET and dispatches execute_reminder per item.
  - Lightweight: one Redis call, no DB, no per-org fan-out.

recovery_sweep_reminders (Beat, every 5 min):
  - Re-syncs the ZSET from DB for all approved orgs.
  - Handles Redis restart, missed ZADDs, or any drift.

execute_reminder (per-reminder):
  - Delegates to ReminderExecutionService for actual execution.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from celery import Task

from src.infra.utils.async_runner import run_async
from src.workers.chat_worker.celery_app import app

logger = logging.getLogger(__name__)


@app.task(
    name="chat.poll_due_reminders",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=10,
    time_limit=15,
)
def poll_due_reminders_task() -> dict[str, Any]:
    """Pop due reminders from the Redis ZSET and dispatch execution tasks."""
    from src.infra.messaging.reminder_schedule import pop_due_reminders

    claimed = pop_due_reminders(time.time())
    if not claimed:
        return {"status": "idle", "dispatched": 0}

    dispatched = 0
    for org_id, reminder_id in claimed:
        try:
            execute_reminder_task.apply_async(
                kwargs={
                    "organisation_id": org_id,
                    "reminder_id": reminder_id,
                },
                queue="chat_messages",
                routing_key="chat_messages",
            )
            dispatched += 1
        except Exception as e:
            logger.error(
                "Failed to dispatch reminder %s for org %s: %s",
                reminder_id,
                org_id,
                e,
            )

    logger.info("Polled %d due reminders, dispatched %d", len(claimed), dispatched)
    return {"status": "dispatched", "claimed": len(claimed), "dispatched": dispatched}


@app.task(
    name="chat.recovery_sweep_reminders",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def recovery_sweep_reminders_task() -> dict[str, Any]:
    """Re-sync the Redis ZSET from DB for all approved orgs.

    Idempotent — ZADD with the same score is a no-op if the member exists.
    Covers: Redis restart, missed ZADD on creation, any inconsistency.
    """
    logger.info("Starting reminder recovery sweep")

    async def _sweep() -> dict[str, Any]:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select
        from sqlmodel import col

        from src.domain.models.master_tables import Organisation, OrganisationStatus
        from src.domain.models.organisation_tables.reminder_models import (
            ReminderStatus,
            UserReminder,
        )
        from src.infra.database.sessions import (
            acquire_master_async_session,
            acquire_org_async_session,
        )
        from src.infra.messaging.reminder_schedule import schedule_reminder

        # Look ahead 10 min (2x sweep interval) to ensure comfortable overlap
        horizon = datetime.now(UTC) + timedelta(minutes=10)

        org_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        total_synced = 0
        for org_id in org_ids:
            try:
                async for session in acquire_org_async_session(org_id):
                    reminder_stmt = select(
                        col(UserReminder.reminder_id),
                        col(UserReminder.organisation_id),
                        col(UserReminder.next_trigger_at),
                    ).where(
                        col(UserReminder.organisation_id) == org_id,
                        col(UserReminder.status) == ReminderStatus.ACTIVE,
                        col(UserReminder.next_trigger_at) <= horizon,
                    )
                    result = await session.execute(reminder_stmt)
                    rows = result.all()

                    for reminder_id, r_org_id, next_trigger in rows:
                        schedule_reminder(r_org_id, reminder_id, next_trigger.timestamp())
                        total_synced += 1
            except Exception as e:
                logger.error("Recovery sweep failed for org %s: %s", org_id, e)

        logger.info(
            "Recovery sweep complete: %d orgs, %d reminders synced",
            len(org_ids),
            total_synced,
        )
        return {
            "status": "completed",
            "orgs_scanned": len(org_ids),
            "reminders_synced": total_synced,
        }

    return run_async(_sweep())


@app.task(
    name="chat.execute_reminder",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def execute_reminder_task(
    self: Task,
    organisation_id: str,
    reminder_id: str,
) -> dict[str, Any]:
    """Execute a single due reminder (static or agentic)."""
    logger.info("Executing reminder org=%s reminder=%s", organisation_id, reminder_id)

    try:
        from src.workers.chat_worker.services import ReminderExecutionService

        org_uuid = UUID(organisation_id)
        rem_uuid = UUID(reminder_id)

        service = ReminderExecutionService(org_uuid)
        result = service.execute(rem_uuid)

        logger.info(
            "Reminder execution complete org=%s reminder=%s status=%s",
            organisation_id,
            reminder_id,
            result.get("status"),
        )
        return result

    except Exception as exc:
        logger.exception(
            "Reminder execution failed org=%s reminder=%s: %s",
            organisation_id,
            reminder_id,
            exc,
        )
        if self.request.retries < self.max_retries:
            countdown = 30 * (2**self.request.retries)
            raise self.retry(exc=exc, countdown=countdown) from exc
        return {"status": "failed", "error": str(exc)}
