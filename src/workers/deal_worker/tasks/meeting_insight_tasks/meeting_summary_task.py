"""Celery task for meeting summary generation.

This task processes MEETING_SUMMARY jobs:
- Post-meeting summary generation (requires transcript)

Both manual (API-triggered) and automated (pipeline-triggered) jobs use the same
task via DealJobs records. See DealMeetingSummaryPipelineService for automated flow.

Task follows fresh session pattern:
- Phase 1: Load job context (fresh session)
- Phase 2: Call service (service manages its own sessions)
- Phase 3: Persist the result, dispatch downstream work, then complete the job
"""

from __future__ import annotations

from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEventDealAssociation,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.crm.jobs.deal_job_event_publisher import DealJobRedisPublisher
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.meeting_insight_services import (
    MeetingSummaryService,
    NonRetryableJobError,
)
from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_notification_task import (
    send_meeting_summary_ready_notification_task,
)
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline_async,
    get_or_create_deal_memory_job,
)

logger = get_logger(__name__)

# Sentinel user ID for automated pipeline jobs
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _should_dispatch_insight_pipeline_after_summary(
    user_id: UUID,
    job_metadata: dict,
) -> bool:
    """Decide whether this summary task owns downstream deal insight dispatch.

    Scheduled deal-summary chains already own their initiating deal. Recall and
    manual jobs bypass that chain, so this task fans their canonical result out
    to every active deal association unless the job explicitly opts out.
    """
    if bool(job_metadata.get("skip_insight_pipeline")):
        return False
    is_automated = (
        user_id == AUTOMATED_PIPELINE_USER_ID or job_metadata.get("source") == "automated_pipeline"
    )
    return not is_automated or job_metadata.get("source") == "recall_webhook"


def _should_dispatch_summary_ready_notification(job_metadata: dict) -> bool:
    return bool(job_metadata.get("send_summary_notification"))


async def _dispatch_insight_pipeline_for_manual_summary(
    organisation_id: UUID,
    deal_id: UUID,
    *,
    event_id: UUID,
    association_id: UUID | None,
    summary_job_id: UUID,
    source: str = "transcript_upload",
) -> None:
    """Dispatch the deal insight pipeline after a manual meeting summary completes.

    Creates (or reuses) a DEAL_MEMORY_BUILD job and fires the full insight chain:
    deal_memory -> group(risk_blocker, stakeholder_analysis).
    """
    try:
        deal_memory_job_id: UUID | None = None
        async for session in acquire_org_async_session(organisation_id):
            deal_memory_job_id, _ = await get_or_create_deal_memory_job(
                session,
                organisation_id,
                deal_id,
                source=source,
                trigger="meeting_summary_completed",
            )
            await session.commit()

        if deal_memory_job_id:
            await dispatch_deal_insight_pipeline_async(
                str(organisation_id),
                str(deal_id),
                str(deal_memory_job_id),
                source_event_id=str(event_id),
                source_association_id=str(association_id) if association_id else None,
                source_summary_job_id=str(summary_job_id),
            )
            logger.info(
                "Dispatched insight pipeline after manual summary",
                extra={
                    "event": "meeting_summary_insight_pipeline_dispatched",
                    "organisation_id": str(organisation_id),
                    "deal_id": str(deal_id),
                    "job_id": str(deal_memory_job_id),
                },
            )
    except Exception:
        logger.exception(
            "Failed to dispatch insight pipeline after manual summary",
            extra={
                "event": "meeting_summary_insight_pipeline_dispatch_failed",
                "organisation_id": str(organisation_id),
                "deal_id": str(deal_id),
            },
        )
        # Summary data is already durable at this point. Propagate so Celery
        # retries dispatch instead of leaving task automation silently stranded.
        raise


async def _dispatch_event_deal_insight_pipelines(
    organisation_id: UUID,
    event_id: UUID,
    summary_job_id: UUID,
    *,
    source: str,
    exclude_deal_id: UUID | None,
) -> None:
    """Dispatch downstream insight work for every active deal link.

    Scheduled pipelines exclude the deal whose existing Celery chain already
    owns downstream work. Recall and manual sources pass no exclusion and fan
    out to every association after the one canonical summary is durable.
    """
    associations: list[tuple[UUID, UUID]] = []
    async for session in acquire_org_async_session(organisation_id):
        # Event ID can originate from a queued job, so organisation remains the
        # tenant boundary. Release the session before broker-backed dispatch.
        stmt = (
            select(
                col(CalendarEventDealAssociation.deal_id),
                col(CalendarEventDealAssociation.association_id),
            )
            .where(
                col(CalendarEventDealAssociation.organisation_id) == organisation_id,
                col(CalendarEventDealAssociation.event_id) == event_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
            )
            .order_by(col(CalendarEventDealAssociation.deal_id))
        )
        associations = [tuple(row) for row in (await session.execute(stmt)).all()]

    for deal_id, association_id in associations:
        if deal_id == exclude_deal_id:
            continue
        await _dispatch_insight_pipeline_for_manual_summary(
            organisation_id,
            deal_id,
            event_id=event_id,
            association_id=association_id,
            summary_job_id=summary_job_id,
            source=source,
        )


