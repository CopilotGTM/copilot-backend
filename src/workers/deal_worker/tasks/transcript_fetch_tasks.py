"""Scheduled Transcript Fetch and Meeting Summary Pipeline Tasks.

This module provides Celery tasks for:
1. Fetching transcripts for deal-associated meetings
2. Automatically generating meeting summaries (sequential, oldest first)
3. Triggering deal summaries after all meetings are processed

Business logic is delegated to TranscriptFetchTaskService which internally
uses DealMeetingSummaryPipelineService for the summary generation.

Architecture (Deal-Centric):
    Tier 1: trigger_transcript_fetch_all_orgs_task (Beat: every 30 min at :15 and :45)
        → Tier 2: fetch_meeting_transcripts_org_task (per org)
            → TranscriptFetchTaskService.process_all()
                - Get all deals needing transcript fetch
                - For each deal (sequential):
                    1. Fetch transcripts for that deal's meetings (parallel)
                    2. Immediately trigger pipeline:
                        - Generate meeting summaries (sequential, oldest first)
                        - Run risk + stakeholder in parallel, then generate deal memory
                    3. Move to next deal

    This is simpler than fetching all transcripts first, then triggering pipelines.
    Each deal completes fully (transcripts → summaries) before moving to the next.

Retry Logic:
    - When API returns empty transcript list, we can't tell if:
      a) Meeting wasn't recorded (permanent)
      b) Transcript still processing (temporary)
      c) User lacks permission (try another user)
    - So we retry up to MAX_ATTEMPTS times
    - After max attempts with no transcript → mark NOT_AVAILABLE
    - This gives ~1 hour window (2 retries * 30 min) for transcript processing
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.transcript_fetch_task_service import (
    TranscriptFetchTaskService,
)

if TYPE_CHECKING:
    from celery import Task

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="deal.fetch_meeting_transcripts_org",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=3000,
    time_limit=3300,
)
def fetch_meeting_transcripts_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Fetch transcripts for deal-associated meetings in an organisation.

    Delegates to TranscriptFetchTaskService which handles:
    - Single query to get all meetings needing fetch
    - Processing with integration priority (owner first)
    - Tracking retry attempts and status transitions
    """
    started_at = datetime.now(UTC).isoformat()

    # Acquire distributed lock
    lock_key = f"task_lock:fetch_meeting_transcripts_org:{organisation_id}"
    if not acquire_task_lock(lock_key, ttl=660):
        logger.info(
            "Transcript fetch job already running for organisation",
            extra={
                "event": "transcript_fetch_org_already_running",
                "organisation_id": organisation_id,
                "failure_reason": "already_running",
            },
        )
        return {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    logger.info(
        "Starting transcript fetch for organisation",
        extra={"event": "transcript_fetch_org_started", "organisation_id": organisation_id},
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        # Deal-centric processing:
        # For each deal: fetch transcripts -> trigger pipeline (meeting summaries -> deal memory)
        # Pipeline is triggered immediately after each deal's transcripts are fetched
        service = TranscriptFetchTaskService(org_uuid)
        result = await service.process_all()

        return {
            "status": "success",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "deals_processed": result.deals_processed,
            "meetings_found": result.meetings_found,
            "transcripts_fetched": result.transcripts_fetched,
            "transcripts_pending": result.transcripts_pending,
            "transcripts_not_available": result.transcripts_not_available,
            "transcripts_failed": result.transcripts_failed,
            "pipelines_triggered": result.pipelines_triggered,
            "deal_memories_triggered": result.deal_memories_triggered,
            "fetching_reset": result.fetching_reset,
            "prep_jobs_created": result.prep_jobs_created,
            "prep_jobs_skipped": result.prep_jobs_skipped,
            "prep_waiting_for_summaries": result.prep_waiting_for_summaries,
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Transcript fetch failed for organisation",
            extra={
                "event": "transcript_fetch_org_failed",
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
    name="deal.trigger_transcript_fetch_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def trigger_transcript_fetch_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Trigger transcript fetch for all approved organizations.

    Scheduled to run every 30 minutes via Celery Beat.
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        logger.info(
            "Starting transcript fetch dispatch for all organisations",
            extra={"event": "transcript_fetch_all_orgs_started"},
        )

        # Query approved organizations
        org_ids: list[UUID] = []

        # Acquire master session to get all approved org IDs
        async for master_session in acquire_master_async_session():
            # Get all approved org IDs
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]  # list of organisation_id UUIDs

        if not org_ids:
            logger.warning(
                "No approved organisations found for transcript fetch",
                extra={
                    "event": "transcript_fetch_all_orgs_no_organisations",
                    "status": "no_organisations",
                },
            )
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {"total_orgs": 0, "orgs_triggered": 0, "orgs_skipped": 0},
                "org_tasks": [],
            }

        logger.info(
            "Found approved organisations for transcript fetch dispatch",
            extra={
                "event": "transcript_fetch_all_orgs_found",
                "organisation_count": len(org_ids),
            },
        )

        # Dispatch Tier 2 tasks
        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                # Trigger Tier 2 task for this org
                # Preserve beat/coordinator lineage into the heavy org-level transcript pipeline.
                task = fetch_meeting_transcripts_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    headers=correlation_headers(),
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "task_id": task.id,
                        "status": "triggered",
                    }
                )
                orgs_triggered += 1
                logger.debug(
                    "Dispatched transcript fetch for org: %s (task_id=%s)", org_id, task.id
                )
            except Exception as e:
                logger.error(
                    "Failed to dispatch transcript fetch task for organisation",
                    extra={
                        "event": "transcript_fetch_org_dispatch_failed",
                        "organisation_id": str(org_id),
                        "failure_reason": str(e),
                    },
                    exc_info=True,
                )
                org_tasks.append(
                    {
                        "org_id": str(org_id),
                        "status": "failed",
                        "error": str(e),
                    }
                )
                orgs_skipped += 1

        logger.info(
            "Transcript fetch dispatch completed",
            extra={
                "event": "transcript_fetch_all_orgs_dispatch_completed",
                "triggered_orgs": orgs_triggered,
                "skipped_orgs": orgs_skipped,
                "total_orgs": len(org_ids),
            },
        )

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
    except Exception as e:
        logger.error(
            "Transcript fetch all-orgs dispatch failed",
            extra={
                "event": "transcript_fetch_all_orgs_failed",
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
