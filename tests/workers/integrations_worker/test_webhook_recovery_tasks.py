from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync import IntegrationSyncHistory, SyncStatus
from src.workers.integrations_worker.tasks.webhook_recovery_tasks import (
    RECOVER_TASK,
    _recover_org_receipts,
    _recovery_attempts,
    _task_for_receipt,
    recover_stale_webhook_syncs_task,
)


class _Rows:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _RecoverySession:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.statement = None
        self.commit = AsyncMock()

    async def execute(self, statement):
        self.statement = statement
        return _Rows(self.rows)

    def add(self, _row) -> None:
        return None


def test_recovery_task_is_registered_on_priority_schedule() -> None:
    """The watchdog must remain discoverable by the integrations worker."""
    assert recover_stale_webhook_syncs_task.name == RECOVER_TASK


def test_recovery_routes_missed_calendar_callback_to_delta_sync() -> None:
    """A missed lifecycle event needs data catch-up, not subscription replacement."""
    assert _task_for_receipt(
        {"provider": "microsoft", "capability": "calendar", "provider_lifecycle_events": ["missed"]}
    ) == ("integrations.sync_calendar_integration", {})


def test_recovery_repairs_removed_subscription_before_delta_sync() -> None:
    """A removed Graph subscription must be repaired before its gap-closing sync."""
    assert _task_for_receipt(
        {
            "provider": "microsoft",
            "capability": "email",
            "provider_lifecycle_events": ["subscriptionRemoved"],
        }
    ) == (
        "integrations.ensure_microsoft_graph_subscription",
        {"kind": "email", "lifecycle_event": "subscriptionRemoved", "sync_after": True},
    )


def test_malformed_recovery_attempt_counter_is_safe() -> None:
    """A malformed JSONB counter cannot abort recovery for later tenants."""
    assert _recovery_attempts({"recovery_attempts": {"unexpected": "shape"}}) == 0


@pytest.mark.asyncio
async def test_recovery_claim_uses_database_row_lock(monkeypatch) -> None:
    """Concurrent watchdogs must skip a receipt already leased by another run."""
    from src.workers.integrations_worker.tasks import webhook_recovery_tasks

    receipt = IntegrationSyncHistory(
        integration_id=uuid4(),
        status=SyncStatus.FAILED,
        completed_at=None,
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
        sync_metadata={
            "trigger_source": "webhook",
            "provider": "microsoft",
            "capability": "calendar",
        },
    )
    session = _RecoverySession([receipt])

    async def _sessions(_organisation_id):
        yield session

    monkeypatch.setattr(webhook_recovery_tasks, "acquire_org_async_session", _sessions)
    monkeypatch.setattr(
        webhook_recovery_tasks,
        "send_integrations_task_async",
        AsyncMock(return_value="task-123"),
    )
    monkeypatch.setattr(
        webhook_recovery_tasks,
        "update_webhook_sync_receipt",
        AsyncMock(return_value=True),
    )

    result = await _recover_org_receipts(uuid4())

    compiled = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert receipt.sync_metadata["recovery_attempts"] == 1
    assert result["recovered"] == 1
    session.commit.assert_awaited_once()
