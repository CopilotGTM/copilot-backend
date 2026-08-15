"""Bounded delayed-work recovery hosted by the existing Chat worker pool.

Celery Beat publishes one sweep every five minutes. The sweep walks approved
organisations under their RLS sessions and spends one global recovery budget;
it never publishes a second task merely to discover whether an org has work.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import (
    Organisation,
    OrganisationStatus,
)
from src.infra.database.sessions import acquire_master_async_session
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services.stale_job_cleanup_service import (
    DEFAULT_STALE_RECOVERY_LIMIT,
    MAX_STALE_RECOVERY_LIMIT,
    ChatStaleJobCleanupService,
)

logger = get_logger("chat_stale_job_cleanup_tasks")

_CLEANUP_LOCK_KEY = "task_lock:chat_stale_cleanup_all_orgs"
_CLEANUP_CURSOR_KEY = "chat:stale_cleanup:last_organisation"
_CLEANUP_LOCK_TTL_SECONDS = 300
_CLEANUP_CURSOR_TTL_SECONDS = 7 * 24 * 60 * 60


def load_cleanup_cursor() -> UUID | None:
    """Return the last examined tenant so a busy early tenant cannot starve later ones."""
    try:
        value = get_redis_client().get(_CLEANUP_CURSOR_KEY)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        return UUID(str(value))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid stale-cleanup cursor", exc_info=True)
    except Exception:
        # Cursor loss affects fairness for one sweep, not recovery correctness.
        logger.warning("Unable to load stale-cleanup cursor", exc_info=True)
    return None


def save_cleanup_cursor(organisation_id: UUID) -> None:
    """Persist the last examined tenant; recovery remains safe if this best-effort write fails."""
    try:
        get_redis_client().set(
            _CLEANUP_CURSOR_KEY,
            str(organisation_id),
            ex=_CLEANUP_CURSOR_TTL_SECONDS,
        )
    except Exception:
        logger.warning("Unable to save stale-cleanup cursor", exc_info=True)


def _rotate_after_cursor(organisation_ids: list[UUID], cursor: UUID | None) -> list[UUID]:
    """Start after the previous tenant while preserving stable master-table ordering."""
    if cursor not in organisation_ids:
        return organisation_ids
    start = organisation_ids.index(cursor) + 1
    return organisation_ids[start:] + organisation_ids[:start]


async def _approved_organisation_ids() -> list[UUID]:
    """Load the stable tenant order from the public control-plane table."""
    async for master_session in acquire_master_async_session():
        statement = (
            select(col(Organisation.organisation_id))
            .where(col(Organisation.status) == OrganisationStatus.APPROVED)
            .order_by(col(Organisation.organisation_id))
        )
        result = await master_session.execute(statement)
        return [row[0] for row in result.all()]
    return []


async def _run_cleanup_sweep(limit: int) -> dict[str, Any]:
    """Process stale rows sequentially across tenants until the global budget is spent."""
    budget = min(max(limit, 0), MAX_STALE_RECOVERY_LIMIT)
    organisation_ids = _rotate_after_cursor(
        await _approved_organisation_ids(),
        load_cleanup_cursor(),
    )
    processed_count = 0
    organisations_scanned = 0
    last_examined: UUID | None = None
    failed_organisations = 0

    for organisation_id in organisation_ids:
        remaining = budget - processed_count
        if remaining <= 0:
            break

        organisations_scanned += 1
        last_examined = organisation_id
        try:
            # The service opens only this tenant's RLS session and applies the
            # remaining global budget at every database query boundary.
            result = await ChatStaleJobCleanupService(organisation_id).cleanup_stale_jobs_async(
                limit=remaining
            )
            processed_count += min(int(result.get("processed_count", 0)), remaining)
        except Exception:
            # A broken tenant is retried on a later sweep without blocking the
            # cursor from giving the remaining organisations a turn.
            failed_organisations += 1
            logger.exception("Delayed-work cleanup failed org=%s", organisation_id)

    if last_examined is not None:
        save_cleanup_cursor(last_examined)

    return {
        "status": "completed",
        "orgs_found": len(organisation_ids),
        "orgs_scanned": organisations_scanned,
        "failed_organisations": failed_organisations,
        "processed_count": processed_count,
        "budget": budget,
    }


@app.task(
    name="chat.cleanup_stale_jobs_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    ignore_result=True,
    soft_time_limit=240,
    time_limit=270,
)
def cleanup_delayed_work_all_orgs_task(
    limit: int = DEFAULT_STALE_RECOVERY_LIMIT,
) -> dict[str, Any]:
    """Run one non-overlapping, globally bounded stale-work sweep."""
    if not acquire_task_lock(_CLEANUP_LOCK_KEY, ttl=_CLEANUP_LOCK_TTL_SECONDS):
        return {"status": "skipped", "reason": "already_running"}

    try:
        return run_async(_run_cleanup_sweep(limit))
    finally:
        release_task_lock(_CLEANUP_LOCK_KEY)
