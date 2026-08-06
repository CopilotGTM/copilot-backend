"""Celery activation task for directly delivered automation source events."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.service.automation.events.access import AutomationEventDispatchAccess
from src.service.automation.triggers.schedule import ScheduleDispatchService
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services.automation_event_activation_service import (
    AutomationEventActivationExecutionService,
)

logger = logging.getLogger(__name__)

# Recover only rows older than the normal publish/activation handoff window;
# the cap keeps one tenant's backlog from monopolizing the recovery worker.
AUTOMATION_EVENT_RECOVERY_AGE = timedelta(minutes=2)
AUTOMATION_EVENT_RECOVERY_LIMIT = 100


async def _active_organisation_ids() -> list[UUID]:
    organisation_ids: list[UUID] = []
    async for session in acquire_master_async_session():
        rows = await session.execute(
            select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED,
                col(Organisation.is_active).is_(True),
            )
        )
        organisation_ids = list(rows.scalars().all())
    return organisation_ids


@app.task(
    name="chat.activate_automation_event",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def activate_automation_event_task(
    self: Task, organisation_id: str, event_id: str
) -> dict[str, Any]:
    """Activate one event; database idempotency makes redelivery safe."""

    try:
        org_uuid = UUID(organisation_id)
        event_uuid = UUID(event_id)
    except ValueError:
        logger.warning("Discarding automation activation task with invalid UUID payload")
        return {"status": "invalid_payload"}

    result = run_async(
        AutomationEventActivationExecutionService(org_uuid).activate(
            event_uuid,
            retry_allowed=self.request.retries < self.max_retries,
        )
    )
    if result.get("status") == "retry":
        raise self.retry(countdown=10 * (2**self.request.retries))
    return result


@app.task(
    name="chat.recover_automation_events_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def recover_automation_events_all_orgs_task() -> dict[str, Any]:
    """Tier 1: fan out pending-dispatch recovery for approved active tenants."""

    async def dispatch() -> dict[str, Any]:
        """Dispatch the current automation event tasks work through the organisation-scoped queue boundary."""
        organisation_ids = await _active_organisation_ids()
        dispatched = 0
        for organisation_id in organisation_ids:
            try:
                recover_automation_events_org_task.apply_async(
                    kwargs={"organisation_id": str(organisation_id)},
                    queue="chat_processing",
                    routing_key="chat_processing",
                    headers=correlation_headers(),
                )
                dispatched += 1
            except Exception:
                logger.exception(
                    "Failed to dispatch automation event recovery org=%s",
                    organisation_id,
                )
        return {
            "status": "completed",
            "orgs_found": len(organisation_ids),
            "orgs_dispatched": dispatched,
        }

    return run_async(dispatch())


@app.task(
    name="chat.recover_automation_events_org",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def recover_automation_events_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: republish a bounded tenant batch; activation remains idempotent."""

    try:
        organisation_uuid = UUID(organisation_id)
    except ValueError:
        return {"status": "invalid_payload"}

    async def load_pending() -> list[UUID]:
        """Load pending for the current organisation-scoped operation."""
        async for session in acquire_org_async_session(organisation_uuid):
            return await AutomationEventDispatchAccess(
                session,
                organisation_uuid,
            ).list_pending_before(
                datetime.now(UTC) - AUTOMATION_EVENT_RECOVERY_AGE,
                limit=AUTOMATION_EVENT_RECOVERY_LIMIT,
            )
        raise RuntimeError("organization session scope yielded no session")

    try:
        event_ids = run_async(load_pending())
    except Exception as exc:
        logger.exception("Automation event recovery failed org=%s", organisation_uuid)
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=30 * (2**self.request.retries),
            ) from exc
        return {"status": "failed", "organisation_id": organisation_id}

    republished = 0
    failures = 0
    for event_id in event_ids:
        try:
            activate_automation_event_task.apply_async(
                kwargs={
                    "organisation_id": organisation_id,
                    "event_id": str(event_id),
                },
                queue="chat_processing",
                routing_key="chat_processing",
                headers=correlation_headers(),
            )
            republished += 1
        except Exception:
            failures += 1
            logger.exception(
                "Failed to republish automation event org=%s event=%s",
                organisation_uuid,
                event_id,
            )
    return {
        "status": "completed",
        "organisation_id": organisation_id,
        "pending_count": len(event_ids),
        "republished_count": republished,
        "publish_failures": failures,
    }


@app.task(
    name="chat.dispatch_due_automation_schedules_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def dispatch_due_automation_schedules_all_orgs_task() -> dict[str, Any]:
    """Tier 1: fan out one non-retrying task per active approved tenant."""

    async def dispatch() -> dict[str, Any]:
        """Dispatch the current automation event tasks work through the organisation-scoped queue boundary."""
        organisation_ids = await _active_organisation_ids()

        dispatched = 0
        for organisation_id in organisation_ids:
            try:
                dispatch_due_automation_schedules_org_task.apply_async(
                    kwargs={"organisation_id": str(organisation_id)},
                    queue="chat_processing",
                    routing_key="chat_processing",
                    headers=correlation_headers(),
                )
                dispatched += 1
            except Exception:
                logger.exception(
                    "Failed to dispatch due schedules for org=%s",
                    organisation_id,
                )
        return {
            "status": "completed",
            "orgs_found": len(organisation_ids),
            "orgs_dispatched": dispatched,
        }

    return run_async(dispatch())


@app.task(
    name="chat.dispatch_due_automation_schedules_org",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def dispatch_due_automation_schedules_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: lock, enqueue, and advance one tenant's bounded due batch."""

    try:
        organisation_uuid = UUID(organisation_id)
    except ValueError:
        return {"status": "invalid_payload"}

    async def dispatch() -> dict[str, Any]:
        """Dispatch the current automation event tasks work through the organisation-scoped queue boundary."""
        async for session in acquire_org_async_session(organisation_uuid):
            result = await ScheduleDispatchService(session, organisation_uuid).dispatch_due(
                now=datetime.now(UTC)
            )
            await session.commit()
            return result.as_dict()
        raise RuntimeError("organization session scope yielded no session")

    try:
        return run_async(dispatch())
    except Exception as exc:
        logger.exception("Due schedule dispatch failed org=%s", organisation_uuid)
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=30 * (2**self.request.retries),
            ) from exc
        return {"status": "failed", "organisation_id": organisation_id}
