"""Celery entrypoint for local contact creation and communication backfill."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task

from src.infra.database.sessions import master_async_session_scope, org_async_session_scope
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.discovery.contact_communication_hydration import (
    ContactCommunicationHydrationService,
)
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.tasks.deal_asset_email_attachment_tasks import (
    extract_email_attachments_deal_task,
)
from src.workers.deal_worker.utils.contact_research_dispatch import (
    dispatch_contact_research_for_contact,
)
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline,
    get_or_create_deal_memory_job,
)

logger = get_logger(__name__)

# Broker retries cover the short outage window without delaying the committed hydration task.
ATTACHMENT_PUBLISH_RETRY_POLICY = {
    "max_retries": 3,
    "interval_start": 0,
    "interval_step": 1,
    "interval_max": 2,
}


# Two delayed retries cover transient database failures without looping indefinitely. The hard
# limit gives a worker 60 seconds after the ten-minute soft limit to unwind the batch cleanly.
# Batches are bounded by the dispatcher, so healthy work should finish inside this window.
@app.task(
    bind=True,
    name="deal.hydrate_contact_communications",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
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
) -> dict[str, Any]:
    """Run one retry-safe local hydration batch without provider research calls."""
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

    async def _run() -> dict[str, Any]:
        """Open both tenant sessions, commit the projection, and return worker-safe metrics.

        The master session identifies internal users while the org session owns all CRM writes.
        """
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
            await org_session.commit()
            await _dispatch_association_followups(
                organisation_id=org_id,
                discovered_contact_ids=set(result.discovered_contact_ids),
                email_deal_jobs=email_deal_jobs,
                attachment_deal_ids=set(result.attachment_deal_ids),
            )
            return {
                "status": "success",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "trigger": trigger,
                **result.to_dict(),
            }

    try:
        return run_async(_run())
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
        raise


async def _dispatch_association_followups(
    *,
    organisation_id: UUID,
    discovered_contact_ids: set[UUID],
    email_deal_jobs: dict[UUID, UUID],
    attachment_deal_ids: set[UUID],
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

    attachment_publish_error: Exception | None = None
    for deal_id, memory_job_id in sorted(email_deal_jobs.items(), key=lambda item: str(item[0])):
        if deal_id in attachment_deal_ids:
            try:
                extract_email_attachments_deal_task.apply_async(
                    kwargs={
                        "organisation_id": str(organisation_id),
                        "deal_id": str(deal_id),
                    },
                    headers=correlation_headers(),
                    retry=True,
                    retry_policy=ATTACHMENT_PUBLISH_RETRY_POLICY,
                )
            except Exception as exc:
                attachment_publish_error = attachment_publish_error or exc
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
    if attachment_publish_error is not None:
        raise attachment_publish_error


def _parse_uuids(values: list[str] | None) -> list[UUID]:
    """Convert Celery's string payload into UUIDs before the service builds SQL filters.

    Invalid values fail the task so Celery exposes malformed dispatch data instead of silently
    omitting a requested record.
    """
    return [UUID(value) for value in values or []]
