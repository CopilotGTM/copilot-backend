"""Celery task for meeting summary ready notifications.

Called after a deal-linked meeting summary job completes. The service uses
NotificationDelivery rows for per-target idempotency, so this task can retry
without sending duplicate emails/channel messages.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.meeting_bot.summary_notification_service import MeetingSummaryNotificationService
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


class MeetingSummaryNotificationRetryableError(RuntimeError):
    """Raised when provider-level failures were persisted and need Celery retry."""


@app.task(
    bind=True,
    name="deal.send_meeting_summary_ready_notification",
    acks_late=True,
    max_retries=3,
)
def send_meeting_summary_ready_notification_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    event_id: str,
):
    """Send idempotent summary-ready notifications for one completed meeting summary.

    The task carries only IDs. It re-loads org-scoped context inside the service so
    queued work cannot send stale or cross-tenant notification content.
    """

    async def _run():
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        event_uuid = UUID(event_id)
        service = MeetingSummaryNotificationService(org_uuid)
        return await service.notify(deal_id=deal_uuid, event_id=event_uuid)

    try:
        result = run_async(_run())
        if int(result.get("failed") or 0) > 0:
            raise MeetingSummaryNotificationRetryableError(
                f"meeting summary notification failed for {result['failed']} target(s)"
            )
        return result
    except Exception as exc:
        # Retry task-level failures; per-target failures are persisted by the service
        # and can be reclaimed without duplicating targets already marked SENT.
        logger.exception(
            "Meeting summary ready notification task failed",
            extra={
                "event": "meeting_summary_ready_notification_task_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "event_id": event_id,
            },
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
