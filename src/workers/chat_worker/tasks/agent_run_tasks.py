"""Celery execution and durable recovery tasks for user-agent runs.

The execution and delivery tasks receive only tenant plus run or delivery
identity and delegate all state transitions to their services. Recovery
republishes durable pending or lease-expired rows, closing each commit/publish
gap.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.observability.metrics import (
    record_agent_run_delivery_recovery,
    record_agent_run_delivery_recovery_age,
)
from src.infra.utils.async_runner import run_async
from src.service.automation.runs.access import AgentRunClaimAccess
from src.service.automation.runs.delivery_access import AgentRunDeliveryAccess
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.client import send_chat_task_async
from src.workers.chat_worker.services.agent_run_delivery_service import (
    AgentRunDeliveryExecutionService,
    _delivery_retry_delay_seconds,
)
from src.workers.chat_worker.services.agent_run_execution_service import (
    AgentRunExecutionService,
)

logger = logging.getLogger(__name__)

# copilotgtm: V1 scans approved orgs in small batches every 30 seconds. Move to
# partitioned wakeups only when measured run volume makes the global sweep costly.
RECOVERY_BATCH_SIZE = 100
RECOVERY_LEASE = timedelta(minutes=30)
# Delivery leases are shorter than model-run leases because provider sends are
# expected to finish quickly and should be reclaimed promptly after a crash.
DELIVERY_RECOVERY_LEASE = timedelta(minutes=10)


@app.task(
    name="chat.execute_agent_run",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=540,
    time_limit=600,
)
def execute_agent_run_task(
    self: Task,
    organisation_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Execute one durable run; duplicate deliveries are safe no-ops.

    The database claim is the idempotency boundary, and each completion repeats
    the attempt fence. Celery retries only after the service releases the row
    back to pending, so a retry can claim it normally.
    """

    try:
        org_uuid = UUID(organisation_id)
        run_uuid = UUID(run_id)
    except ValueError:
        logger.warning("Discarding agent run task with invalid UUID payload")
        return {"status": "invalid_payload"}

    worker_id = (
        f"{getattr(self.request, 'hostname', None) or 'chat-worker'}:"
        f"{getattr(self.request, 'id', None) or 'direct'}"
    )
    retry_allowed = self.request.retries < self.max_retries
    try:
        result = cast(
            dict[str, Any],
            run_async(
                AgentRunExecutionService(org_uuid).execute(
                    run_uuid,
                    worker_id=worker_id,
                    retry_allowed=retry_allowed,
                )
            ),
        )
    except Exception as exc:
        logger.exception("Agent run task failed before a durable outcome")
        if retry_allowed:
            countdown = 30 * (2**self.request.retries)
            raise self.retry(exc=exc, countdown=countdown) from exc
        return {"status": "failed", "run_id": run_id}

    if result.get("status") == "retry":
        countdown = 30 * (2**self.request.retries)
        raise self.retry(countdown=countdown)
    return result


