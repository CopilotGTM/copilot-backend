"""Dispatch helpers for stakeholder research jobs.

Creates a DealJobs record and enqueues a Celery task for each stakeholder.
Async variant provided for different call sites.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.infra.database.sessions import acquire_org_async_session

logger = logging.getLogger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
RESEARCH_COOLDOWN_DAYS = 30


async def dispatch_stakeholder_research_async(
    organisation_id: UUID,
    stakeholder_id: UUID,
    deal_id: UUID | None = None,
    user_id: UUID | None = None,
    force: bool = False,
) -> str | None:
    """Create a DealJobs record and dispatch research task (async variant).

    Args:
        organisation_id: Security boundary
        stakeholder_id: The stakeholder to research
        deal_id: Optional — stored on job for context
        user_id: Who triggered this (defaults to automated pipeline)
        force: Skip cooldown check (for manual user-triggered research)

    Returns:
        job_id string, or None if skipped (cooldown / not approved)
    """
    async for session in acquire_org_async_session(organisation_id):
        participant = await _load_research_participant(session, organisation_id, stakeholder_id)
        if not participant:
            logger.warning(
                "dispatch_stakeholder_research: participant stakeholder %s not found",
                stakeholder_id,
                extra={
                    "event": "stakeholder_research_dispatch_not_found",
                    "organisation_id": str(organisation_id),
                    "stakeholder_id": str(stakeholder_id),
                },
            )
            return None

        if _status_value(participant.approval_status) != "APPROVED":
            logger.debug(
                "dispatch_stakeholder_research: skipping non-approved stakeholder %s",
                stakeholder_id,
            )
            return None

        contact = await _load_contact(session, organisation_id, participant.related_local_id)
        email = _normalize_email(
            contact.email if contact and contact.email else participant.related_email
        )
        linkedin_url = _normalize_text(
            (getattr(contact, "linkedin_url", None) if contact else None)
            or getattr(participant, "linkedin_url", None)
        )
        if not email and not linkedin_url:
            logger.warning(
                "dispatch_stakeholder_research: participant %s has no email or LinkedIn URL",
                stakeholder_id,
            )
            return None

        resolved_deal_id = deal_id or participant.deal_id

        # When force=True (manual trigger), skip cooldown — user explicitly wants fresh research
        if not force and email:
            already_researched = await _find_recent_contact_research(
                session,
                organisation_id,
                email,
            )

            if already_researched and already_researched.last_researched_at:
                age = datetime.now(UTC) - already_researched.last_researched_at
                if age < timedelta(days=RESEARCH_COOLDOWN_DAYS):
                    if (
                        already_researched.contact_research
                        and contact
                        and contact.contact_id != already_researched.contact_id
                        and not contact.contact_research
                    ):
                        await session.execute(
                            update(CRMContact)
                            .where(
                                col(CRMContact.organisation_id) == organisation_id,
                                col(CRMContact.contact_id) == contact.contact_id,
                            )
                            .values(
                                contact_research=already_researched.contact_research,
                                last_researched_at=already_researched.last_researched_at,
                                updated_at=datetime.now(UTC),
                            )
                        )
                        await session.commit()
                    logger.info(
                        "Stakeholder research reused recent contact result",
                        extra={
                            "event": "stakeholder_research_reused",
                            "organisation_id": str(organisation_id),
                            "stakeholder_id": str(stakeholder_id),
                            "source_contact_id": str(already_researched.contact_id),
                        },
                    )
                    return None

        # Check for existing active job to avoid duplicate work. Email-backed
        # stakeholders dedupe per person; LinkedIn-only placeholders dedupe per row.
        existing_job_stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == organisation_id,
            col(DealJobs.job_type) == DealJobType.STAKEHOLDER_RESEARCH,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        if email:
            existing_job_stmt = existing_job_stmt.join(
                CRMDealParticipant,
                col(DealJobs.stakeholder_id) == col(CRMDealParticipant.participant_id),
            ).where(
                func.lower(func.trim(col(CRMDealParticipant.related_email))) == email,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
            )
        else:
            existing_job_stmt = existing_job_stmt.where(
                col(DealJobs.stakeholder_id) == stakeholder_id,
            )
        existing = (await session.execute(existing_job_stmt)).scalar_one_or_none()
        if existing:
            # Stale job guard: if PENDING for over 5 min, the Celery message was
            # likely lost (worker restart, etc.). Mark it FAILED and re-create.
            stale_threshold = timedelta(minutes=5)
            if (
                existing.status == JobStatus.PENDING
                and existing.created_at
                and (datetime.now(UTC) - existing.created_at) > stale_threshold
            ):
                existing.status = JobStatus.FAILED
                existing.error_message = "Stale PENDING job — re-dispatching"
                await session.commit()
                logger.info(
                    "Stakeholder research stale job marked failed",
                    extra={
                        "event": "stakeholder_research_job_recreated",
                        "organisation_id": str(organisation_id),
                        "job_id": str(existing.job_id),
                        "stakeholder_id": str(stakeholder_id),
                    },
                )
            else:
                if (
                    user_id is not None
                    and existing.user_id == AUTOMATED_PIPELINE_USER_ID
                    and existing.deal_id == resolved_deal_id
                ):
                    existing.user_id = user_id
                    existing.stakeholder_id = stakeholder_id
                    existing.job_metadata = {
                        **(existing.job_metadata or {}),
                        "trigger": "manual_research",
                        "claimed_from": "automated_pipeline",
                        "stakeholder_id": str(stakeholder_id),
                        "deal_id": str(resolved_deal_id),
                    }
                    await session.commit()
                    logger.info(
                        "Stakeholder research active automated job claimed by user",
                        extra={
                            "event": "stakeholder_research_job_claimed",
                            "organisation_id": str(organisation_id),
                            "job_id": str(existing.job_id),
                            "stakeholder_id": str(stakeholder_id),
                            "user_id": str(user_id),
                        },
                    )
                logger.debug(
                    "dispatch_stakeholder_research: active job exists for email=%s",
                    email,
                )
                return str(existing.job_id)

        is_manual_trigger = user_id is not None
        metadata = {
            "trigger": "manual_research" if is_manual_trigger else "stakeholder_created",
            "stakeholder_id": str(stakeholder_id),
            "deal_id": str(resolved_deal_id),
        }
        if not is_manual_trigger:
            metadata["source"] = "automated_pipeline"

        # Keep one row per dispatch request so user-visible job streams and
        # retries stay tied to who/what triggered research instead of collapsing
        # multiple manual calls into one invisible action.
        job = DealJobs(
            organisation_id=organisation_id,
            user_id=user_id or AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.STAKEHOLDER_RESEARCH,
            deal_id=resolved_deal_id,
            stakeholder_id=stakeholder_id,
            status=JobStatus.PENDING,
            job_metadata=metadata,
        )
        session.add(job)
        await session.commit()
        job_id_str = str(job.job_id)

    # Dispatch via client (by task name) to avoid importing the full worker
    # task module, which pulls in heavy dependencies not available in the API container
    from src.workers.deal_worker.client import send_deal_task_async

    await send_deal_task_async(
        "stakeholder_research.process_job",
        kwargs={"organisation_id": str(organisation_id), "job_id": job_id_str},
        queue="deal_jobs",
    )

    logger.info(
        "Stakeholder research dispatched",
        extra={
            "event": "stakeholder_research_dispatched",
            "organisation_id": str(organisation_id),
            "stakeholder_id": str(stakeholder_id),
            "deal_id": str(deal_id) if deal_id else None,
            "job_id": job_id_str,
            "user_id": str(user_id) if user_id else None,
        },
    )
    return job_id_str


async def _load_research_participant(
    session,
    organisation_id: UUID,
    participant_id: UUID,
) -> CRMDealParticipant | None:
    stmt = select(CRMDealParticipant).where(
        col(CRMDealParticipant.organisation_id) == organisation_id,
        col(CRMDealParticipant.participant_id) == participant_id,
        col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
        col(CRMDealParticipant.is_deleted).is_(False),
    )
    result = await session.execute(stmt)
    return cast(CRMDealParticipant | None, result.scalar_one_or_none())


async def _load_contact(
    session,
    organisation_id: UUID,
    contact_id: UUID | None,
) -> CRMContact | None:
    if not contact_id:
        return None
    contact = await session.get(CRMContact, contact_id)
    if not contact or contact.organisation_id != organisation_id or contact.is_deleted:
        return None
    return cast(CRMContact, contact)


async def _find_recent_contact_research(
    session,
    organisation_id: UUID,
    email: str,
) -> CRMContact | None:
    stmt = (
        select(CRMContact)
        .where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(func.trim(col(CRMContact.email))) == email,
            col(CRMContact.contact_research).isnot(None),
            col(CRMContact.last_researched_at).isnot(None),
        )
        .order_by(col(CRMContact.last_researched_at).desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return cast(CRMContact | None, result.scalar_one_or_none())


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


def _normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    return normalized or None


def _status_value(status: object) -> str:
    return str(getattr(status, "value", status))
