"""Recover webhook work that never reached a successful worker completion."""

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
from src.service.integrations.webhooks.sync_receipt import update_webhook_sync_receipt
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task_async
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

RECOVER_TASK = "integrations.recover_stale_webhook_syncs"
_LIMITS = TASK_LIMITS[RECOVER_TASK]
# Five minutes is the recovery SLO: normal webhook work starts in seconds, so
# older receipts are safe to replay through cursor-based idempotent syncs.
_STALE_AFTER = timedelta(minutes=5)
# Stop after six watchdog replays and emit an operator-visible error; auth or
# scope failures need reconnect rather than infinite queue churn.
_MAX_RECOVERY_ATTEMPTS = 6
# Bound both tenant and coordinator work so the five-minute CronJob cannot grow
# without limit as organisation count increases.
_BATCH_SIZE = 100
_TOTAL_BATCH_SIZE = 500


def _task_for_receipt(metadata: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Resolve the existing worker path encoded in a durable webhook receipt."""
    provider = metadata.get("provider")
    capability = metadata.get("capability")
    if provider not in {"google", "microsoft"}:
        return None
    lifecycle_events = metadata.get("provider_lifecycle_events") or []
    recovery_event = next(
        (
            event
            for event in ("subscriptionRemoved", "reauthorizationRequired")
            if event in lifecycle_events
        ),
        None,
    )
    if provider == "microsoft" and capability in {"calendar", "email"} and recovery_event:
        return (
            "integrations.ensure_microsoft_graph_subscription",
            {
                "kind": capability,
                "lifecycle_event": recovery_event,
                "sync_after": True,
            },
        )
    if capability in {"calendar"}:
        return "integrations.sync_calendar_integration", {}
    if capability in {"gmail", "email"}:
        return "integrations.sync_email_integration", {}
    return None


def _recovery_attempts(metadata: dict[str, Any]) -> int:
    """Parse an untrusted JSONB attempt counter into a safe bounded integer."""
    try:
        attempts = int(metadata.get("recovery_attempts") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, attempts)


async def _recover_org_receipts(organisation_id: UUID) -> dict[str, int]:
    """Atomically lease and re-publish one bounded batch for an organisation."""
    stale_before = datetime.now(UTC) - _STALE_AFTER
    receipts: list[tuple[UUID, UUID, dict[str, Any], str, dict[str, Any]]] = []
    exhausted = 0
    skipped = 0
    async for session in acquire_org_async_session(organisation_id):
        # Row locks are the correctness boundary. The Redis coordinator lock is
        # allowed to fail open, so two watchdogs may reach this query together.
        statement = (
            select(IntegrationSyncHistory)
            .where(
                col(IntegrationSyncHistory.status).in_(
                    [SyncStatus.QUEUED, SyncStatus.IN_PROGRESS, SyncStatus.FAILED]
                ),
                col(IntegrationSyncHistory.updated_at) <= stale_before,
                col(IntegrationSyncHistory.completed_at).is_(None),
                col(IntegrationSyncHistory.sync_metadata)["trigger_source"].as_string()
                == "webhook",
            )
            .order_by(col(IntegrationSyncHistory.updated_at))
            .limit(_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(statement)
        for receipt in result.scalars().all():
            metadata = dict(receipt.sync_metadata or {})
            task_spec = _task_for_receipt(metadata)
            if task_spec is None:
                # Invalid internal metadata cannot become runnable later. Close
                # it once so it does not poison the oldest-row recovery window.
                receipt.status = SyncStatus.FAILED
                receipt.completed_at = datetime.now(UTC)
                receipt.sync_metadata = {
                    **metadata,
                    "terminal": True,
                    "failure_stage": "recovery_invalid_metadata",
                }
                session.add(receipt)
                skipped += 1
                continue

            attempts = _recovery_attempts(metadata)
            if attempts >= _MAX_RECOVERY_ATTEMPTS:
                # Persist exhaustion before alerting. Future watchdog runs exclude
                # completed rows, so operators receive one signal rather than a
                # duplicate error every five minutes.
                receipt.status = SyncStatus.FAILED
                receipt.completed_at = datetime.now(UTC)
                receipt.sync_metadata = {
                    **metadata,
                    "terminal": True,
                    "recovery_exhausted_at": datetime.now(UTC).isoformat(),
                }
                session.add(receipt)
                exhausted += 1
                logger.error(
                    "Webhook recovery attempts exhausted",
                    extra={
                        "event": "webhook_sync_recovery_exhausted",
                        "organisation_id": str(organisation_id),
                        "sync_id": str(receipt.sync_id),
                        "integration_id": str(receipt.integration_id),
                    },
                )
                continue

            task_name, extra_kwargs = task_spec
            receipt.status = SyncStatus.QUEUED
            receipt.completed_at = None
            receipt.sync_metadata = {
                **metadata,
                "recovery_attempts": attempts + 1,
                "last_recovery_at": datetime.now(UTC).isoformat(),
            }
            session.add(receipt)
            receipts.append(
                (
                    receipt.sync_id,
                    receipt.integration_id,
                    receipt.sync_metadata,
                    task_name,
                    extra_kwargs,
                )
            )

        # Commit every lease before publishing. A crash after this commit leaves
        # an incomplete queued row for the next watchdog rather than losing work.
        await session.commit()

    recovered = 0
    for sync_id, integration_id, _metadata, task_name, extra_kwargs in receipts:
        try:
            task_id = await send_integrations_task_async(
                task_name=task_name,
                kwargs={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "trigger_source": "webhook",
                    "webhook_sync_id": str(sync_id),
                    **extra_kwargs,
                },
                queue="integrations_priority",
                routing_key="integrations_priority",
            )
        except Exception as exc:
            await update_webhook_sync_receipt(
                organisation_id=organisation_id,
                sync_id=sync_id,
                status=SyncStatus.FAILED,
                metadata={"failure_stage": "recovery_queue_publish"},
                error=str(exc),
                completed=False,
            )
            logger.exception(
                "Failed to republish stale webhook receipt",
                extra={"organisation_id": str(organisation_id), "sync_id": str(sync_id)},
            )
            continue
        await update_webhook_sync_receipt(
            organisation_id=organisation_id,
            sync_id=sync_id,
            metadata={"task_id": task_id, "failure_stage": None},
        )
        recovered += 1

    return {
        "checked": len(receipts) + exhausted + skipped,
        "recovered": recovered,
        "exhausted": exhausted,
        "skipped": skipped,
    }


@app.task(
    name=RECOVER_TASK,
    bind=False,
    max_retries=_LIMITS["retries"],
    acks_late=True,
    soft_time_limit=_LIMITS["soft"],
    time_limit=_LIMITS["hard"],
)
def recover_stale_webhook_syncs_task() -> dict[str, Any]:
    """Recover stale webhook receipts across active approved organisations."""
    lock_key = "task_lock:recover_stale_webhook_syncs"
    if not acquire_task_lock(lock_key, ttl=_LIMITS["hard"]):
        return {"status": "skipped", "reason": "already_running"}

    async def _run() -> dict[str, Any]:
        organisation_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            result = await session.execute(
                select(col(Organisation.organisation_id)).where(
                    col(Organisation.is_active).is_(True),
                    col(Organisation.status) == OrganisationStatus.APPROVED,
                )
            )
            organisation_ids = list(result.scalars().all())

        totals = {"checked": 0, "recovered": 0, "exhausted": 0, "skipped": 0}
        for organisation_id in organisation_ids:
            if totals["checked"] >= _TOTAL_BATCH_SIZE:
                break
            try:
                counts = await _recover_org_receipts(organisation_id)
            except Exception:
                # Tenant databases fail independently. Preserve recovery for the
                # remaining organisations and leave this tenant for the next run.
                logger.exception(
                    "Webhook recovery failed for organisation",
                    extra={"organisation_id": str(organisation_id)},
                )
                continue
            for key, value in counts.items():
                totals[key] += value
        return {"status": "success", "organisations_checked": len(organisation_ids), **totals}

    try:
        return run_async(_run())
    finally:
        release_task_lock(lock_key)


__all__ = ["RECOVER_TASK", "recover_stale_webhook_syncs_task"]
