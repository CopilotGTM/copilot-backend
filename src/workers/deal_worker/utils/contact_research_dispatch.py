"""Contact research dispatch boundary for API and retry callers.

API callers use this module after committing a contact create. This module owns
eligibility checks and active-job dedupe; the worker owns provider calls and
persistence.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.infra.database.sessions import acquire_org_async_session
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.contacts.research_service import (
    CONTACT_RESEARCH_PROMPT_VERSION,
    PROFILE_METADATA_KEY,
    profile_payload_hash,
)
from src.workers.deal_worker.client import send_deal_task_async

logger = logging.getLogger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
CONTACT_RESEARCH_COOLDOWN_DAYS = 30
ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.IN_PROGRESS)


async def dispatch_contact_research_for_contact(
    *,
    organisation_id: UUID,
    contact_id: UUID | None,
    user_id: UUID | None = None,
    trigger: str,
    force_refresh: bool = False,
) -> str | None:
    """Skip unlinked entities before entering contact research eligibility checks."""
    if contact_id is None:
        return None
    return await dispatch_contact_research_async(
        organisation_id=organisation_id,
        contact_id=contact_id,
        user_id=user_id,
        trigger=trigger,
        force_refresh=force_refresh,
    )


async def dispatch_contact_research_async(
    *,
    organisation_id: UUID,
    contact_id: UUID,
    user_id: UUID | None = None,
    trigger: str = "contact_created",
    force_refresh: bool = False,
) -> str | None:
    """Create and enqueue a contact research job when the contact is eligible."""
    async for session in acquire_org_async_session(organisation_id):
        # Lock the contact through job creation so simultaneous API/meeting/email
        # triggers cannot both pass dedupe; the org predicate is the tenant boundary.
        contact_result = await session.execute(
            select(CRMContact)
            .where(
                col(CRMContact.contact_id) == contact_id,
                col(CRMContact.organisation_id) == organisation_id,
                col(CRMContact.is_deleted).is_(False),
            )
            .with_for_update()
        )
        contact = contact_result.scalars().first()
        if not contact:
            logger.warning(
                "Contact research dispatch skipped because contact was not found",
                extra={
                    "event": "contact_research_dispatch_not_found",
                    "organisation_id": str(organisation_id),
                    "contact_id": str(contact_id),
                },
            )
            return None

        email = (contact.email or "").strip().lower()
        has_safe_email = bool(AccountDomainResolutionService.derive_safe_domain_from_email(email))
        profile_enrichment = contact.profile_enrichment or {}
        has_cached_provider_data = bool(profile_enrichment) and (
            profile_enrichment.get("status") != "not_found"
        )
        if not has_safe_email and not has_cached_provider_data:
            return None

        if (
            not force_refresh
            and contact.contact_research
            and _is_recent(contact.last_researched_at)
            and _has_current_summary(profile_enrichment)
        ):
            return None

        existing = await session.execute(
            select(DealJobs).where(
                col(DealJobs.organisation_id) == organisation_id,
                col(DealJobs.job_type) == DealJobType.CONTACT_RESEARCH,
                col(DealJobs.status).in_(ACTIVE_JOB_STATUSES),
            )
        )
        for job in existing.scalars().all():
            if (job.job_metadata or {}).get("contact_id") == str(contact_id):
                return str(job.job_id)

        job = DealJobs(
            organisation_id=organisation_id,
            user_id=user_id or AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.CONTACT_RESEARCH,
            status=JobStatus.PENDING,
            job_metadata={
                "contact_id": str(contact_id),
                "normalized_email": email or None,
                "trigger": trigger,
                "force_refresh": force_refresh,
                "source": "api" if user_id else "automated_pipeline",
            },
        )
        session.add(job)
        await ActivityFeedProjectionService(
            session,
            organisation_id,
        ).upsert_contact_enrichment_triggered(
            contact,
            job_id=job.job_id,
            actor_user_id=user_id,
        )
        await session.commit()
        job_id = str(job.job_id)

    await send_deal_task_async(
        "contact_research.process_job",
        kwargs={"organisation_id": str(organisation_id), "job_id": job_id},
        queue="deal_jobs",
    )
    return job_id


def _is_recent(last_researched_at: datetime | None) -> bool:
    if not last_researched_at:
        return False
    return last_researched_at >= datetime.now(UTC) - timedelta(days=CONTACT_RESEARCH_COOLDOWN_DAYS)


def _has_current_summary(profile_enrichment: dict) -> bool:
    """Treat legacy summaries as stale after prompt or provider evidence changes."""
    metadata = profile_enrichment.get(PROFILE_METADATA_KEY)
    if not isinstance(metadata, dict):
        return False
    if metadata.get("prompt_version") != CONTACT_RESEARCH_PROMPT_VERSION:
        return False
    payload_hash = profile_payload_hash(profile_enrichment)
    return bool(payload_hash) and metadata.get("summary_source_hash") == payload_hash
