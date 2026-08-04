"""Celery entrypoint for local contact creation and communication backfill."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobType
from src.infra.database.sessions import master_async_session_scope, org_async_session_scope
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.communications.hydration_service import (
    ContactCommunicationHydrationService,
)
from src.service.crm.communications.participant_resolution_service import (
    CommunicationHydrationBusyError,
)
from src.service.integrations.initial_sync_progress_service import (
    InitialSyncErrorCode,
    InitialSyncProgressService,
)
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.tasks.deal_asset_email_attachment_tasks import (
    dispatch_email_attachment_extraction_async,
)
from src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking import (
    VisibleDealJobTracker,
)
from src.workers.deal_worker.utils.contact_research_dispatch import (
    dispatch_contact_research_for_contact,
)
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline,
    get_or_create_deal_memory_job,
)

logger = get_logger(__name__)

# copilotgtm: keep attachment payloads aligned with hydration's 100-source batch;
# add a continuation coordinator only if measured replay volume outgrows this bound.
ATTACHMENT_EMAIL_CHUNK_SIZE = 100
HYDRATION_BUSY_MAX_RETRIES = 5


# Two delayed retries cover transient database failures without looping indefinitely. The hard
# limit gives a worker 60 seconds after the twenty-minute soft limit to unwind the batch cleanly.
# Batches remain bounded; the wider window accommodates measured large-tenant projection time.
@app.task(
    bind=True,
    name="deal.hydrate_contact_communications",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=1200,
    time_limit=1260,
)
def hydrate_contact_communications_task(
    self: Task,
    organisation_id: str,
    integration_id: str | None = None,
    changed_email_ids: list[str] | None = None,
    changed_event_ids: list[str] | None = None,
    contact_ids: list[str] | None = None,
    deal_ids: list[str] | None = None,
    trigger: str = "communication_sync",
    job_id: str | None = None,
    job_type: str | None = None,
    emit_events: bool = False,
    initial_sync_id: str | None = None,
    initial_sync_branch: Literal["email", "calendar"] | None = None,
    initial_sync_chunk_key: str | None = None,
) -> dict[str, Any]:
    """Run one retry-safe local hydration batch without provider research calls."""
    if emit_events and (not job_id or not job_type):
        raise ValueError("emit_events=True requires job_id and job_type")
    if not emit_events and (job_id or job_type):
        raise ValueError("job context requires emit_events=True")
    tracking_values = (initial_sync_id, initial_sync_branch, initial_sync_chunk_key)
    if any(tracking_values) and not all(tracking_values):
        raise ValueError("initial sync tracking fields must be supplied together")

    org_id = UUID(organisation_id)
    int_id = UUID(integration_id) if integration_id else None
    email_ids = _parse_uuids(changed_email_ids)
    event_ids = _parse_uuids(changed_event_ids)
    explicit_contact_ids = _parse_uuids(contact_ids)
    explicit_deal_ids = _parse_uuids(deal_ids)
    source_mode = changed_email_ids is not None or changed_event_ids is not None
    mode_count = sum((contact_ids is not None, deal_ids is not None, source_mode))
    if mode_count > 1:
        raise ValueError("contact, deal, and changed-source hydration cannot be mixed")
    if mode_count == 0:
        raise ValueError("a contact, deal, or changed-source payload is required")
    visible_job_type = DealJobType(job_type) if job_type else None
    tracker = (
        VisibleDealJobTracker(
            org_id,
            job_id,
            visible_job_type,
            emit_events=True,
        )
        if emit_events and job_id and visible_job_type
        else None
    )

    async def _run() -> dict[str, Any]:
        """Open both tenant sessions, commit the projection, and return worker-safe metrics.

        The master session identifies internal users while the org session owns all CRM writes.
        """
        if tracker:
            progress = (
                tracker.start
                if visible_job_type == DealJobType.DEAL_COMMUNICATION_HYDRATION
                else tracker.progress
            )
            await progress("hydrating_communications", "Matching communication history", 0.65)

        async with (
            master_async_session_scope() as master_session,
            org_async_session_scope(org_id) as org_session,
        ):
            service = ContactCommunicationHydrationService(
                org_session,
                master_session,
                org_id,
            )
            # Each task owns one bounded transaction. Retries reuse contacts and
            # replay deterministic activity IDs only after the full batch rolls back.
            if deal_ids is not None:
                result = await service.backfill_deals(
                    explicit_deal_ids,
                    integration_id=int_id,
                )
            elif contact_ids is not None:
                result = await service.backfill_contacts(
                    explicit_contact_ids,
                    integration_id=int_id,
                )
            else:
                result = await service.hydrate_changed_sources(
                    integration_id=int_id,
                    changed_email_ids=email_ids,
                    changed_event_ids=event_ids,
                )
            email_deal_jobs: dict[UUID, UUID] = {}
            for affected_deal_id in sorted(result.email_deal_ids, key=str):
                memory_job_id, _created = await get_or_create_deal_memory_job(
                    org_session,
                    org_id,
                    affected_deal_id,
                    "communication_association",
                    "email_associated",
                )
                email_deal_jobs[affected_deal_id] = memory_job_id
            progress_snapshot = None
            if initial_sync_id and initial_sync_branch and initial_sync_chunk_key:
                # Creation flags exist only in this transaction. Stage progress before the
                # commit so a crash cannot commit CRM rows while losing their exact counts.
                progress_snapshot = (
                    await InitialSyncProgressService.complete_hydration_chunk_in_session(
                        org_session,
                        org_id,
                        UUID(initial_sync_id),
                        initial_sync_branch,
                        initial_sync_chunk_key,
                        contacts_created=result.contacts_created,
                        accounts_created=result.accounts_created,
                        emails_linked=result.emails_linked,
                    )
                )
            await org_session.commit()
            InitialSyncProgressService.publish_committed_snapshot(progress_snapshot)
            await _dispatch_association_followups(
                organisation_id=org_id,
                discovered_contact_ids=set(result.discovered_contact_ids),
                email_deal_jobs=email_deal_jobs,
                attachment_email_ids_by_deal={
                    deal_id: set(email_ids)
                    for deal_id, email_ids in result.attachment_email_ids_by_deal.items()
                },
            )
            response = {
                "status": "success",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "trigger": trigger,
                **result.to_dict(),
            }
        if tracker:
            await tracker.complete(
                response,
                "saving_communication_links",
                "Saving communication links",
            )
        return response

    try:
        return run_async(_run())
    except CommunicationHydrationBusyError as exc:
        # Lock contention is expected when two triggers target one organisation.
        # Retry with backoff so this worker can immediately process another org.
        if self.request.retries < HYDRATION_BUSY_MAX_RETRIES:
            countdown = min(60 * (2**self.request.retries), 300)
            logger.info(
                "Contact communication hydration deferred behind active organisation batch",
                extra={
                    "organisation_id": organisation_id,
                    "trigger": trigger,
                    "retry_countdown": countdown,
                },
            )
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=HYDRATION_BUSY_MAX_RETRIES,
            ) from exc
        if tracker:
            run_async(tracker.fail(str(exc)))
        if initial_sync_id and initial_sync_branch and initial_sync_chunk_key:
            run_async(
                InitialSyncProgressService.fail_hydration_chunk(
                    org_id,
                    UUID(initial_sync_id),
                    initial_sync_branch,
                    initial_sync_chunk_key,
                    error=InitialSyncErrorCode.HYDRATION_FAILED,
                )
            )
        raise
    except Exception as exc:
        logger.exception(
            "Contact communication hydration failed",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "trigger": trigger,
            },
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        if tracker:
            run_async(tracker.fail(str(exc)))
        if initial_sync_id and initial_sync_branch and initial_sync_chunk_key:
            run_async(
                InitialSyncProgressService.fail_hydration_chunk(
                    org_id,
                    UUID(initial_sync_id),
                    initial_sync_branch,
                    initial_sync_chunk_key,
                    error=InitialSyncErrorCode.HYDRATION_FAILED,
                )
            )
        raise


async def _dispatch_association_followups(
    *,
    organisation_id: UUID,
    discovered_contact_ids: set[UUID],
    email_deal_jobs: dict[UUID, UUID],
    attachment_email_ids_by_deal: dict[UUID, set[UUID]],
) -> None:
    """Run idempotent downstream work only after association rows commit."""
    for contact_id in sorted(discovered_contact_ids, key=str):
        try:
            await dispatch_contact_research_for_contact(
                organisation_id=organisation_id,
                contact_id=contact_id,
                trigger="communication_participant_discovery",
            )
        except Exception:
            logger.warning(
                "Failed to dispatch research after participant discovery",
                extra={
                    "organisation_id": str(organisation_id),
                    "contact_id": str(contact_id),
                },
                exc_info=True,
            )

    for deal_id, memory_job_id in sorted(email_deal_jobs.items(), key=lambda item: str(item[0])):
        attachment_email_ids = sorted(
            attachment_email_ids_by_deal.get(deal_id, set()),
            key=str,
        )
        for offset in range(0, len(attachment_email_ids), ATTACHMENT_EMAIL_CHUNK_SIZE):
            try:
                await dispatch_email_attachment_extraction_async(
                    organisation_id=organisation_id,
                    deal_id=deal_id,
                    email_ids=attachment_email_ids[offset : offset + ATTACHMENT_EMAIL_CHUNK_SIZE],
                    trigger="communication_association",
                )
            except Exception:
                logger.warning(
                    "Failed to dispatch email attachment extraction after association",
                    extra={
                        "organisation_id": str(organisation_id),
                        "deal_id": str(deal_id),
                    },
                    exc_info=True,
                )
        try:
            dispatch_deal_insight_pipeline(
                str(organisation_id),
                str(deal_id),
                str(memory_job_id),
            )
        except Exception:
            logger.warning(
                "Failed to dispatch deal insights after email association",
                extra={
                    "organisation_id": str(organisation_id),
                    "deal_id": str(deal_id),
                },
                exc_info=True,
            )


def _parse_uuids(values: list[str] | None) -> list[UUID]:
    """Convert Celery's string payload into UUIDs before the service builds SQL filters.

    Invalid values fail the task so Celery exposes malformed dispatch data instead of silently
    omitting a requested record.
    """
    return [UUID(value) for value in values or []]