@app.task(
    name="chat.deliver_agent_run",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def deliver_agent_run_task(
    self: Task,
    organisation_id: str,
    delivery_id: str,
) -> dict[str, Any]:
    """Deliver one persisted result without executing the agent again.

    The ledger claim token makes duplicate Celery messages harmless. Retry only
    classified provider failures; an unknown provider outcome is terminal until
    a human or explicit repair flow confirms it is safe.
    """

    try:
        org_uuid = UUID(organisation_id)
        delivery_uuid = UUID(delivery_id)
    except ValueError:
        logger.warning("Discarding agent delivery task with invalid UUID payload")
        return {"status": "invalid_payload"}

    worker_id = (
        f"{getattr(self.request, 'hostname', None) or 'chat-worker'}:"
        f"{getattr(self.request, 'id', None) or 'direct'}"
    )
    retry_allowed = self.request.retries < self.max_retries
    try:
        result = cast(
            dict[str, Any],
            run_async(
                AgentRunDeliveryExecutionService(org_uuid).execute(
                    delivery_uuid,
                    worker_id=worker_id,
                    retry_allowed=retry_allowed,
                )
            ),
        )
    except Exception as exc:
        logger.exception("Agent delivery task failed before a durable outcome")
        if retry_allowed:
            countdown = _delivery_retry_delay_seconds(self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from exc
        return {"status": "failed", "delivery_id": delivery_id}

    if result.get("status") == "retry":
        # The service has already persisted the next-attempt timestamp; Celery's
        # retry keeps prompt delivery while the recovery sweep covers lost messages.
        countdown = _delivery_retry_delay_seconds(self.request.retries + 1)
        raise self.retry(countdown=countdown)
    return result


@app.task(
    name="chat.recover_agent_runs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def recover_agent_runs_task() -> dict[str, Any]:
    """Republish durable pending and lease-expired runs across approved orgs."""

    return cast(dict[str, Any], run_async(_recover_agent_runs()))


async def _recover_agent_runs() -> dict[str, Any]:
    org_ids: list[UUID] = []
    async for session in acquire_master_async_session():
        rows = await session.execute(
            select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
        )
        org_ids = list(rows.scalars().all())

    stale_before = datetime.now(UTC) - RECOVERY_LEASE
    dispatched = 0
    failed = 0
    for organisation_id in org_ids:
        run_ids: list[UUID] = []
        try:
            # Read only the bounded durable page, then let the session scope close
            # before broker publication so recovery does not hold a DB connection.
            async for session in acquire_org_async_session(organisation_id):
                run_ids = await AgentRunClaimAccess(
                    session,
                    organisation_id,
                ).list_dispatchable_run_ids(
                    stale_before=stale_before,
                    limit=RECOVERY_BATCH_SIZE,
                )
        except Exception:
            logger.exception("Agent run recovery scan failed org=%s", organisation_id)
            failed += 1
            continue

        for run_id in run_ids:
            try:
                # Publication may duplicate an existing message. The atomic
                # claim turns duplicate deliveries into harmless no-ops.
                await send_chat_task_async(
                    "chat.execute_agent_run",
                    kwargs={
                        "organisation_id": str(organisation_id),
                        "run_id": str(run_id),
                    },
                    queue="chat_messages",
                    routing_key="chat_messages",
                )
                dispatched += 1
            except Exception:
                # The durable row remains dispatchable for the next sweep.
                logger.exception(
                    "Agent run recovery publish failed org=%s run=%s",
                    organisation_id,
                    run_id,
                )
                failed += 1

    return {
        "status": "completed",
        "organisations_scanned": len(org_ids),
        "dispatched": dispatched,
        "failed": failed,
    }


@app.task(
    name="chat.recover_agent_run_deliveries",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def recover_agent_run_deliveries_task() -> dict[str, Any]:
    """Republish pending and lease-expired output deliveries."""

    return cast(dict[str, Any], run_async(_recover_agent_run_deliveries()))


async def _recover_agent_run_deliveries() -> dict[str, Any]:
    org_ids: list[UUID] = []
    async for session in acquire_master_async_session():
        rows = await session.execute(
            select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
        )
        org_ids = list(rows.scalars().all())

    now = datetime.now(UTC)
    stale_before = now - DELIVERY_RECOVERY_LEASE
    dispatched = 0
    failed = 0
    reclaimed = 0
    skipped = 0
    for organisation_id in org_ids:
        candidates = []
        try:
            # Scan under the tenant session, then publish after the scope closes;
            # a failed publish leaves the row eligible for the next sweep.
            async for session in acquire_org_async_session(organisation_id):
                candidates = await AgentRunDeliveryAccess(
                    session,
                    organisation_id,
                ).list_dispatchable(
                    stale_before=stale_before,
                    now=now,
                    limit=RECOVERY_BATCH_SIZE,
                )
        except Exception:
            logger.exception("Agent delivery recovery scan failed org=%s", organisation_id)
            failed += 1
            continue

        record_agent_run_delivery_recovery("scanned", rows=len(candidates))
        for candidate in candidates:
            age_from = (
                candidate.claimed_at
                if candidate.status == "sending" and candidate.claimed_at is not None
                else candidate.created_at
            )
            record_agent_run_delivery_recovery_age(
                candidate.status,
                _elapsed_seconds(now, age_from),
            )
            if candidate.status == "sending":
                # A stale sending lease is eligible for another claim; the new
                # worker's token will fence the old process if it wakes later.
                reclaimed += 1
                record_agent_run_delivery_recovery("reclaimed")
            try:
                await send_chat_task_async(
                    "chat.deliver_agent_run",
                    kwargs={
                        "organisation_id": str(organisation_id),
                        "delivery_id": str(candidate.delivery_id),
                    },
                    queue="chat_messages",
                    routing_key="chat_messages",
                )
                dispatched += 1
                record_agent_run_delivery_recovery("dispatched")
            except Exception:
                logger.exception(
                    "Agent delivery recovery publish failed org=%s run=%s",
                    organisation_id,
                    candidate.run_id,
                )
                failed += 1
                skipped += 1
                record_agent_run_delivery_recovery("skipped")

    return {
        "status": "completed",
        "organisations_scanned": len(org_ids),
        "dispatched": dispatched,
        "failed": failed,
        "reclaimed": reclaimed,
        "skipped": skipped,
    }


def _elapsed_seconds(now: datetime, started_at: datetime) -> float:
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return max((now - started_at).total_seconds(), 0.0)
