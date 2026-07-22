"""Stale job cleanup and durable queue recovery for the chat worker.

Eight passes run with fresh organization sessions:

1. WEB CHAT PENDING - republish unclaimed jobs older than two minutes. The web
   worker claims jobs under a row lock, so duplicate broker deliveries are safe.

2. WHATSAPP PENDING - lease and republish jobs older than two minutes.

3. WHATSAPP DELIVERY - resume stored responses after two minutes; fail unknown
   agent executions after twelve minutes without rerunning their tools.

4. WHATSAPP ORDERING - release the oldest waiting message when no predecessor
   remains active. This repairs a crash between terminal commit and queue advance.

5. ATTACHMENTS - republish post-upload work left in PROCESSING after the
   producer or worker lost the original task.

6. OTHER CHAT JOBS - fail channels without a durable recovery contract when
   PENDING for 60 minutes or IN_PROGRESS for 30 minutes.

7. STUCK TRIGGERED REMINDERS - reset recoverable reminders to ACTIVE or fail
   them after the retry ceiling. This covers crashes after the TRIGGERED commit.

8. STALE REMINDER RUNS - mark runs stuck IN_PROGRESS for 30 minutes as FAILED.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlmodel import col

from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
)
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
from src.infra.observability.metrics import (
    record_whatsapp_enqueue,
    record_whatsapp_job_age,
    record_whatsapp_recovery_sweep,
)
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.bots.whatsapp_bot_service import enqueue_whatsapp_job

logger = get_logger("chat_stale_job_cleanup_service")

# Thresholds
CHAT_JOB_PENDING_THRESHOLD_MIN = 60
CHAT_JOB_IN_PROGRESS_THRESHOLD_MIN = 30
REMINDER_TRIGGERED_THRESHOLD_MIN = 30
REMINDER_RUN_THRESHOLD_MIN = 30
# Atomic worker claims make duplicate delivery harmless, so recovery can start
# after one 15-second beat interval instead of delaying a failed publish for minutes.
DURABLE_CHAT_PENDING_REQUEUE_AFTER_SECONDS = 15
WEB_CHAT_MAX_ENQUEUE_ATTEMPTS = 5
WEB_CHAT_RECOVERY_BATCH_SIZE = 500
_CHAT_QUEUE_PUBLISH_TIMEOUT_SECONDS = 4
WHATSAPP_IN_PROGRESS_STALE_AFTER_MINUTES = 12
WHATSAPP_ENQUEUE_LEASE_MINUTES = 2
WHATSAPP_SEND_LEASE_MINUTES = 2
WHATSAPP_MAX_ENQUEUE_ATTEMPTS = 5
WHATSAPP_MAX_SEND_ATTEMPTS = 3
WHATSAPP_RECOVERY_BATCH_SIZE = 500
_WHATSAPP_QUEUE_PUBLISH_TIMEOUT_SECONDS = 4
_PERSISTED_ERROR_LIMIT = 1000
# Post-upload tasks have a 10-minute hard limit. Waiting two extra minutes keeps
# legitimate long processing from being duplicated while still repairing lost work.
ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES = 12
ATTACHMENT_RECOVERY_BATCH_SIZE = 100
_ATTACHMENT_QUEUE_PUBLISH_TIMEOUT_SECONDS = 4
_WHATSAPP_STATIC_FAILURE_MESSAGE = (
    "I'm sorry, something went wrong while processing your request. "
    "Please try again or use the CopilotGTM web app."
)

# Max consecutive failures before permanently failing a reminder
MAX_CONSECUTIVE_FAILURES = 3


async def enqueue_web_chat_job(organisation_id: UUID, job_id: UUID) -> str:
    """Publish one persisted web-chat job with a bounded broker wait."""
    from src.workers.chat_worker.client import send_chat_task_async

    return await asyncio.wait_for(
        send_chat_task_async(
            "chat.process_message",
            args=[str(organisation_id), str(job_id)],
            queue="chat_messages",
            routing_key="chat_messages",
        ),
        timeout=_CHAT_QUEUE_PUBLISH_TIMEOUT_SECONDS,
    )


async def enqueue_whatsapp_delivery_resume(organisation_id: UUID, job_id: UUID) -> str:
    """Publish a send-only task from durable response state."""
    from src.workers.chat_worker.client import send_chat_task_async

    return await asyncio.wait_for(
        send_chat_task_async(
            "chat.resume_whatsapp_delivery",
            args=[str(organisation_id), str(job_id)],
            queue="chat_messages",
            routing_key="chat_messages",
        ),
        timeout=_WHATSAPP_QUEUE_PUBLISH_TIMEOUT_SECONDS,
    )


async def enqueue_attachment_post_upload(
    organisation_id: UUID,
    attachment_id: UUID,
) -> str:
    """Republish one attachment through the existing post-upload queue."""
    from src.workers.chat_worker.client import send_chat_task_async

    return await asyncio.wait_for(
        send_chat_task_async(
            "attachments.post_upload",
            args=[str(organisation_id), str(attachment_id)],
            queue="attachments",
            routing_key="attachments",
        ),
        timeout=_ATTACHMENT_QUEUE_PUBLISH_TIMEOUT_SECONDS,
    )


class ChatStaleJobCleanupService:
    """Cleans up stuck chat jobs and reminders for one organisation.

    Called by the per-org Tier 2 Celery task (cleanup_stale_chat_jobs_org_task).
    Each pass uses a fresh DB session so connections aren't held across passes.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    def cleanup_stale_jobs(self) -> dict:
        """Run all recovery passes and emit one scheduler-health outcome."""
        try:
            result = run_async(self._run())
        except Exception:
            record_whatsapp_recovery_sweep("failed")
            raise
        record_whatsapp_recovery_sweep("completed")
        return result

    async def _run(self) -> dict:
        now = datetime.now(UTC)
        counts: dict[str, int] = {
            "web_chat_requeued": 0,
            "web_chat_requeue_failed": 0,
            "web_chat_enqueue_exhausted": 0,
            "whatsapp_requeued": 0,
            "whatsapp_requeue_failed": 0,
            "whatsapp_enqueue_exhausted": 0,
            "whatsapp_send_resumed": 0,
            "whatsapp_send_resume_failed": 0,
            "whatsapp_agent_failed": 0,
            "whatsapp_waiting_released": 0,
            "attachments_requeued": 0,
            "attachment_requeue_failed": 0,
            "stale_chat_jobs": 0,
            "stuck_triggered_reminders": 0,
            "stale_reminder_runs": 0,
        }

        web_counts = await self._recover_stale_web_chat_pending_jobs(now)
        counts["web_chat_requeued"] = web_counts["requeued"]
        counts["web_chat_requeue_failed"] = web_counts["requeue_failed"]
        counts["web_chat_enqueue_exhausted"] = web_counts["enqueue_exhausted"]

        whatsapp_counts = await self._recover_stale_whatsapp_pending_jobs(now)
        counts["whatsapp_requeued"] = whatsapp_counts["requeued"]
        counts["whatsapp_requeue_failed"] = whatsapp_counts["requeue_failed"]
        counts["whatsapp_enqueue_exhausted"] = whatsapp_counts["enqueue_exhausted"]

        delivery_counts = await self._recover_stale_whatsapp_deliveries(now)
        counts["whatsapp_send_resumed"] = delivery_counts["send_resumed"]
        counts["whatsapp_send_resume_failed"] = delivery_counts["send_resume_failed"]
        counts["whatsapp_agent_failed"] = delivery_counts["agent_failed"]

        counts["whatsapp_waiting_released"] = await self._recover_waiting_whatsapp_session_jobs()

        attachment_counts = await self._recover_stale_attachments(now)
        counts["attachments_requeued"] = attachment_counts["requeued"]
        counts["attachment_requeue_failed"] = attachment_counts["requeue_failed"]

        # WhatsApp rows have stage-aware recovery and must never enter the generic
        # bulk failure pass used by non-durable bot channels.
        counts["stale_chat_jobs"] = await self._cleanup_stale_chat_jobs(now)

        # ── Pass 2: Stuck TRIGGERED reminders ─────────────────────────
        counts["stuck_triggered_reminders"] = await self._cleanup_stuck_reminders(now)

        # ── Pass 3: Stale UserReminderRuns ────────────────────────────
        counts["stale_reminder_runs"] = await self._cleanup_stale_reminder_runs(now)

        logger.info(
            "Cleanup done for org=%s: attachments_requeued=%d, "
            "attachment_requeue_failed=%d, stale_chat_jobs=%d, "
            "stuck_reminders=%d, stale_runs=%d",
            self.organisation_id,
            counts["attachments_requeued"],
            counts["attachment_requeue_failed"],
            counts["stale_chat_jobs"],
            counts["stuck_triggered_reminders"],
            counts["stale_reminder_runs"],
        )

        return {"organisation_id": str(self.organisation_id), **counts}

    async def _recover_stale_attachments(self, now: datetime) -> dict[str, int]:
        """Lease and republish attachments whose post-upload work disappeared."""
        attachment_ids = await self._lease_stale_attachments(now)
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

    async def _lease_stale_attachments(self, now: datetime) -> list[UUID]:
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
                .limit(ATTACHMENT_RECOVERY_BATCH_SIZE)
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

    async def _recover_waiting_whatsapp_session_jobs(self) -> int:
        """Release session heads left waiting after a terminal worker crash."""
        from src.domain.models.organisation_tables.chat_message_models import ChatMessage
        from src.service.bots.whatsapp_bot_service import WhatsAppBotService

        enqueue_status = ChatMessageJob.job_metadata["enqueue_status"].as_string()
        session_ids: list[UUID] = []
        async for session in acquire_org_async_session(self.organisation_id):
            statement = (
                select(col(ChatMessage.session_id))
                .join(
                    ChatMessageJob,
                    col(ChatMessageJob.message_id) == col(ChatMessage.message_id),
                )
                .where(
                    col(ChatMessageJob.chat_type) == "WHATSAPP_CHAT",
                    col(ChatMessageJob.status) == JobStatus.PENDING,
                    enqueue_status == "waiting_for_predecessor",
                )
                .distinct()
                .limit(WHATSAPP_RECOVERY_BATCH_SIZE)
            )
            session_ids = list((await session.execute(statement)).scalars().all())
            break

        released = 0
        whatsapp = WhatsAppBotService()
        for session_id in session_ids:
            # enqueue_next_session_job rechecks the full ordered queue under the
            # ChatSession lock, so this discovery scan cannot skip an active head.
            if await whatsapp.enqueue_next_session_job(self.organisation_id, session_id):
                released += 1
        return released

    async def _recover_stale_web_chat_pending_jobs(self, now: datetime) -> dict[str, int]:
        leased, exhausted = await self._lease_stale_web_chat_pending_jobs(now)
        counts = {
            "requeued": 0,
            "requeue_failed": 0,
            "enqueue_exhausted": exhausted,
        }

        for job_id, attempt in leased:
            try:
                task_id = await enqueue_web_chat_job(self.organisation_id, job_id)
            except Exception as exc:
                counts["requeue_failed"] += 1
                await self._record_web_chat_requeue_result(
                    job_id,
                    attempt=attempt,
                    task_id=None,
                    error=str(exc)[:_PERSISTED_ERROR_LIMIT],
                )
                logger.error("Failed to republish web chat job=%s", job_id, exc_info=True)
                continue

            await self._record_web_chat_requeue_result(
                job_id,
                attempt=attempt,
                task_id=task_id,
                error=None,
            )
            counts["requeued"] += 1

        return counts

    async def _lease_stale_web_chat_pending_jobs(
        self, now: datetime
    ) -> tuple[list[tuple[UUID, int]], int]:
        """Lease unclaimed web jobs before publishing outside the transaction."""
        cutoff = now - timedelta(seconds=DURABLE_CHAT_PENDING_REQUEUE_AFTER_SECONDS)
        enqueue_status = ChatMessageJob.job_metadata["enqueue_status"].as_string()
        recoverable_statuses = ("pending", "failed", "enqueuing", "requeueing", "enqueued")
        leased: list[tuple[UUID, int]] = []
        exhausted = 0

        async for session in acquire_org_async_session(self.organisation_id):
            statement = (
                select(ChatMessageJob)
                .where(
                    col(ChatMessageJob.chat_type) == "GENERAL_CHAT",
                    col(ChatMessageJob.status) == JobStatus.PENDING,
                    col(ChatMessageJob.updated_at) < cutoff,
                    or_(
                        enqueue_status.is_(None),
                        enqueue_status.in_(recoverable_statuses),
                    ),
                )
                .order_by(col(ChatMessageJob.updated_at))
                .limit(WEB_CHAT_RECOVERY_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            jobs = (await session.execute(statement)).scalars().all()
            if not jobs:
                return leased, exhausted

            for job in jobs:
                metadata = dict(job.job_metadata or {})
                if metadata.get("enqueue_status") not in {None, *recoverable_statuses}:
                    continue
                attempts = int(metadata.get("enqueue_attempts") or 0)
                if attempts >= WEB_CHAT_MAX_ENQUEUE_ATTEMPTS:
                    job.status = JobStatus.FAILED
                    job.error_message = "Web chat enqueue retry budget exhausted"
                    metadata["enqueue_status"] = "exhausted"
                    job.job_metadata = metadata
                    job.updated_at = now
                    session.add(job)
                    exhausted += 1
                    continue

                attempt = attempts + 1
                metadata.update(
                    {
                        "enqueue_status": "requeueing",
                        "enqueue_attempts": attempt,
                        "celery_task_id": None,
                        "last_enqueue_error": None,
                    }
                )
                job.job_metadata = metadata
                job.updated_at = now
                session.add(job)
                leased.append((job.job_id, attempt))

            await session.commit()
            return leased, exhausted

        return leased, exhausted

    async def _record_web_chat_requeue_result(
        self,
        job_id: UUID,
        *,
        attempt: int,
        task_id: str | None,
        error: str | None,
    ) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            job = await session.get(ChatMessageJob, job_id)
            if not job:
                return
            metadata = dict(job.job_metadata or {})
            if int(metadata.get("enqueue_attempts") or 0) != attempt:
                return
            metadata.update(
                {
                    "enqueue_status": "enqueued" if task_id else "failed",
                    "celery_task_id": task_id,
                    "last_enqueue_error": error,
                }
            )
            job.job_metadata = metadata
            session.add(job)
            await session.commit()
            return

    async def _recover_stale_whatsapp_pending_jobs(self, now: datetime) -> dict[str, int]:
        leased, exhausted = await self._lease_stale_whatsapp_pending_jobs(now)
        counts = {
            "requeued": 0,
            "requeue_failed": 0,
            "enqueue_exhausted": exhausted,
        }

        for job_id, envelope, attempt in leased:
            try:
                task_id = await enqueue_whatsapp_job(job_id=job_id, envelope=envelope)
            except Exception as exc:
                counts["requeue_failed"] += 1
                record_whatsapp_enqueue("failed")
                await self._record_whatsapp_requeue_result(
                    job_id,
                    attempt=attempt,
                    task_id=None,
                    error=str(exc)[:_PERSISTED_ERROR_LIMIT],
                )
                logger.error("Failed to republish WhatsApp job=%s", job_id, exc_info=True)
                continue

            await self._record_whatsapp_requeue_result(
                job_id,
                attempt=attempt,
                task_id=task_id,
                error=None,
            )
            counts["requeued"] += 1
            record_whatsapp_enqueue("requeued")

        return counts

    async def _lease_stale_whatsapp_pending_jobs(
        self, now: datetime
    ) -> tuple[list[tuple[UUID, dict, int]], int]:
        """Lock, mark, and commit pending recovery leases before broker I/O."""
        cutoff = now - timedelta(seconds=DURABLE_CHAT_PENDING_REQUEUE_AFTER_SECONDS)
        enqueue_status = ChatMessageJob.job_metadata["enqueue_status"].as_string()
        leased: list[tuple[UUID, dict, int]] = []
        exhausted = 0

        async for session in acquire_org_async_session(self.organisation_id):
            statement = (
                select(ChatMessageJob)
                .where(
                    col(ChatMessageJob.chat_type) == "WHATSAPP_CHAT",
                    col(ChatMessageJob.status) == JobStatus.PENDING,
                    col(ChatMessageJob.updated_at) < cutoff,
                    or_(
                        enqueue_status.is_(None),
                        enqueue_status.in_(("pending", "failed", "enqueuing", "requeueing")),
                    ),
                )
                .order_by(col(ChatMessageJob.updated_at))
                .limit(WHATSAPP_RECOVERY_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            jobs = (await session.execute(statement)).scalars().all()
            if not jobs:
                return leased, exhausted

            for job in jobs:
                record_whatsapp_job_age(
                    "pending",
                    (now - job.updated_at).total_seconds(),
                )
                metadata = dict(job.job_metadata or {})
                if metadata.get("enqueue_status") not in {
                    None,
                    "pending",
                    "failed",
                    "enqueuing",
                    "requeueing",
                }:
                    continue
                attempts = int(metadata.get("enqueue_attempts") or 0)
                envelope = metadata.get("dispatch_envelope")

                if attempts >= WHATSAPP_MAX_ENQUEUE_ATTEMPTS:
                    job.status = JobStatus.FAILED
                    job.error_message = "WhatsApp enqueue retry budget exhausted"
                    metadata["enqueue_status"] = "exhausted"
                    job.job_metadata = metadata
                    job.updated_at = now
                    session.add(job)
                    exhausted += 1
                    record_whatsapp_enqueue("exhausted")
                    continue

                if not isinstance(envelope, dict):
                    job.status = JobStatus.FAILED
                    job.error_message = "WhatsApp dispatch envelope is missing"
                    metadata["enqueue_status"] = "invalid"
                    job.job_metadata = metadata
                    job.updated_at = now
                    session.add(job)
                    exhausted += 1
                    continue

                attempt = attempts + 1
                metadata.update(
                    {
                        "enqueue_status": "requeueing",
                        "enqueue_attempts": attempt,
                        "last_enqueue_error": None,
                    }
                )
                job.job_metadata = metadata
                job.updated_at = now
                session.add(job)
                leased.append((job.job_id, dict(envelope), attempt))

            await session.commit()
            return leased, exhausted

        return leased, exhausted

    async def _record_whatsapp_requeue_result(
        self,
        job_id: UUID,
        *,
        attempt: int,
        task_id: str | None,
        error: str | None,
    ) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            job = await session.get(ChatMessageJob, job_id)
            if not job:
                return
            metadata = dict(job.job_metadata or {})
            if int(metadata.get("enqueue_attempts") or 0) != attempt:
                return
            metadata.update(
                {
                    "enqueue_status": "enqueued" if task_id else "failed",
                    "celery_task_id": task_id,
                    "last_enqueue_error": error,
                }
            )
            job.job_metadata = metadata
            session.add(job)
            await session.commit()
            return

    async def _recover_stale_whatsapp_deliveries(self, now: datetime) -> dict[str, int]:
        job_ids, agent_failed = await self._lease_stale_whatsapp_deliveries(now)
        counts = {
            "send_resumed": 0,
            "send_resume_failed": 0,
            "agent_failed": agent_failed,
        }
        for job_id in job_ids:
            try:
                task_id = await enqueue_whatsapp_delivery_resume(
                    self.organisation_id,
                    job_id,
                )
            except Exception as exc:
                counts["send_resume_failed"] += 1
                await self._record_whatsapp_send_recovery_result(
                    job_id,
                    task_id=None,
                    error=str(exc)[:_PERSISTED_ERROR_LIMIT],
                )
                logger.error(
                    "Failed to enqueue WhatsApp send recovery job=%s", job_id, exc_info=True
                )
                continue

            await self._record_whatsapp_send_recovery_result(
                job_id,
                task_id=task_id,
                error=None,
            )
            counts["send_resumed"] += 1
        return counts

    async def _lease_stale_whatsapp_deliveries(self, now: datetime) -> tuple[list[UUID], int]:
        """Lease send-only recovery or fail an unknown stale agent execution."""
        send_cutoff = now - timedelta(minutes=WHATSAPP_SEND_LEASE_MINUTES)
        agent_cutoff = now - timedelta(minutes=WHATSAPP_IN_PROGRESS_STALE_AFTER_MINUTES)
        processing_stage = ChatMessageJob.job_metadata["processing_stage"].as_string()
        job_ids: list[UUID] = []
        agent_failed = 0

        async for session in acquire_org_async_session(self.organisation_id):
            statement = (
                select(ChatMessageJob)
                .where(
                    col(ChatMessageJob.chat_type) == "WHATSAPP_CHAT",
                    col(ChatMessageJob.status).in_([JobStatus.IN_PROGRESS, JobStatus.FAILED]),
                    col(ChatMessageJob.updated_at) < send_cutoff,
                    or_(
                        processing_stage.in_(("response_ready", "sending")),
                        and_(
                            col(ChatMessageJob.status) == JobStatus.IN_PROGRESS,
                            or_(
                                processing_stage.is_(None),
                                processing_stage == "agent_running",
                            ),
                        ),
                    ),
                )
                .order_by(col(ChatMessageJob.updated_at))
                .limit(WHATSAPP_RECOVERY_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            jobs = (await session.execute(statement)).scalars().all()
            if not jobs:
                return job_ids, agent_failed

            for job in jobs:
                status_value = getattr(job.status, "value", job.status)
                if status_value == JobStatus.IN_PROGRESS.value:
                    record_whatsapp_job_age(
                        "in_progress",
                        (now - job.updated_at).total_seconds(),
                    )
                metadata = dict(job.job_metadata or {})
                stage = str(metadata.get("processing_stage") or "agent_running")

                if stage in {"response_ready", "sending"}:
                    next_attempt_at = self._parse_datetime(metadata.get("next_send_attempt_at"))
                    if next_attempt_at and next_attempt_at > now:
                        continue
                    if int(metadata.get("send_attempts") or 0) >= WHATSAPP_MAX_SEND_ATTEMPTS:
                        metadata.update(
                            {
                                "send_status": "exhausted",
                                "processing_stage": "failed",
                                "failure_stage": "sending",
                            }
                        )
                        job.job_metadata = metadata
                        job.status = JobStatus.FAILED
                        job.error_message = "WhatsApp send retry budget exhausted"
                        job.updated_at = now
                        session.add(job)
                        continue
                elif status_value == JobStatus.IN_PROGRESS.value and job.updated_at < agent_cutoff:
                    metadata.update(
                        {
                            "agent_success": False,
                            "agent_error": "Worker stopped before producing a recoverable response",
                            "failure_stage": "agent_running",
                            "processing_stage": "response_ready",
                            "send_status": "pending",
                            "send_attempts": 0,
                            "next_send_attempt_at": None,
                            "outbound_chunks": [
                                {
                                    "index": 0,
                                    "text": _WHATSAPP_STATIC_FAILURE_MESSAGE,
                                    "provider_message_id": None,
                                    "accepted_at": None,
                                }
                            ],
                        }
                    )
                    job.status = JobStatus.FAILED
                    job.error_message = metadata["agent_error"]
                    agent_failed += 1
                else:
                    continue

                metadata.update(
                    {
                        "send_recovery_status": "requeueing",
                        "send_recovery_attempts": int(metadata.get("send_recovery_attempts") or 0)
                        + 1,
                        "last_send_recovery_error": None,
                    }
                )
                job.job_metadata = metadata
                job.updated_at = now
                session.add(job)
                job_ids.append(job.job_id)

            await session.commit()
            return job_ids, agent_failed

        return job_ids, agent_failed

    async def _record_whatsapp_send_recovery_result(
        self,
        job_id: UUID,
        *,
        task_id: str | None,
        error: str | None,
    ) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            job = await session.get(ChatMessageJob, job_id)
            if not job:
                return
            metadata = dict(job.job_metadata or {})
            metadata.update(
                {
                    "send_recovery_status": "enqueued" if task_id else "failed",
                    "send_recovery_task_id": task_id,
                    "last_send_recovery_error": error,
                }
            )
            job.job_metadata = metadata
            session.add(job)
            await session.commit()
            return

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

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
                                col(ChatMessageJob.chat_type).notin_(
                                    ("WHATSAPP_CHAT", "GENERAL_CHAT")
                                ),
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
