"""DEPRECATED: Celery tasks for deal-email matching.

DEPRECATION NOTICE (January 2026):
----------------------------------
This module is DEPRECATED and moved to _deprecated folder.
Replaced by: src/workers/deal_worker/tasks/email_matching_tasks.py

New architecture:
- match_email_to_deals_org_task: Replaces trigger_deal_email_processing_all_orgs
- match_email_to_deals_task: Single deal re-matching (new capability)

The new tasks are triggered by:
1. Email sync completion (automatic)
2. Stakeholder addition (Task 1 triggers Task 3)
3. Manual refresh

Do NOT use these tasks for new development.
----------------------------------

This module provides Celery tasks that orchestrate the deal-email matching process:
- Match emails to CRM deals based on team and contact stakeholder participation
- Uses Email-First batch approach for efficient hourly processing
- Runs incrementally - only processes emails not yet in associations table

Tasks:
    - process_deal_emails_task: Process all emails for a single organisation
    - trigger_deal_email_processing_all_orgs: Periodic task for all organisations (hourly)

The tasks delegate to DealEmailMatcher service for the actual matching logic.
See docs/Technical Documentation/deal/deal-email-matching-automation.md for algorithm details.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.infra.database.factory import get_async_provider
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_email_matcher import process_deal_emails

logger = get_logger(__name__)


@app.task(bind=True, name="deal.process_deal_emails_task", acks_late=True, max_retries=3)
def process_deal_emails_task(self: Task, organisation_id: str) -> dict[str, int]:
    """
    Celery task to process email matching for an organisation.

    Uses Email-First batch approach:
    1. Load all active deals with stakeholders → Build lookup maps
    2. Query unprocessed emails (not yet in associations table)
    3. Match emails to deals using in-memory lookups
    4. Create associations for matches above threshold

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)

    Returns:
        Dictionary with processing results:
        - emails_processed: Total emails evaluated
        - emails_matched: Emails that had at least one deal match
        - associations_created: New DealEmailAssociation records created
        - deals_with_matches: Number of unique deals that got email associations
    """
    logger.info(f"Starting deal email processing for org {organisation_id}")

    async def _run():
        org_uuid = UUID(organisation_id)
        async for session in acquire_org_async_session(org_uuid):
            return await process_deal_emails(session, org_uuid)

    try:
        result = run_async(_run())
        logger.info(f"Deal email processing completed for org {organisation_id}: {result}")
        return result
    except Exception as e:
        logger.error(f"Deal email processing failed for org {organisation_id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e


@app.task(name="deal.trigger_deal_email_processing_all_orgs")
def trigger_deal_email_processing_all_orgs():
    """
    Periodic task to trigger deal email processing for all active organisations.

    This task runs hourly and enqueues individual processing tasks for each
    active organisation. Uses incremental processing - only emails not yet
    in the associations table are processed.

    Schedule: Every hour (configured in celery beat)
    """
    logger.info("Triggering deal email processing for all organisations")

    async def _run():
        provider = get_async_provider()
        async with provider.master_session() as session:
            # Fetch all active organisations
            stmt = select(Organisation).where(Organisation.status == OrganisationStatus.APPROVED)
            result = await session.execute(stmt)
            orgs = result.scalars().all()

            logger.info(f"Found {len(orgs)} active organisations for email processing")

            for org in orgs:
                # Enqueue task for each organisation
                process_deal_emails_task.apply_async(
                    args=[str(org.organisation_id)],
                    headers=correlation_headers(),
                )

            return len(orgs)

    count = run_async(_run())
    logger.info(f"Enqueued deal email processing for {count} organisations")
    return {"organisations_queued": count}
