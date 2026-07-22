"""Durable webhook-to-worker handoff using the existing sync-history table.

Google and Microsoft callbacks call this module before acknowledging delivery.
The history row is committed before queue publication, so a broker failure can
return 503 and let the provider retry without silently losing the change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationSyncHistory, SyncStatus
from src.infra.database.sessions import acquire_org_async_session


def _now_naive() -> datetime:
    """Return UTC in the legacy naive shape used by sync-history timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def is_terminal_webhook_result(error: Any) -> bool:
    """Return whether a failed result requires configuration or user action.

    Some sync services return structured failures instead of raising typed
    exceptions. These states cannot recover through queue replay and should be
    closed until the integration is enabled, connected, or recreated.
    """
    message = str(error or "").lower()
    return any(
        marker in message
        for marker in (
            "not found",
            "no longer exists",
            "not active",
            "inactive",
            "not enabled",
            "not connected",
            "unknown calendar provider",
        )
    )


async def create_webhook_sync_receipt(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    metadata: dict[str, Any],
) -> UUID:
    """Commit a queued sync-history row before the callback publishes work."""
    async for session in acquire_org_async_session(organisation_id):
        # The integration id came from a public callback. The org-scoped session
        # is the tenant boundary; a provider id alone must never select a row.
        receipt = IntegrationSyncHistory(
            integration_id=integration_id,
            started_at=_now_naive(),
            status=SyncStatus.QUEUED,
            sync_metadata={"trigger_source": "webhook", **metadata},
        )
        session.add(receipt)
        await session.commit()
        await session.refresh(receipt)
        return receipt.sync_id
    raise RuntimeError("Organisation database session unavailable")


async def update_webhook_sync_receipt(
    *,
    organisation_id: UUID,
    sync_id: UUID,
    status: SyncStatus | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
    completed: bool = False,
) -> bool:
    """Merge progress into a receipt without regressing a completed delivery.

    Publishers use a metadata-only update after Celery accepts a task because a
    fast worker may already have completed the receipt. Worker status updates
    take a row lock so concurrent duplicate tasks cannot replace a successful
    outcome with an older failure or queue state.

    Returns ``True`` when the row exists and the update was applied.
    """
    async for session in acquire_org_async_session(organisation_id):
        result = await session.execute(
            select(IntegrationSyncHistory)
            .where(col(IntegrationSyncHistory.sync_id) == sync_id)
            .with_for_update()
        )
        receipt = result.scalar_one_or_none()
        if receipt is None:
            return False

        # SUCCESS is monotonic. Webhook redelivery can run an older duplicate
        # after the successful worker, but that duplicate must not reopen work.
        if receipt.status == SyncStatus.SUCCESS and status not in {None, SyncStatus.SUCCESS}:
            return False

        # A terminal action-required failure is intentionally closed until the
        # user reconnects; the watchdog must not turn it back into queued work.
        existing_metadata = dict(receipt.sync_metadata or {})
        if existing_metadata.get("terminal") is True and status in {
            SyncStatus.QUEUED,
            SyncStatus.IN_PROGRESS,
        }:
            return False

        if status is not None:
            receipt.status = status
        receipt.sync_metadata = {**dict(receipt.sync_metadata or {}), **(metadata or {})}
        if error is not None:
            # Provider SDK exceptions can be very large. Keep enough context for
            # operators without allowing one failure to inflate the JSONB row.
            receipt.error_details = {"error": error[:4000]}
        elif status == SyncStatus.SUCCESS:
            receipt.error_details = None
        if completed:
            receipt.completed_at = _now_naive()
        elif status is not None and status in {SyncStatus.QUEUED, SyncStatus.IN_PROGRESS}:
            receipt.completed_at = None
        session.add(receipt)
        await session.commit()
        return True
    return False
