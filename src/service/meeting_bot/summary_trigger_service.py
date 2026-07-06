"""Transcript-ready summary job trigger for meeting recordings."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.integrations.transcripts.service import TranscriptService
from src.workers.deal_worker.client import send_deal_task_async

_SUMMARY_ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.IN_PROGRESS)
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

logger = get_logger(__name__)


class MeetingSummaryTriggerService:
    """Materialize transcripts and enqueue event-scoped summary jobs."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id
        self.transcript_service = TranscriptService(organisation_id)

    async def handle_transcript_ready(self, event_id: UUID) -> dict[str, Any]:
        """Download the Recall transcript and enqueue summary jobs for one event."""
        lock_key = f"task_lock:recall_transcript_materialize:{self.organisation_id}:{event_id}"
        if not acquire_task_lock(lock_key, ttl=300):
            return {
                "transcript_materialized": False,
                "summary_jobs_created": 0,
                "summary_jobs_skipped": 0,
                "reason": "materialization_already_running",
            }

        try:
            transcript_result = await self.transcript_service.materialize_recall_transcript(
                event_id
            )
            if not transcript_result.success:
                if transcript_result.should_retry:
                    # Retryable materialization failures should bubble to the Celery
                    # task so the original webhook delivery can retry as one unit.
                    raise RuntimeError(transcript_result.error or "Recall transcript not ready")
                return {
                    "transcript_materialized": False,
                    "summary_jobs_created": 0,
                    "summary_jobs_skipped": 0,
                    "transcript_materialization_error": transcript_result.error,
                }

            enqueue_plan = await self._create_meeting_summary_jobs(event_id)
            enqueue_failures = 0
            for job_id in enqueue_plan["job_ids_to_enqueue"]:
                try:
                    # The durable job is committed before this enqueue. If broker
                    # dispatch fails, raising here lets the webhook task retry and
                    # re-enqueue any still-pending job without creating a duplicate.
                    await send_deal_task_async(
                        "meeting_summary_task.process",
                        args=[str(self.organisation_id), str(job_id)],
                        queue="deal_jobs",
                        routing_key="deal_jobs",
                    )
                except Exception:
                    enqueue_failures += 1
                    logger.exception(
                        "Failed to enqueue Recall webhook meeting summary job",
                        extra={
                            "event": "recall_webhook_summary_enqueue_failed",
                            "organisation_id": str(self.organisation_id),
                            "calendar_event_id": str(event_id),
                            "job_id": str(job_id),
                        },
                    )

            if enqueue_failures:
                raise RuntimeError(
                    f"Failed to enqueue {enqueue_failures} Recall webhook summary job(s)"
                )

            return {
                "transcript_materialized": True,
                "summary_jobs_created": enqueue_plan["created"],
                "summary_jobs_skipped": enqueue_plan["skipped"],
            }
        finally:
            release_task_lock(lock_key)

    async def _create_meeting_summary_jobs(self, event_id: UUID) -> dict[str, Any]:
        """Create or reuse summary jobs after a Recall transcript becomes durable."""
        job_ids_to_enqueue: list[UUID] = []
        created = 0
        skipped = 0

        async for session in acquire_org_async_session(self.organisation_id):
            assoc_stmt = select(CalendarEventDealAssociation).where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.event_id) == event_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
            )
            associations = list((await session.execute(assoc_stmt)).scalars().all())
            for association in associations:
                if (
                    association.meeting_summary_status == GenerationStatus.COMPLETED
                    and association.meeting_summary_data
                ):
                    skipped += 1
                    continue

                active_job_stmt = select(DealJobs).where(
                    col(DealJobs.organisation_id) == self.organisation_id,
                    col(DealJobs.deal_id) == association.deal_id,
                    col(DealJobs.event_id) == event_id,
                    col(DealJobs.job_type) == DealJobType.MEETING_SUMMARY,
                    col(DealJobs.status).in_(_SUMMARY_ACTIVE_STATUSES),
                )
                active_job = (await session.execute(active_job_stmt)).scalars().first()
                if active_job:
                    skipped += 1
                    if active_job.status == JobStatus.PENDING:
                        job_ids_to_enqueue.append(active_job.job_id)
                    continue

                job = DealJobs(
                    organisation_id=self.organisation_id,
                    deal_id=association.deal_id,
                    event_id=event_id,
                    job_type=DealJobType.MEETING_SUMMARY,
                    status=JobStatus.PENDING,
                    user_id=AUTOMATED_PIPELINE_USER_ID,
                    job_metadata={
                        "source": "recall_webhook",
                        "trigger": "transcript_done",
                        "send_summary_notification": True,
                        "event_id": str(event_id),
                        "association_id": str(association.association_id),
                    },
                )
                session.add(job)
                await session.flush()
                job_ids_to_enqueue.append(job.job_id)
                created += 1

            if not associations:
                active_job_stmt = select(DealJobs).where(
                    col(DealJobs.organisation_id) == self.organisation_id,
                    col(DealJobs.deal_id).is_(None),
                    col(DealJobs.event_id) == event_id,
                    col(DealJobs.job_type) == DealJobType.MEETING_SUMMARY,
                    col(DealJobs.status).in_(_SUMMARY_ACTIVE_STATUSES),
                )
                active_job = (await session.execute(active_job_stmt)).scalars().first()
                if active_job:
                    skipped += 1
                    if active_job.status == JobStatus.PENDING:
                        job_ids_to_enqueue.append(active_job.job_id)
                else:
                    meeting_stmt = select(Meeting).where(
                        col(Meeting.organisation_id) == self.organisation_id,
                        col(Meeting.calendar_event_id) == event_id,
                        col(Meeting.is_deleted).is_(False),
                    )
                    meeting = (await session.execute(meeting_stmt)).scalars().first()
                    if (
                        meeting
                        and meeting.meeting_summary_status == GenerationStatus.COMPLETED
                        and meeting.meeting_summary_data
                    ):
                        skipped += 1
                    else:
                        job = DealJobs(
                            organisation_id=self.organisation_id,
                            deal_id=None,
                            event_id=event_id,
                            job_type=DealJobType.MEETING_SUMMARY,
                            status=JobStatus.PENDING,
                            user_id=AUTOMATED_PIPELINE_USER_ID,
                            job_metadata={
                                "source": "recall_webhook",
                                "trigger": "transcript_done",
                                "send_summary_notification": True,
                                "summary_scope": "meeting",
                                "event_id": str(event_id),
                                "skip_insight_pipeline": True,
                            },
                        )
                        session.add(job)
                        await session.flush()
                        job_ids_to_enqueue.append(job.job_id)
                        created += 1

            await session.commit()

        return {
            "created": created,
            "skipped": skipped,
            "job_ids_to_enqueue": job_ids_to_enqueue,
        }
