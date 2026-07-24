"""Celery execution and durable recovery tasks for user-agent runs.

`execute_agent_run` receives only tenant/run identity and delegates all state
transitions to the execution service. `recover_agent_runs` republishes durable
pending or lease-expired rows, closing the database-commit/broker-publish gap.
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
from src.infra.utils.async_runner import run_async
from src.service.automation.runs.access import AgentRunClaimAccess
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.client import send_chat_task_async
from src.workers.chat_worker.services.agent_run_execution_service import (
    AgentRunExecutionService,
)

logger = logging.getLogger(__name__)

# copilotgtm: V1 scans approved orgs in small batches every 30 seconds. Move to
# partitioned wakeups only when measured run volume makes the global sweep costly.
RECOVERY_BATCH_SIZE = 100
RECOVERY_LEASE = timedelta(minutes=30)


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
