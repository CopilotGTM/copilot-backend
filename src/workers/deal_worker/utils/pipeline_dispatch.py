"""Shared Celery orchestration helpers for deal insight pipelines.

This module centralizes the async dispatch pattern used by note, email,
meeting, and CRM-sync triggers:

1. Reuse or create a single active ``DEAL_MEMORY_BUILD`` job per deal.
2. Run Risk/Blocker extraction and Stakeholder analysis in parallel.
3. Run Deal Memory only after both specialist tasks complete.

Keeping this orchestration in one place prevents trigger-specific code paths
from drifting into slightly different queueing behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from celery import chain, chord, group
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.workers.deal_worker.client import get_deal_celery_client

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


async def get_or_create_deal_memory_job(
    session: AsyncSession,
    organisation_id: UUID,
    deal_id: UUID,
    source: str,
    trigger: str,
    *,
    user_id: UUID | None = None,
    trigger_source_id: UUID | str | None = None,
    chained_from: str | None = None,
) -> tuple[UUID, bool]:
    """Return an active deal memory job for a deal, creating one if needed.

    We intentionally deduplicate active jobs so multiple triggers arriving
    close together do not enqueue overlapping deal-memory rebuilds.

    Args:
        session: Open organisation-scoped async session used for the lookup/create.
        organisation_id: Organisation security boundary for the job.
        deal_id: Deal to attach the job to.
        source: High-level source of the trigger (for example ``email_matching``).
        trigger: Specific trigger type (for example ``new_note``).
        user_id: Optional actor user ID. Defaults to automated pipeline sentinel.
        trigger_source_id: Optional source record ID that caused this update.
        chained_from: Optional upstream pipeline stage name for audit/debugging.

    Returns:
        A tuple of ``(job_id, created)`` where ``created`` is ``True`` only when
        a new ``DEAL_MEMORY_BUILD`` row was inserted.
    """
    # Deal memory is deal-scoped, so the active-job lookup always uses
    # ``event_id IS NULL`` and the dedicated deal-memory job type.
    stmt = select(DealJobs).where(
        col(DealJobs.organisation_id) == organisation_id,
        col(DealJobs.deal_id) == deal_id,
        col(DealJobs.event_id).is_(None),
        col(DealJobs.job_type) == DealJobType.DEAL_MEMORY_BUILD,
        col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
    )
    result = await session.execute(stmt)
    existing_job = result.scalars().first()

    if existing_job:
        # Keep trigger metadata fresh on reused active jobs so downstream logic
        # can still inspect the latest triggering source/id.
        existing_metadata = existing_job.job_metadata or {}
        metadata_changed = False
        if existing_metadata.get("source") != source:
            existing_metadata["source"] = source
            metadata_changed = True
        if existing_metadata.get("trigger_type") != trigger:
            existing_metadata["trigger_type"] = trigger
            metadata_changed = True
        if trigger_source_id is not None and existing_metadata.get("trigger_source_id") != str(
            trigger_source_id
        ):
            existing_metadata["trigger_source_id"] = str(trigger_source_id)
            metadata_changed = True
        if chained_from is not None and existing_metadata.get("chained_from") != chained_from:
            existing_metadata["chained_from"] = chained_from
            metadata_changed = True

        if metadata_changed:
            existing_job.job_metadata = existing_metadata
        return existing_job.job_id, False

    # Persist enough trigger metadata for the downstream deal-memory task to
    # understand why it ran and, when available, which upstream record caused it.
    metadata: dict[str, str] = {
        "source": source,
        "trigger_type": trigger,
    }
    if trigger_source_id is not None:
        metadata["trigger_source_id"] = str(trigger_source_id)
    if chained_from is not None:
        metadata["chained_from"] = chained_from

    job = DealJobs(
        organisation_id=organisation_id,
        user_id=user_id or AUTOMATED_PIPELINE_USER_ID,
        job_type=DealJobType.DEAL_MEMORY_BUILD,
        deal_id=deal_id,
        event_id=None,
        status=JobStatus.PENDING,
        job_metadata=metadata,
    )
    session.add(job)
    await session.flush()
    await session.refresh(job)
    return job.job_id, True


def build_deal_insight_chord(
    organisation_id: str,
    deal_id: str,
    job_id: str,
):
    """Build the parallel risk+stakeholder -> deal memory canvas.

    The callback signature is immutable so the chord header results are not
    injected into the deal-memory task arguments. Deal Memory reads specialist
    outputs from the database instead of consuming raw Celery return payloads.

    Args:
        organisation_id: Organisation ID serialized for Celery transport.
        deal_id: Deal ID serialized for Celery transport.
        job_id: Existing ``DEAL_MEMORY_BUILD`` job to complete in the callback.

    Returns:
        A Celery canvas representing ``chord(group(risk, stakeholder), deal_memory)``.
    """
    celery_app = get_deal_celery_client()

    # Both specialist tasks are deal-scoped and run independently.
    risk_blocker_sig = celery_app.signature(
        "risk_blocker.extract_deal_risks_blockers_task",
        args=[organisation_id, deal_id],
        queue="deal_jobs",
        immutable=True,
    )
    stakeholder_sig = celery_app.signature(
        "stakeholder.analyze_deal_stakeholders_task",
        args=[organisation_id, deal_id],
        queue="deal_jobs",
        immutable=True,
    )
    # Deal Memory runs only after both upstream tasks finish successfully.
    deal_memory_sig = celery_app.signature(
        "deal_memory.process_job",
        kwargs={"organisation_id": organisation_id, "job_id": job_id},
        queue="deal_jobs",
        immutable=True,
    )

    return chord(group(risk_blocker_sig, stakeholder_sig), deal_memory_sig)


def dispatch_deal_insight_chord(
    organisation_id: str,
    deal_id: str,
    job_id: str,
    *,
    prefix_signatures: Sequence | None = None,
) -> None:
    """Dispatch the deal insight pipeline.

    When ``prefix_signatures`` are provided, they run first and the chord is
    attached at the end of the chain. This is used by the meeting-summary
    pipeline so all pending meeting summaries finish before the parallel
    specialist stage starts.

    Args:
        organisation_id: Organisation ID serialized for Celery transport.
        deal_id: Deal ID serialized for Celery transport.
        job_id: Existing ``DEAL_MEMORY_BUILD`` job to complete in the callback.
        prefix_signatures: Optional sequential Celery signatures to run before
            the parallel risk/stakeholder stage.
    """
    pipeline = build_deal_insight_chord(organisation_id, deal_id, job_id)
    if prefix_signatures:
        # Meeting summary tasks must stay sequential, so we chain them first and
        # then attach the parallel risk/stakeholder -> deal-memory stage.
        pipeline = chain(*prefix_signatures, pipeline)

    pipeline.apply_async()
