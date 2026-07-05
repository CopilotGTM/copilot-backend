"""Celery task for meeting summary ready notifications.

Called after a meeting summary job completes. The service uses
NotificationDelivery rows for per-target idempotency, so this task can retry
without sending duplicate emails/channel messages.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import acquire_org_async_session
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
    deal_id: str | None,
    event_id: str,
    notification_job_id: str | None = None,
):
    """Send idempotent summary-ready notifications for one completed meeting summary.

    The task requires a durable MEETING_SUMMARY_NOTIFICATION job. Delivery rows
    remain the per-target idempotency ledger, while the job row proves the summary
    flow explicitly authorized this notification run.
    """

    async def _run():
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id) if deal_id else None
        event_uuid = UUID(event_id)
        if not notification_job_id:
            return {"status": "skipped", "reason": "missing_notification_job_id"}

        job_uuid = UUID(notification_job_id)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if (
                not job
                or job.organisation_id != org_uuid
                or job.job_type != DealJobType.MEETING_SUMMARY_NOTIFICATION
                or job.event_id != event_uuid
                or job.deal_id != deal_uuid
            ):
                return {"status": "skipped", "reason": "invalid_notification_job"}

            if job.status == JobStatus.COMPLETED:
                return {"status": "skipped", "reason": "notification_job_completed"}

            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        service = MeetingSummaryNotificationService(org_uuid)
        result = await service.notify(deal_id=deal_uuid, event_id=event_uuid)

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                failed = int(result.get("failed") or 0)
                job.status = JobStatus.FAILED if failed else JobStatus.COMPLETED
                job.result_data = result
                if failed:
                    job.error_message = f"notification failed for {failed} target(s)"
                else:
                    job.error_message = None
                await session.commit()
        return result

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
                "job_id": notification_job_id,
            },
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
