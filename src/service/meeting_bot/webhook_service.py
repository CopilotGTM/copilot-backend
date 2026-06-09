"""Worker-side Recall webhook processing for meeting recording sessions.

Called from the Celery webhook task after the HTTP layer verifies the request.
This service maps raw Recall events onto the normalized session record stored in
the organisation schema. It is intentionally small and event-driven because webhook
timing is not guaranteed to be strictly ordered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
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
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.config.configs import recall_config
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.integrations.meeting_recording.providers import RecallMeetingRecordingProvider
from src.service.integrations.transcripts.service import TranscriptService
from src.service.meeting_bot.session_service import MeetingRecordingSessionService
from src.workers.deal_worker.client import send_deal_task_async

TRANSCRIPT_ARTIFACT_READY_EVENTS = {"transcript.done"}
# Only active work blocks a new summary job. Completed jobs are handled by the
# summary storage checks below so replayed webhooks remain idempotent.
_SUMMARY_ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.IN_PROGRESS)
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

logger = get_logger(__name__)


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else value


class RecallMeetingRecordingWebhookService:
    """Apply Recall webhook events to org-scoped recording sessions.

    The service performs tenant-scoped state transitions only. Provider callbacks can
    be retried, delayed, or duplicated, so each transition is written to the same
    persisted session row.
    """

    def __init__(self, organisation_id: UUID) -> None:
        """Bind callback handling to one tenant context."""
        self.organisation_id = organisation_id
        self.session_service = MeetingRecordingSessionService(organisation_id)
        self.transcript_service = TranscriptService(organisation_id)

    @staticmethod
    def extract_organisation_id(payload: dict[str, Any]) -> str | None:
        """Recover the tenant boundary from the bot metadata we created on send."""
        metadata = ((payload.get("data") or {}).get("bot") or {}).get("metadata") or {}
        organisation_id = metadata.get("organisation_id")
        return organisation_id if isinstance(organisation_id, str) else None

    async def process_recall_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one Recall event and persist the resulting normalized state.

        Return status is either:
        - "processed": a session row existed and was updated
        - "skipped": event is unsupported, duplicate, or not tied to a local session
        """
        event_name = str(payload.get("event") or "")
        data = payload.get("data") or {}
        bot = data.get("bot") or {}
        bot_id = bot.get("id")
        if not isinstance(bot_id, str) or not bot_id:
            return {
                "status": "skipped",
                "reason": "missing_bot_id",
                "event": event_name,
            }

        # Provider bot ids are not trusted across tenants; the service stays
        # org-scoped and looks up only rows within the recovered organisation.
        existing = await self.session_service.get_by_provider_session_id(bot_id)
        if not existing:
            return {
                "status": "skipped",
                "reason": "session_not_found",
                "event": event_name,
                "provider_session_id": bot_id,
            }

        provider_data = data.get("data") or {}
        provider_status = str(provider_data.get("code") or event_name or "")
        updated_at = self._parse_timestamp(provider_data.get("updated_at"))
        status_message = self._build_status_message(provider_data)

        # Preserve the last provider snapshot so debugging one session does not
        # require reconstructing state from logs plus multiple webhook deliveries.
        merged_payload = dict(existing.provider_payload or {})
        merged_payload["last_webhook_event_name"] = event_name
        merged_payload["last_webhook_event"] = payload

        status = self._map_status(event_name)
        transcript_status = self._map_transcript_status(event_name)
        transcript_ready_at: datetime | None = None

        if event_name in TRANSCRIPT_ARTIFACT_READY_EVENTS:
            # Only transcript completion should trigger the provider follow-up call.
            # Recording lifecycle events update status only; transcript availability
            # remains owned by Recall's transcript artifact lifecycle.
            bot_snapshot, transcript_artifact = await self._fetch_transcript_artifact(bot_id)
            if bot_snapshot is not None:
                merged_payload["bot_snapshot"] = bot_snapshot
            if transcript_artifact is not None:
                merged_payload["transcript_artifact"] = transcript_artifact
                transcript_status = MeetingTranscriptStatus.READY
                transcript_ready_at = updated_at or datetime.now(UTC)

        record = await self.session_service.apply_webhook_update(
            bot_id,
            provider_status=provider_status,
            provider_payload=merged_payload,
            status=status,
            transcript_status=transcript_status,
            status_message=status_message,
            last_webhook_at=updated_at,
            transcript_ready_at=transcript_ready_at,
        )
        if not record:
            return {
                "status": "skipped",
                "reason": "session_not_found",
                "event": event_name,
                "provider_session_id": bot_id,
            }

        result = {
            "status": "processed",
            "event": event_name,
            "provider_session_id": bot_id,
            "session_id": str(record.session_id),
            "organisation_id": str(self.organisation_id),
            "normalized_status": _enum_value(record.status),
            "transcript_status": _enum_value(record.transcript_status),
        }
        if event_name in TRANSCRIPT_ARTIFACT_READY_EVENTS:
            result.update(
                await self._materialize_recall_transcript_and_enqueue_summaries(
                    record.calendar_event_id
                )
            )
        return result

    async def _materialize_recall_transcript_and_enqueue_summaries(
        self,
        event_id: UUID,
    ) -> dict[str, Any]:
        """Download the Recall transcript and enqueue summary jobs for one event.

        Called only after Recall reports `transcript.done`. The lock is event-scoped
        so duplicate webhook deliveries do not download the same transcript or enqueue
        duplicate summary work at the same time.
        """
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
        """Create or reuse summary jobs after a Recall transcript becomes durable.

        Deal-linked meetings create one job per active deal association and later
        trigger deal insights. Meetings with no deal association create exactly one
        event-level job with `deal_id=None`, which stores summary data on Meeting and
        does not run deal pipelines.
        """
        job_ids_to_enqueue: list[UUID] = []
        created = 0
        skipped = 0

        async for session in acquire_org_async_session(self.organisation_id):
            # Deal associations are scoped by org and event. The event id comes from
            # provider webhook metadata, so organisation_id remains the tenant boundary.
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
                        # Pending jobs may have been committed but not enqueued if
                        # the previous webhook task crashed after commit.
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
                        "event_id": str(event_id),
                        "association_id": str(association.association_id),
                    },
                )
                session.add(job)
                await session.flush()
                job_ids_to_enqueue.append(job.job_id)
                created += 1

            if not associations:
                # No deal association means this is still a valid meeting summary,
                # but it must not create deal-memory/risk/stakeholder work.
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
                        # Same recovery rule as deal jobs: re-enqueue a pending
                        # durable row rather than creating another one.
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

    async def _fetch_transcript_artifact(
        self,
        provider_session_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """Fetch the latest bot payload only when transcript artifacts should exist.

        The extra provider call is intentionally gated to transcript-ready events to
        avoid needless API usage on every webhook delivery.
        """
        api_key = await recall_config.get_api_key()
        if not api_key:
            return None, None

        provider = RecallMeetingRecordingProvider(
            api_key=api_key,
            region=recall_config.RECALL_REGION,
            timeout=recall_config.RECALL_TIMEOUT_SECONDS,
        )
        bot_snapshot = await provider.fetch_session(provider_session_id)
        transcript_artifact = provider.extract_transcript_download(bot_snapshot)
        return bot_snapshot, transcript_artifact

    @staticmethod
    def _map_status(event_name: str) -> MeetingRecordingStatus | None:
        """Map provider event names to our internal lifecycle state."""
        # Keep unknown events unopinionated so a webhook rename does not block other
        # state updates (status stays preserved unless we have a known mapping).
        if event_name in {"bot.joining_call", "bot.in_waiting_room"}:
            return MeetingRecordingStatus.JOINING
        if event_name in {
            "bot.in_call_not_recording",
            "bot.recording_permission_allowed",
            "bot.recording_permission_denied",
            "bot.in_call_recording",
        }:
            return MeetingRecordingStatus.IN_CALL
        if event_name in {"bot.call_ended", "bot.done", "recording.done"}:
            return MeetingRecordingStatus.COMPLETED
        if event_name in {"bot.fatal", "recording.failed"}:
            return MeetingRecordingStatus.FAILED
        if event_name == "recording.deleted":
            return MeetingRecordingStatus.CANCELLED
        return None

    @staticmethod
    def _map_transcript_status(event_name: str) -> MeetingTranscriptStatus | None:
        """Map transcript lifecycle events from Recall to local status values."""
        if event_name == "transcript.processing":
            return MeetingTranscriptStatus.PENDING
        if event_name == "transcript.done":
            return MeetingTranscriptStatus.READY
        if event_name == "transcript.failed":
            return MeetingTranscriptStatus.FAILED
        if event_name == "transcript.deleted":
            return MeetingTranscriptStatus.NOT_AVAILABLE
        if event_name == "recording.failed":
            return MeetingTranscriptStatus.FAILED
        if event_name == "recording.deleted":
            return MeetingTranscriptStatus.NOT_AVAILABLE
        return None

    @staticmethod
    def _build_status_message(provider_data: dict[str, Any]) -> str | None:
        """Prefer explicit sub-code + message for operator-level diagnostics."""
        message = provider_data.get("message")
        sub_code = provider_data.get("sub_code")
        if message and sub_code:
            return f"{sub_code}: {message}"
        if message:
            return str(message)
        if sub_code:
            return str(sub_code)
        return None

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        """Parse Recall ISO-8601 timestamps and normalize missing timezone to UTC."""
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
