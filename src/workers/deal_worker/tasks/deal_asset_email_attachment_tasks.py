"""Three-tier Celery tasks for extracting email attachments into deal assets.

Tier 1: extract_email_attachments_all_orgs_task (coordinator)
                    |
                    +-> Dispatches Tier 2 for each approved org

Tier 2: extract_email_attachments_org_task (org-level)
                    |
                    +-> Processes all deals in org

Tier 3: extract_email_attachments_deal_task (per-deal, on-demand)
                    |
                    +-> Processes attachments for a single deal
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.crm.assets.email_attachment_service import (
    DealAssetEmailAttachmentService,
)
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.client import send_deal_task, send_deal_task_async

logger = get_logger(__name__)

MAX_SCOPED_EMAIL_IDS = 100
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class _RetryableAttachmentError(RuntimeError):
    """Carry the canonical email IDs that should be replayed by Celery."""

    def __init__(self, message: str, email_ids: list[str]):
        super().__init__(message)
        self.email_ids = email_ids


async def dispatch_email_attachment_extraction_async(
    *,
    organisation_id: UUID,
    deal_id: UUID,
    email_ids: list[UUID],
    trigger: str,
) -> UUID:
    """Persist one internal attachment job before publishing its bounded Celery task."""
    if not email_ids:
        raise ValueError("email_ids must be non-empty")
    if len(email_ids) > MAX_SCOPED_EMAIL_IDS:
        raise ValueError(f"email_ids cannot exceed {MAX_SCOPED_EMAIL_IDS}")

    job = DealJobs(
        organisation_id=organisation_id,
        user_id=AUTOMATED_PIPELINE_USER_ID,
        job_type=DealJobType.EMAIL_ATTACHMENT_EXTRACTION,
        deal_id=deal_id,
        status=JobStatus.PENDING,
        job_metadata={
            "email_ids": [str(email_id) for email_id in email_ids],
            "trigger": trigger,
            "enqueue_status": "pending",
        },
    )
    async for session in acquire_org_async_session(organisation_id):
        session.add(job)
        await session.commit()
        break

    try:
        celery_task_id = await send_deal_task_async(
            "deal_asset.extract_email_attachments_deal",
            kwargs={
                "organisation_id": str(organisation_id),
                "deal_id": str(deal_id),
                "email_ids": [str(email_id) for email_id in email_ids],
                "job_id": str(job.job_id),
            },
            queue="deal",
        )
    except Exception:
        await _update_attachment_job(
            organisation_id,
            deal_id,
            job.job_id,
            status=JobStatus.FAILED,
            result_data=None,
            error_message="Failed to enqueue email attachment extraction",
        )
        raise

    async for session in acquire_org_async_session(organisation_id):
        stored = await session.get(DealJobs, job.job_id)
        if stored is not None:
            metadata = dict(stored.job_metadata or {})
            metadata.update(
                {
                    "enqueue_status": "enqueued",
                    "celery_task_id": celery_task_id,
                }
            )
            stored.job_metadata = metadata
            await session.commit()
        break
    return job.job_id


async def _update_attachment_job(
    organisation_id: UUID,
    deal_id: UUID,
    job_id: UUID,
    *,
    status: JobStatus,
    result_data: dict[str, Any] | None,
    error_message: str | None,
    retry_count: int | None = None,
) -> None:
    """Update only the attachment job owned by this organisation and deal."""
    async for session in acquire_org_async_session(organisation_id):
        result = await session.execute(
            select(DealJobs).where(
                col(DealJobs.job_id) == job_id,
                col(DealJobs.organisation_id) == organisation_id,
                col(DealJobs.deal_id) == deal_id,
                col(DealJobs.job_type) == DealJobType.EMAIL_ATTACHMENT_EXTRACTION,
            )
        )
        job = result.scalar_one_or_none()
        if job is None:
            logger.warning(
                "Email attachment job %s was not found for deal %s",
                job_id,
                deal_id,
            )
            return
        job.status = status
        job.result_data = result_data
        job.error_message = error_message
        if retry_count is not None:
            job.retry_count = retry_count
        await session.commit()
        return


@app.task(
    bind=True,
    name="deal_asset.extract_email_attachments_org",
    acks_late=True,
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=900,
    time_limit=960,
)
def extract_email_attachments_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Extract email attachments into deal assets for an entire organisation.

    Uses Redis lock to prevent concurrent execution for the same org.
    """
    started_at = datetime.now(UTC).isoformat()
    lock_key = f"task_lock:extract_email_attachments_org:{organisation_id}"

    if not acquire_task_lock(lock_key, ttl=960):
        logger.info(
            "Email attachment extraction: skipping org %s — already running",
            organisation_id,
        )
        return {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    logger.info(
        "Email attachment extraction: starting for org %s",
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        service = DealAssetEmailAttachmentService(org_uuid)
        stats = await service.process_organisation()
        for asset_id in stats.get("post_upload_asset_ids", []):
            send_deal_task(
                "deal_asset.post_upload",
                args=[organisation_id, str(asset_id)],
                queue="deal",
            )
        for continuation in stats.get("continuations", []):
            email_ids = list(continuation["email_ids"])
            for offset in range(0, len(email_ids), MAX_SCOPED_EMAIL_IDS):
                await dispatch_email_attachment_extraction_async(
                    organisation_id=org_uuid,
                    deal_id=UUID(str(continuation["deal_id"])),
                    email_ids=[
                        UUID(email_id)
                        for email_id in email_ids[offset : offset + MAX_SCOPED_EMAIL_IDS]
                    ],
                    trigger="organisation_repair_continuation",
                )
        for continuation in stats.get("retry_continuations", []):
            email_ids = list(continuation["email_ids"])
            for offset in range(0, len(email_ids), MAX_SCOPED_EMAIL_IDS):
                await dispatch_email_attachment_extraction_async(
                    organisation_id=org_uuid,
                    deal_id=UUID(str(continuation["deal_id"])),
                    email_ids=[
                        UUID(email_id)
                        for email_id in email_ids[offset : offset + MAX_SCOPED_EMAIL_IDS]
                    ],
                    trigger="organisation_retry_continuation",
                )
        if stats.get("unscoped_errors", stats["total_errors"]) > 0:
            raise RuntimeError(
                "Organisation email attachment extraction completed with "
                f"{stats.get('unscoped_errors', stats['total_errors'])} error(s)"
            )
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **stats,
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Email attachment extraction failed for org %s: %s",
            organisation_id,
            exc,
            exc_info=True,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=120 * (2**self.request.retries)) from exc

        raise
    finally:
        release_task_lock(lock_key)


@app.task(
    bind=True,
    name="deal_asset.extract_email_attachments_deal",
    acks_late=True,
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=300,
    time_limit=360,
)
def extract_email_attachments_deal_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    email_ids: list[str] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Extract email attachments for a single deal.

    Triggered downstream from email matching when new emails are
    associated with a deal.
    """
    if email_ids == []:
        raise ValueError("email_ids must be non-empty; use None for explicit repair")
    if email_ids is not None and len(email_ids) > MAX_SCOPED_EMAIL_IDS:
        raise ValueError(f"email_ids cannot exceed {MAX_SCOPED_EMAIL_IDS}")
    parsed_email_ids = [UUID(email_id) for email_id in email_ids] if email_ids is not None else None
    org_uuid = UUID(organisation_id)
    deal_uuid = UUID(deal_id)
    parsed_job_id = UUID(job_id) if job_id else None
    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Email attachment extraction: deal %s in org %s",
        deal_id,
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        if parsed_job_id is not None:
            await _update_attachment_job(
                org_uuid,
                deal_uuid,
                parsed_job_id,
                status=JobStatus.IN_PROGRESS,
                result_data=None,
                error_message=None,
            )

        service = DealAssetEmailAttachmentService(org_uuid)
        stats = await service.process_deal(deal_uuid, email_ids=parsed_email_ids)
        # The service commits PROCESSING rows first. A broker failure must escape this
        # task so retry can rediscover and republish the same idempotent asset IDs.
        for asset_id in stats.get("post_upload_asset_ids", []):
            send_deal_task(
                "deal_asset.post_upload",
                args=[organisation_id, str(asset_id)],
                queue="deal",
            )
        if stats["errors"] > 0:
            raise _RetryableAttachmentError(
                f"Email attachment extraction completed with {stats['errors']} retryable error(s)",
                list(stats.get("retryable_email_ids", [])),
            )
        remaining_email_ids = list(stats.get("remaining_email_ids", []))
        if stats.get("limit_reached") and remaining_email_ids:
            for offset in range(0, len(remaining_email_ids), MAX_SCOPED_EMAIL_IDS):
                await dispatch_email_attachment_extraction_async(
                    organisation_id=org_uuid,
                    deal_id=deal_uuid,
                    email_ids=[
                        UUID(email_id)
                        for email_id in remaining_email_ids[offset : offset + MAX_SCOPED_EMAIL_IDS]
                    ],
                    trigger="attachment_limit_continuation",
                )
        response = {
            "status": (
                "completed_with_warnings" if stats.get("permanent_skips", 0) > 0 else "success"
            ),
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **stats,
        }
        if parsed_job_id is not None:
            await _update_attachment_job(
                org_uuid,
                deal_uuid,
                parsed_job_id,
                status=JobStatus.COMPLETED,
                result_data=response,
                error_message=None,
            )
        return response

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Email attachment extraction failed for deal %s: %s",
            deal_id,
            exc,
            exc_info=True,
        )
        if self.request.retries < self.max_retries:
            retry_count = self.request.retries + 1
            if parsed_job_id is not None:
                run_async(
                    _update_attachment_job(
                        org_uuid,
                        deal_uuid,
                        parsed_job_id,
                        status=JobStatus.PENDING,
                        result_data=None,
                        error_message=str(exc),
                        retry_count=retry_count,
                    )
                )
            retry_email_ids = (
                exc.email_ids
                if isinstance(exc, _RetryableAttachmentError) and exc.email_ids
                else email_ids
            )
            raise self.retry(
                exc=exc,
                countdown=60 * (2**self.request.retries),
                kwargs={
                    "organisation_id": organisation_id,
                    "deal_id": deal_id,
                    "email_ids": retry_email_ids,
                    "job_id": job_id,
                },
            ) from exc

        if parsed_job_id is not None:
            run_async(
                _update_attachment_job(
                    org_uuid,
                    deal_uuid,
                    parsed_job_id,
                    status=JobStatus.FAILED,
                    result_data=None,
                    error_message=str(exc),
                    retry_count=self.request.retries,
                )
            )
        raise


@app.task(
    name="deal_asset.extract_email_attachments_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def extract_email_attachments_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Dispatch email attachment extraction for all approved orgs."""
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch() -> dict[str, Any]:
        logger.info("Tier 1: Dispatching email attachment extraction for all orgs")

        org_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "orgs_triggered": 0,
            }

        orgs_triggered = 0
        for org_id in org_ids:
            try:
                extract_email_attachments_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    headers=correlation_headers(),
                )
                orgs_triggered += 1
            except Exception as e:
                logger.error(
                    "Failed to dispatch attachment extraction for org %s: %s",
                    org_id,
                    e,
                )

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "total_orgs": len(org_ids),
            "orgs_triggered": orgs_triggered,
        }

    try:
        return run_async(_dispatch())
    except Exception as e:
        logger.error(
            "Email attachment extraction all-orgs failed: %s",
            e,
            exc_info=True,
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
        }
