"""Tasks for direct meeting recording dispatch.

These tasks cover three entry points:
- matcher-driven sends for newly linked future meetings
- webhook processing for provider state changes
- a nightly fallback scan to catch active integration events missed by fast paths
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.meeting_bot import (
    MeetingRecordingDispatchService,
    RecallMeetingRecordingWebhookService,
)
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="deal.process_meeting_recording_webhook",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=180,
    time_limit=240,
)
def process_meeting_recording_webhook_task(
    self: Task,
    payload: dict[str, Any],
    webhook_id: str | None = None,
) -> dict[str, Any]:
    """Process a verified Recall webhook outside the request lifecycle.

    This is a worker-side recovery path for outbound provider callbacks. The task is
    intentionally constrained to one webhook payload so failures stay localized and
    retry cost is bounded.
    """
    started_at = datetime.now(UTC).isoformat()
    lock_key = f"task_lock:process_meeting_recording_webhook:{webhook_id or 'unknown'}"

    # The HTTP route already dedupes obvious retries, but the worker lock is the
    # last line of defense when duplicate deliveries race each other.
    if not acquire_task_lock(lock_key, ttl=240):
        return {
            "status": "skipped",
            "reason": "already_running",
            "webhook_id": webhook_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def _run() -> dict[str, Any]:
        organisation_id = RecallMeetingRecordingWebhookService.extract_organisation_id(payload)
        if not organisation_id:
            return {
                "status": "skipped",
                "reason": "missing_organisation_id",
                "webhook_id": webhook_id,
            }

        service = RecallMeetingRecordingWebhookService(UUID(organisation_id))
        result = await service.process_recall_webhook(payload)
        return {
            **result,
            "webhook_id": webhook_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    try:
        return run_async(_run())
    except Exception as exc:
        # Retry transient transport/provider issues, but do not spin forever on
        # permanent validation failures that are likely in bad payloads.
        logger.error(
            "Meeting recording webhook processing failed",
            extra={
                "event": "meeting_recording_webhook_task_failed",
                "webhook_id": webhook_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        return {
            "status": "failed",
            "webhook_id": webhook_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    finally:
        release_task_lock(lock_key)


@app.task(
    bind=True,
    name="deal.send_meeting_recording",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=180,
    time_limit=240,
)
def send_meeting_recording_task(
    self: Task,
    organisation_id: str,
    calendar_event_id: str,
) -> dict[str, Any]:
    """Send one calendar event to the recording provider.

    This is the fast path for newly matched meetings, where the service decides if
    the row should be skipped, already-sent, or retried as sent.
    """
    started_at = datetime.now(UTC).isoformat()
    lock_key = f"task_lock:send_meeting_recording:{organisation_id}:{calendar_event_id}"

    # Event-level locking prevents duplicate bot creation when the matcher and
    # fallback scan notice the same meeting around the same time.
    if not acquire_task_lock(lock_key, ttl=240):
        return {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": organisation_id,
            "calendar_event_id": calendar_event_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def _run() -> dict[str, Any]:
        service = MeetingRecordingDispatchService(UUID(organisation_id))
        result = await service.dispatch_event(UUID(calendar_event_id))
        return {
            **result,
            "organisation_id": organisation_id,
            "calendar_event_id": calendar_event_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    try:
        return run_async(_run())
    except Exception as exc:
        # Single-event sends are idempotent at the service layer, so a retry is
        # safe and preferred for short network/provider flaps.
        logger.error(
            "Meeting recording dispatch failed",
            extra={
                "event": "meeting_recording_task_failed",
                "organisation_id": organisation_id,
                "calendar_event_id": calendar_event_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "calendar_event_id": calendar_event_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    finally:
        release_task_lock(lock_key)


@app.task(
    bind=True,
    name="deal.send_unsent_meeting_recordings_org",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
)
def send_unsent_meeting_recordings_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Fallback scan for upcoming active integration meetings not yet sent to Recall.

    The scan is intentionally eventual: it is useful when a matcher event was missed,
    when previous sends failed before persistence, or when race conditions left a
    queue gap.
    """
    started_at = datetime.now(UTC).isoformat()
    lock_key = f"task_lock:send_unsent_meeting_recordings_org:{organisation_id}"

    # Org-level locking keeps the nightly scan from overlapping with itself while
    # still allowing direct event sends to proceed independently.
    if not acquire_task_lock(lock_key, ttl=660):
        return {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def _run() -> dict[str, Any]:
        service = MeetingRecordingDispatchService(UUID(organisation_id))
        result = await service.dispatch_unsent_upcoming()
        logger.info(
            "Meeting recording fallback org task completed",
            extra={
                "event": "meeting_recording_fallback_org_completed",
                "organisation_id": organisation_id,
                **result,
            },
        )
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **result,
        }

    try:
        return run_async(_run())
    except Exception as exc:
        # Keep the org task as resilient as possible. A scan failure for one tenant
        # should not prevent the worker process from accepting future scan cycles.
        logger.error(
            "Meeting recording fallback org task failed",
            extra={
                "event": "meeting_recording_fallback_org_failed",
                "organisation_id": organisation_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    finally:
        release_task_lock(lock_key)


@app.task(
    name="deal.trigger_send_unsent_meeting_recordings_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def trigger_send_unsent_meeting_recordings_all_orgs_task() -> dict[str, Any]:
    """Tier 1 dispatcher that triggers fallback scan for all approved orgs."""
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        org_ids: list[UUID] = []

        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {"total_orgs": 0, "orgs_triggered": 0, "orgs_skipped": 0},
                "org_tasks": [],
            }

        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                # Fan out per-org work so a failure in one tenant does not block
                # the scan for other approved organisations.
                task = send_unsent_meeting_recordings_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)}
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "task_id": task.id,
                        "status": "triggered",
                    }
                )
                orgs_triggered += 1
            except Exception as exc:
                logger.error(
                    "Failed to dispatch meeting recording fallback task for organisation",
                    extra={
                        "event": "meeting_recording_fallback_dispatch_failed",
                        "organisation_id": str(org_id),
                        "failure_reason": str(exc),
                    },
                    exc_info=True,
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                orgs_skipped += 1

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": len(org_ids),
                "orgs_triggered": orgs_triggered,
                "orgs_skipped": orgs_skipped,
            },
            "org_tasks": org_tasks,
        }

    try:
        return run_async(_dispatch_all_orgs())
    except Exception as exc:
        logger.error(
            "Meeting recording fallback all-org dispatch failed",
            extra={
                "event": "meeting_recording_fallback_all_orgs_failed",
                "failure_reason": str(exc),
            },
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
