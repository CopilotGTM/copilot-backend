"""Celery entrypoint for local contact creation and communication backfill."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task

from src.infra.database.sessions import master_async_session_scope, org_async_session_scope
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.discovery.contact_communication_hydration import (
    ContactCommunicationHydrationService,
)
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


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
    trigger: str = "communication_sync",
) -> dict[str, Any]:
    """Run one retry-safe local hydration batch without provider research calls."""
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id) if integration_id else None
    email_ids = _parse_uuids(changed_email_ids)
    event_ids = _parse_uuids(changed_event_ids)
    explicit_contact_ids = _parse_uuids(contact_ids)
    if contact_ids is not None and (changed_email_ids is not None or changed_event_ids is not None):
        raise ValueError("contact backfill and changed-source hydration cannot be mixed")
    if contact_ids is None and changed_email_ids is None and changed_event_ids is None:
        raise ValueError("a contact or changed-source payload is required")

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
            if contact_ids is not None:
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
            await org_session.commit()
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


def _parse_uuids(values: list[str] | None) -> list[UUID]:
    """Convert Celery's string payload into UUIDs before the service builds SQL filters.

    Invalid values fail the task so Celery exposes malformed dispatch data instead of silently
    omitting a requested record.
    """
    return [UUID(value) for value in values or []]