async def _dispatch_summary_ready_notification(
    organisation_id: UUID,
    deal_id: UUID | None,
    event_id: UUID,
    source_summary_job_id: UUID,
) -> None:
    """Create a durable notification job, then queue delivery work.

    Delivery rows are per-target idempotency. This job row is the upstream intent:
    only summary jobs that explicitly create it may cause user-visible sends.
    """
    try:
        notification_job_id: UUID | None = None
        async for session in acquire_org_async_session(organisation_id):
            job = DealJobs(
                organisation_id=organisation_id,
                user_id=AUTOMATED_PIPELINE_USER_ID,
                job_type=DealJobType.MEETING_SUMMARY_NOTIFICATION,
                deal_id=deal_id,
                event_id=event_id,
                status=JobStatus.PENDING,
                job_metadata={
                    "source": "meeting_summary_completed",
                    "source_summary_job_id": str(source_summary_job_id),
                },
            )
            session.add(job)
            await session.flush()
            notification_job_id = job.job_id
            await session.commit()

        if not notification_job_id:
            raise RuntimeError("Meeting summary notification job was not created")

        send_meeting_summary_ready_notification_task.apply_async(
            args=[
                str(organisation_id),
                str(deal_id) if deal_id else None,
                str(event_id),
                str(notification_job_id),
            ],
            queue="deal_jobs",
        )
        logger.info(
            "Queued meeting summary ready notification",
            extra={
                "event": "meeting_summary_ready_notification_queued",
                "organisation_id": str(organisation_id),
                "deal_id": str(deal_id) if deal_id else None,
                "event_id": str(event_id),
                "job_id": str(notification_job_id),
                "source_summary_job_id": str(source_summary_job_id),
            },
        )
    except Exception:
        logger.exception(
            "Failed to queue meeting summary ready notification",
            extra={
                "event": "meeting_summary_ready_notification_queue_failed",
                "organisation_id": str(organisation_id),
                "deal_id": str(deal_id) if deal_id else None,
                "event_id": str(event_id),
                "source_summary_job_id": str(source_summary_job_id),
            },
        )


