"""Task 3: Email Matching Tasks.

This module provides Celery tasks for matching emails to CRM deals.

TRIGGERS:
---------
1. Email sync completes → match_email_to_deals_org_task (all new emails)
2. Stakeholder added → match_email_to_deals_task (single deal re-match)
3. Manual refresh → match_email_to_deals_task (single deal re-match)

ARCHITECTURE:
-------------
    Email Sync (org_task.py)
            │
            └─→ match_email_to_deals_org_task (process all emails for org)

    Task 1 (stakeholder sync) with PARTICIPANT_ADDED
            │
            └─→ match_email_to_deals_task (re-match single deal)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_email_matcher import DealEmailMatcher

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="deal.match_email_to_deals_org",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,  # 10 minutes
    time_limit=660,  # 11 minutes
)
def match_email_to_deals_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Match emails to deals for an entire organisation.

    This task is typically triggered after email sync completes.
    It processes unmatched emails and matches them against active deals.

    Args:
        organisation_id: UUID of the organization (string)

    Returns:
        Dictionary with matching results:
        {
            "status": "success" | "failed",
            "organisation_id": "...",
            "started_at": "...",
            "completed_at": "...",
            "emails_processed": N,
            "emails_matched": N,
            "associations_created": N,
            "deals_with_matches": N,
        }
    """
    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Task 3 (org): Matching emails to deals for org %s",
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        async for session in acquire_org_async_session(org_uuid):
            matcher = DealEmailMatcher(session, org_uuid)
            stats = await matcher.process_organisation()

            return {
                "status": "success",
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "emails_processed": stats.get("emails_processed", 0),
                "emails_matched": stats.get("emails_matched", 0),
                "associations_created": stats.get("associations_created", 0),
                "deals_with_matches": stats.get("deals_with_matches", 0),
            }

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Task 3 (org) failed for org %s: %s",
            organisation_id,
            str(exc),
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


@app.task(
    bind=True,
    name="deal.match_email_to_deals",
    acks_late=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=120,  # 2 minutes
    time_limit=150,  # 2.5 minutes
)
def match_email_to_deals_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
) -> dict[str, Any]:
    """Match emails to a single deal.

    This task is triggered when:
    - A new deal is created (NEW_DEAL)
    - A stakeholder is added to an existing deal (PARTICIPANT_ADDED)
    - User requests a manual refresh

    It re-processes all emails against this specific deal's
    stakeholders to find historical email threads.

    Args:
        organisation_id: UUID of the organization (string)
        deal_id: UUID of the deal to re-match (string)

    Returns:
        Dictionary with matching results
    """
    started_at = datetime.now(UTC).isoformat()

    logger.info(
        "Task 3 (deal): Re-matching emails for deal %s in org %s",
        deal_id,
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        async for session in acquire_org_async_session(org_uuid):
            matcher = DealEmailMatcher(session, org_uuid)
            stats = await matcher.process_deal(deal_uuid)

            return {
                "status": "success",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "emails_processed": stats.get("emails_processed", 0),
                "emails_matched": stats.get("emails_matched", 0),
                "associations_created": stats.get("associations_created", 0),
            }

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Task 3 (deal) failed for deal %s: %s",
            deal_id,
            str(exc),
            exc_info=True,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
