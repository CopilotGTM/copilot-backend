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
from src.infra.database.sessions import acquire_master_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_asset_email_attachment_service import (
    DealAssetEmailAttachmentService,
)

logger = get_logger(__name__)


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

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
        }
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
) -> dict[str, Any]:
    """Extract email attachments for a single deal.

    Triggered downstream from email matching when new emails are
    associated with a deal.
    """
    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Email attachment extraction: deal %s in org %s",
        deal_id,
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        service = DealAssetEmailAttachmentService(UUID(organisation_id))
        stats = await service.process_deal(UUID(deal_id))
        if stats["errors"] > 0:
            raise RuntimeError(
                f"Email attachment extraction completed with {stats['errors']} error(s)"
            )
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **stats,
        }

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
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

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