@app.task(bind=True, name="meeting_summary_task.process", acks_late=True, max_retries=3)
def process_meeting_summary(self: Task, organisation_id: str, job_id: str):
    """Process a meeting summary job.

    Follows fresh session pattern:
    - Phase 1: Load job, verify type, mark IN_PROGRESS (fresh session)
    - Phase 2: Call MeetingSummaryService (service manages its own sessions)
    - Phase 3: Persist the result, dispatch downstream work, and mark completion

    Args:
        organisation_id: The organisation ID (as string for JSON serialization)
        job_id: The job ID to process (as string for JSON serialization)

    Returns:
        Dictionary with job completion status
    """
    logger.info(
        "Processing meeting summary job",
        extra={
            "event": "meeting_summary_job_processing",
            "organisation_id": str(organisation_id),
            "job_id": str(job_id),
        },
    )

    lock_key: str | None = None
    lock_acquired = False

    async def _run():
        nonlocal lock_acquired, lock_key
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        # Initialize variables that will be set in the session block
        deal_id: UUID | None
        event_id: UUID
        job_type_str: str
        user_id: UUID
        job_metadata: dict
        stored_result: dict | None

        async for session in acquire_org_async_session(org_uuid):
            # Load only the durable job context here, then release the session before
            # summary generation. The service owns its own fresh-session pattern.
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            # Verify job type
            if job.job_type != DealJobType.MEETING_SUMMARY:
                raise RuntimeError(f"Invalid job type {job.job_type} for meeting summary task")

            # Extract values before closing session
            if not job.event_id:
                raise NonRetryableJobError("Meeting summary requires event_id", "invalid_data")
            if job.status == JobStatus.COMPLETED:
                return {
                    "status": "completed",
                    "job_id": job_id,
                    "job_type": str(DealJobType.MEETING_SUMMARY),
                }

            deal_id = job.deal_id
            event_id = job.event_id
            job_type_str = str(job.job_type)
            user_id = job.user_id
            job_metadata = job.job_metadata or {}
            stored_result = job.result_data

            # Every source can create a differently scoped DealJobs row, but
            # the expensive output belongs to the event. This is the final
            # single-flight boundary if legacy duplicate rows reach the queue.
            lock_key = f"task_lock:meeting_summary_event:{org_uuid}:{event_id}"
            if not acquire_task_lock(lock_key, ttl=700):
                logger.info(
                    "Meeting summary event already running, skipping",
                    extra={
                        "event": "meeting_summary_job_skipped",
                        "organisation_id": str(org_uuid),
                        "job_id": str(job_uuid),
                        "event_id": str(event_id),
                        "failure_reason": "already_running",
                    },
                )
                return {"status": "skipped", "reason": "already_running"}
            lock_acquired = True

            # Mark as IN_PROGRESS
            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        # Determine if automated (for logging)
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )

        logger.info(
            "Loaded meeting summary job",
            extra={
                "event": "meeting_summary_job_loaded",
                "organisation_id": str(org_uuid),
                "job_id": str(job_uuid),
                "deal_id": str(deal_id) if deal_id else None,
                "event_id": str(event_id),
                "automated": is_automated,
            },
        )

        # Only deal-linked summaries are user-visible job streams. Non-deal
        # Recall summaries still notify attendees, but do not publish job SSE.
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str) if deal_id else None

        # Phase 2: Call service (service manages its own sessions)
        service = MeetingSummaryService(
            organisation_id=org_uuid,
            deal_id=deal_id,
            event_id=event_id,
            publisher=publisher,
        )
        try:
            if stored_result is None:
                logger.info(
                    "Generating meeting summary",
                    extra={
                        "event": "meeting_summary_generating",
                        "organisation_id": str(org_uuid),
                        "job_id": str(job_uuid),
                        "deal_id": str(deal_id) if deal_id else None,
                        "event_id": str(event_id),
                        "automated": is_automated,
                    },
                )
                # Deal linkage controls SSE and downstream fanout, not summary
                # content. One subject-centric artifact is shared by all links.
                result = await service.generate_meeting_level_summary()
            else:
                result = stored_result

        except NonRetryableJobError as e:
            logger.info(
                "Meeting summary job permanently failed",
                extra={
                    "event": "meeting_summary_job_failed_permanently",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id) if deal_id else None,
                    "event_id": str(event_id),
                    "error_code": e.error_code,
                    "automated": is_automated,
                },
            )

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)
                    await session.commit()

            if publisher:
                publisher.publish_error(str(e), e.error_code)
                publisher.publish_finalize("error")

            return {
                "status": "failed",
                "job_id": job_id,
                "error": str(e),
                "error_code": e.error_code,
                "retryable": False,
            }

        except Exception as e:
            logger.exception(
                "Meeting summary job failed",
                extra={
                    "event": "meeting_summary_job_failed",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id) if deal_id else None,
                    "event_id": str(event_id),
                    "automated": is_automated,
                },
            )

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)[:500]
                    job.retry_count += 1
                    await session.commit()

            if publisher:
                publisher.publish_finalize("error")
            raise

        # Persist the expensive artifact before broker-backed fanout. A retry can
        # reuse it while the job stays IN_PROGRESS until downstream dispatch succeeds.
        token_usage = service.last_token_usage if stored_result is None else None
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.result_data = result
                if token_usage:
                    job.token_usage = token_usage.to_dict()
                    job.model_name = token_usage.model_name
                    if token_usage.cost is not None:
                        from decimal import Decimal

                        job.cost = Decimal(str(token_usage.cost))
                    if token_usage.duration_seconds is not None:
                        job.latency_ms = int(token_usage.duration_seconds * 1000)
                await session.commit()

        if publisher:
            publisher.publish_result(result)

        if _should_dispatch_summary_ready_notification(job_metadata):
            await _dispatch_summary_ready_notification(org_uuid, deal_id, event_id, job_uuid)

        # Recall/manual jobs own the full event fanout. Scheduled chains already
        # own their initiating deal, so the worker dispatches only the other links.
        if _should_dispatch_insight_pipeline_after_summary(user_id, job_metadata):
            fanout_source = (
                "recall_webhook"
                if job_metadata.get("source") == "recall_webhook"
                else "transcript_upload"
            )
            await _dispatch_event_deal_insight_pipelines(
                org_uuid,
                event_id,
                job_uuid,
                source=fanout_source,
                exclude_deal_id=None,
            )
        elif job_metadata.get("source") == "automated_pipeline" and deal_id:
            await _dispatch_event_deal_insight_pipelines(
                org_uuid,
                event_id,
                job_uuid,
                source="automated_pipeline",
                exclude_deal_id=deal_id,
            )

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                await session.commit()

        if publisher:
            publisher.publish_finalize("done")

        logger.info(
            "Meeting summary job completed",
            extra={
                "event": "meeting_summary_job_completed",
                "organisation_id": str(org_uuid),
                "job_id": str(job_uuid),
                "deal_id": str(deal_id) if deal_id else None,
                "event_id": str(event_id),
                "automated": is_automated,
            },
        )

        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": str(DealJobType.MEETING_SUMMARY),
        }

    try:
        return run_async(_run())
    except NonRetryableJobError:
        logger.warning(
            "NonRetryableJobError escaped meeting summary task",
            extra={
                "event": "meeting_summary_task_nonretryable_escape",
                "organisation_id": str(organisation_id),
                "job_id": str(job_id),
            },
        )
        return {
            "status": "failed",
            "job_id": job_id,
            "retryable": False,
        }
    except Exception as e:
        logger.exception(
            "Meeting summary task failed",
            extra={
                "event": "meeting_summary_task_failed",
                "organisation_id": str(organisation_id),
                "job_id": str(job_id),
            },
        )
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
    finally:
        if lock_acquired and lock_key:
            release_task_lock(lock_key)
