"""Shared Celery orchestration helpers for deal insight pipelines.

This module centralizes the async dispatch pattern used by note, email,
meeting, and CRM-sync triggers:

1. Reuse or create a single active ``DEAL_MEMORY_BUILD`` job per deal.
2. Run Deal Memory first — it builds the foundational deal narrative.
3. Run Risk/Blocker extraction and Stakeholder analysis in parallel
   after Deal Memory completes (they read deal memory as context).
4. Run deal task sync after the insight group so task extraction sees the
   updated deal memory, risks/blockers, and stakeholder context.
5. Optionally run deal update after task sync when the feature flag is enabled.

Keeping this orchestration in one place prevents trigger-specific code paths
from drifting into slightly different queueing behavior.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import UUID

from celery import chain, group
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import org_async_session_scope, org_session_scope
from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import get_deal_celery_client

logger = get_logger(__name__)

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


def _create_pipeline_jobs(
    organisation_id: str,
    deal_id: str,
) -> tuple[str, str, str, str | None]:
    """Create DealJobs records for downstream deal insight pipeline tasks.

    Jobs are created BEFORE Celery dispatch so the UI can show PENDING status
    immediately and costing data has a record to attach to.

    Returns:
        Tuple of risk, stakeholder, task-sync, and optional deal-update job IDs.
    """
    org_uuid = UUID(organisation_id)
    deal_uuid = UUID(deal_id)

    with org_session_scope(org_uuid) as session:
        risk_job = DealJobs(
            organisation_id=org_uuid,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.RISK_BLOCKER_EXTRACTION,
            deal_id=deal_uuid,
            status=JobStatus.PENDING,
            job_metadata={"source": "automated_pipeline"},
        )
        stakeholder_job = DealJobs(
            organisation_id=org_uuid,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.STAKEHOLDER_ANALYSIS,
            deal_id=deal_uuid,
            status=JobStatus.PENDING,
            job_metadata={"source": "automated_pipeline"},
        )
        task_job = DealJobs(
            organisation_id=org_uuid,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.DEAL_TASK_SYNC,
            deal_id=deal_uuid,
            status=JobStatus.PENDING,
            job_metadata={
                "source": "automated_pipeline",
                "chained_from": "deal_insights",
            },
        )
        session.add(risk_job)
        session.add(stakeholder_job)
        session.add(task_job)

        deal_update_job = None
        if _deal_update_pipeline_enabled():
            deal_update_job = DealJobs(
                organisation_id=org_uuid,
                user_id=AUTOMATED_PIPELINE_USER_ID,
                job_type=DealJobType.DEAL_UPDATE,
                deal_id=deal_uuid,
                status=JobStatus.PENDING,
                job_metadata={
                    "source": "automated_pipeline",
                    "trigger_type": "deal_insight_pipeline",
                    "trigger_source_id": None,
                    "instruction": (
                        "Update safe deal CRM fields from the latest deal insight context."
                    ),
                    "approval_mode": "auto_safe_fields",
                    "chained_from": "deal_insights",
                },
            )
            session.add(deal_update_job)
        session.flush()

        risk_job_id = str(risk_job.job_id)
        stakeholder_job_id = str(stakeholder_job.job_id)
        task_job_id = str(task_job.job_id)
        deal_update_job_id = str(deal_update_job.job_id) if deal_update_job else None

        session.commit()

    logger.info(
        "Created pipeline jobs for deal %s: risk=%s, stakeholder=%s, task=%s, update=%s",
        deal_id,
        risk_job_id,
        stakeholder_job_id,
        task_job_id,
        deal_update_job_id,
        extra={
            "event": "deal_insight_pipeline_jobs_created",
            "organisation_id": str(org_uuid),
            "deal_id": str(deal_uuid),
            "risk_blocker_job_id": risk_job_id,
            "stakeholder_job_id": stakeholder_job_id,
            "deal_task_job_id": task_job_id,
            "deal_update_job_id": deal_update_job_id,
            "pipeline_type": "sync",
        },
    )
    return risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id


async def _create_pipeline_jobs_async(
    organisation_id: str,
    deal_id: str,
) -> tuple[str, str, str, str | None]:
    """Async equivalent of pipeline job creation for async worker call sites."""
    org_uuid = UUID(organisation_id)
    deal_uuid = UUID(deal_id)

    async with org_async_session_scope(org_uuid) as session:
        risk_job = DealJobs(
            organisation_id=org_uuid,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.RISK_BLOCKER_EXTRACTION,
            deal_id=deal_uuid,
            status=JobStatus.PENDING,
            job_metadata={"source": "automated_pipeline"},
        )
        stakeholder_job = DealJobs(
            organisation_id=org_uuid,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.STAKEHOLDER_ANALYSIS,
            deal_id=deal_uuid,
            status=JobStatus.PENDING,
            job_metadata={"source": "automated_pipeline"},
        )
        task_job = DealJobs(
            organisation_id=org_uuid,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.DEAL_TASK_SYNC,
            deal_id=deal_uuid,
            status=JobStatus.PENDING,
            job_metadata={
                "source": "automated_pipeline",
                "chained_from": "deal_insights",
            },
        )
        session.add(risk_job)
        session.add(stakeholder_job)
        session.add(task_job)

        deal_update_job = None
        if _deal_update_pipeline_enabled():
            deal_update_job = DealJobs(
                organisation_id=org_uuid,
                user_id=AUTOMATED_PIPELINE_USER_ID,
                job_type=DealJobType.DEAL_UPDATE,
                deal_id=deal_uuid,
                status=JobStatus.PENDING,
                job_metadata={
                    "source": "automated_pipeline",
                    "trigger_type": "deal_insight_pipeline",
                    "trigger_source_id": None,
                    "instruction": (
                        "Update safe deal CRM fields from the latest deal insight context."
                    ),
                    "approval_mode": "auto_safe_fields",
                    "chained_from": "deal_insights",
                },
            )
            session.add(deal_update_job)
        await session.flush()
        await session.refresh(risk_job)
        await session.refresh(stakeholder_job)
        await session.refresh(task_job)
        if deal_update_job:
            await session.refresh(deal_update_job)

        risk_job_id = str(risk_job.job_id)
        stakeholder_job_id = str(stakeholder_job.job_id)
        task_job_id = str(task_job.job_id)
        deal_update_job_id = str(deal_update_job.job_id) if deal_update_job else None

    logger.info(
        "Created async pipeline jobs for deal %s: risk=%s, stakeholder=%s, task=%s, update=%s",
        deal_id,
        risk_job_id,
        stakeholder_job_id,
        task_job_id,
        deal_update_job_id,
        extra={
            "event": "deal_insight_pipeline_jobs_created",
            "organisation_id": str(org_uuid),
            "deal_id": str(deal_uuid),
            "risk_blocker_job_id": risk_job_id,
            "stakeholder_job_id": stakeholder_job_id,
            "deal_task_job_id": task_job_id,
            "deal_update_job_id": deal_update_job_id,
            "pipeline_type": "async",
        },
    )
    return risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id


def _deal_update_pipeline_enabled() -> bool:
    """Return whether automatic deal-update pipeline wiring is enabled."""
    return os.getenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", "false").lower() == "true"


def _build_deal_insight_canvas(
    organisation_id: str,
    deal_id: str,
    job_id: str,
    risk_job_id: str,
    stakeholder_job_id: str,
    task_job_id: str,
    deal_update_job_id: str | None = None,
):
    """Build the deal memory -> parallel insights -> task sync pipeline.

    Deal Memory runs first to build the foundational deal narrative.
    Risk/Blocker extraction and Stakeholder analysis then run in parallel,
    using the deal memory as their primary context. Deal task sync runs after
    the insight group, and deal update can run as a final feature-flagged step.

    All signatures are immutable — downstream tasks read from the database,
    not from Celery return payloads.

    Args:
        organisation_id: Organisation ID serialized for Celery transport.
        deal_id: Deal ID serialized for Celery transport.
        job_id: Existing ``DEAL_MEMORY_BUILD`` job to complete first.

    Returns:
        A Celery canvas representing the deal insight pipeline.
    """
    celery_app = get_deal_celery_client()

    # Attach headers per signature so every branch's task_prerun receives the same
    # correlation set, regardless of how Celery serializes the canvas.
    # Deal Memory runs first — builds the foundational deal narrative.
    deal_memory_sig = celery_app.signature(
        "deal_memory.process_job",
        kwargs={"organisation_id": organisation_id, "job_id": job_id},
        queue="deal_jobs",
        immutable=True,
        headers=correlation_headers(),
    )
    # Risk and Stakeholder run in parallel after deal memory completes.
    risk_blocker_sig = celery_app.signature(
        "risk_blocker.extract_deal_risks_blockers_task",
        kwargs={
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "job_id": risk_job_id,
        },
        queue="deal_jobs",
        immutable=True,
        headers=correlation_headers(),
    )
    stakeholder_sig = celery_app.signature(
        "stakeholder.analyze_deal_stakeholders_task",
        kwargs={
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "job_id": stakeholder_job_id,
        },
        queue="deal_jobs",
        immutable=True,
        headers=correlation_headers(),
    )
    deal_task_sig = celery_app.signature(
        "deal_tasks.sync_from_summary",
        kwargs={
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "job_id": task_job_id,
        },
        queue="deal_jobs",
        immutable=True,
        headers=correlation_headers(),
    )

    insight_group = group(risk_blocker_sig, stakeholder_sig)
    if deal_update_job_id:
        deal_update_sig = celery_app.signature(
            "deal_update.process_job",
            kwargs={"organisation_id": organisation_id, "job_id": deal_update_job_id},
            queue="deal_jobs",
            immutable=True,
            headers=correlation_headers(),
        )
        return chain(deal_memory_sig, insight_group, deal_task_sig, deal_update_sig)

    return chain(deal_memory_sig, insight_group, deal_task_sig)


def build_deal_insight_pipeline(
    organisation_id: str,
    deal_id: str,
    job_id: str,
):
    """Build the deal memory -> parallel risk+stakeholder pipeline."""
    risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id = _create_pipeline_jobs(
        organisation_id, deal_id
    )
    return _build_deal_insight_canvas(
        organisation_id,
        deal_id,
        job_id,
        risk_job_id,
        stakeholder_job_id,
        task_job_id,
        deal_update_job_id,
    )


def dispatch_deal_insight_pipeline(
    organisation_id: str,
    deal_id: str,
    job_id: str,
    *,
    prefix_signatures: Sequence | None = None,
) -> None:
    """Dispatch the deal insight pipeline.

    Pipeline order: deal_memory -> group(risk, stakeholder) -> deal_task_sync -> optional deal_update

    When ``prefix_signatures`` are provided (e.g. meeting summaries), they run
    first in sequence before the deal insight pipeline starts.

    Args:
        organisation_id: Organisation ID serialized for Celery transport.
        deal_id: Deal ID serialized for Celery transport.
        job_id: Existing ``DEAL_MEMORY_BUILD`` job to run first.
        prefix_signatures: Optional sequential Celery signatures to run before
            the deal insight pipeline (e.g. meeting summary tasks).
    """
    pipeline = build_deal_insight_pipeline(organisation_id, deal_id, job_id)
    if prefix_signatures:
        pipeline = chain(*prefix_signatures, pipeline)

    # Headers are already attached to the individual signatures that need them.
    pipeline.apply_async()


async def dispatch_deal_insight_pipeline_async(
    organisation_id: str,
    deal_id: str,
    job_id: str,
    *,
    prefix_signatures: Sequence | None = None,
) -> None:
    """Async-safe dispatch variant for code paths already running inside an event loop."""
    risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id = (
        await _create_pipeline_jobs_async(organisation_id, deal_id)
    )
    pipeline = _build_deal_insight_canvas(
        organisation_id,
        deal_id,
        job_id,
        risk_job_id,
        stakeholder_job_id,
        task_job_id,
        deal_update_job_id,
    )
    if prefix_signatures:
        pipeline = chain(*prefix_signatures, pipeline)

    # Headers are already attached to the individual signatures that need them.
    pipeline.apply_async()
