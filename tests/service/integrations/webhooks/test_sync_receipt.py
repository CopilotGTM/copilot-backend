"""Regression tests for monotonic durable-webhook receipt transitions."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationSyncHistory, SyncStatus
from src.service.integrations.webhooks import sync_receipt


class _OneResult:
    def __init__(self, receipt: IntegrationSyncHistory) -> None:
        self.receipt = receipt

    def scalar_one_or_none(self) -> IntegrationSyncHistory:
        return self.receipt


class _Session:
    def __init__(self, receipt: IntegrationSyncHistory) -> None:
        self.receipt = receipt
        self.commit = AsyncMock()

    async def execute(self, _statement):
        return _OneResult(self.receipt)

    def add(self, _receipt) -> None:
        return None


def _session_provider(session: _Session):
    async def _provider(_organisation_id):
        yield session

    return _provider


@pytest.mark.asyncio
async def test_success_cannot_regress_to_queued(monkeypatch) -> None:
    """A fast worker may finish before publisher metadata is stored."""
    receipt = IntegrationSyncHistory(
        integration_id=uuid4(),
        status=SyncStatus.SUCCESS,
        sync_metadata={"trigger_source": "webhook"},
    )
    session = _Session(receipt)
    monkeypatch.setattr(
        sync_receipt,
        "acquire_org_async_session",
        _session_provider(session),
    )

    changed = await sync_receipt.update_webhook_sync_receipt(
        organisation_id=uuid4(),
        sync_id=receipt.sync_id,
        status=SyncStatus.QUEUED,
    )

    assert changed is False
    assert receipt.status == SyncStatus.SUCCESS
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_only_update_preserves_success(monkeypatch) -> None:
    """Post-publish task IDs are diagnostic metadata, not lifecycle transitions."""
    receipt = IntegrationSyncHistory(
        integration_id=uuid4(),
        status=SyncStatus.SUCCESS,
        sync_metadata={"trigger_source": "webhook"},
    )
    session = _Session(receipt)
    monkeypatch.setattr(
        sync_receipt,
        "acquire_org_async_session",
        _session_provider(session),
    )

    changed = await sync_receipt.update_webhook_sync_receipt(
        organisation_id=uuid4(),
        sync_id=receipt.sync_id,
        metadata={"task_id": "task-123"},
    )

    assert changed is True
    assert receipt.status == SyncStatus.SUCCESS
    assert receipt.sync_metadata["task_id"] == "task-123"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_action_required_failure_stays_closed(monkeypatch) -> None:
    """The watchdog cannot replay a receipt that needs user reconnection."""
    receipt = IntegrationSyncHistory(
        integration_id=uuid4(),
        status=SyncStatus.FAILED,
        sync_metadata={"trigger_source": "webhook", "terminal": True},
    )
    session = _Session(receipt)
    monkeypatch.setattr(
        sync_receipt,
        "acquire_org_async_session",
        _session_provider(session),
    )

    changed = await sync_receipt.update_webhook_sync_receipt(
        organisation_id=uuid4(),
        sync_id=receipt.sync_id,
        status=SyncStatus.IN_PROGRESS,
    )

    assert changed is False
    assert receipt.status == SyncStatus.FAILED
