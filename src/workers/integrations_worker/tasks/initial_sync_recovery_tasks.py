"""Bounded recovery task for initial sync runs abandoned by worker failure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationSyncHistory, SyncStatus
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.integrations.initial_sync_progress_service import (
    INITIAL_SYNC_KIND,
    InitialSyncProgressService,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

RECOVER_TASK = "integrations.recover_stale_initial_syncs"
_LIMITS = TASK_LIMITS[RECOVER_TASK]
_STALE_AFTER = timedelta(hours=2)
_BATCH_SIZE = 100
logger = get_logger(__name__)


async def _stale_sync_ids(organisation_id: UUID, limit: int) -> list[UUID]:
    stale_before = datetime.now(UTC).replace(tzinfo=None) - _STALE_AFTER
    async for session in acquire_org_async_session(organisation_id):
        statement = (
            select(col(IntegrationSyncHistory.sync_id))
            .where(
                col(IntegrationSyncHistory.organisation_id) == organisation_id,
                col(IntegrationSyncHistory.status).in_([SyncStatus.QUEUED, SyncStatus.IN_PROGRESS]),
                col(IntegrationSyncHistory.updated_at) <= stale_before,
                col(IntegrationSyncHistory.completed_at).is_(None),
                col(IntegrationSyncHistory.sync_metadata)["kind"].as_string() == INITIAL_SYNC_KIND,
            )
            .order_by(col(IntegrationSyncHistory.updated_at))
            .limit(limit)
        )
        return list((await session.execute(statement)).scalars().all())
    return []


@app.task(
    name=RECOVER_TASK,
    bind=False,
    max_retries=_LIMITS["retries"],
    acks_late=True,
    soft_time_limit=_LIMITS["soft"],
    time_limit=_LIMITS["hard"],
)
def recover_stale_initial_syncs_task() -> dict[str, Any]:
    """Terminalize stale runs across active tenants under one global task lock."""
    lock_key = "task_lock:recover_stale_initial_syncs"
    if not acquire_task_lock(lock_key, ttl=_LIMITS["hard"]):
        return {"status": "skipped", "reason": "already_running"}

    async def _run() -> dict[str, Any]:
        async for session in acquire_master_async_session():
            statement = select(col(Organisation.organisation_id)).where(
                col(Organisation.is_active).is_(True),
                col(Organisation.status) == OrganisationStatus.APPROVED,
            )
            organisation_ids = list((await session.execute(statement)).scalars().all())
            break
        else:
            organisation_ids = []

        recovered = 0
        for organisation_id in organisation_ids:
            if recovered >= _BATCH_SIZE:
                break
            try:
                sync_ids = await _stale_sync_ids(organisation_id, _BATCH_SIZE - recovered)
                for sync_id in sync_ids:
                    await InitialSyncProgressService.terminalize_stale_run(organisation_id, sync_id)
                    recovered += 1
            except Exception:
                logger.exception(
                    "Initial sync recovery failed for organisation",
                    extra={"organisation_id": str(organisation_id)},
                )
        return {"status": "success", "terminalized": recovered}

    try:
        return run_async(_run())
    finally:
        release_task_lock(lock_key)


__all__ = ["RECOVER_TASK", "recover_stale_initial_syncs_task"]
