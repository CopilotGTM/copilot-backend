"""Regression tests for sync-history terminal persistence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.service.integrations.sync_history_manager import SyncHistoryManager


@pytest.mark.asyncio
async def test_fail_rolls_back_before_persisting_bounded_terminal_state() -> None:
    """A failed write must not leave its history row stuck in progress."""
    sync_id = uuid4()
    integration_id = uuid4()
    history = SimpleNamespace(sync_id=sync_id)
    persisted_history = SimpleNamespace()
    integration = SimpleNamespace()
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: persisted_history),
            SimpleNamespace(scalar_one_or_none=lambda: integration),
        ]
    )

    await SyncHistoryManager(session).fail(
        history,
        integration_id=integration_id,
        records_processed=100,
        records_failed=12,
        error=RuntimeError("payload=" + "x" * 2_000),
    )

    session.rollback.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert persisted_history.status.value == "failed"
    assert persisted_history.records_processed == 100
    assert persisted_history.records_failed == 12
    assert len(persisted_history.error_details["error"]) <= 300
    assert integration.last_sync_status == "failed"
    assert len(integration.last_sync_error) <= 300


@pytest.mark.asyncio
async def test_fail_does_not_replace_the_original_error_when_reporting_fails() -> None:
    """Observability persistence is best-effort so caller retry classification stays intact."""
    session = AsyncMock()
    session.rollback.side_effect = RuntimeError("database still unavailable")

    await SyncHistoryManager(session).fail(
        SimpleNamespace(sync_id=uuid4()),
        integration_id=uuid4(),
        records_processed=100,
        records_failed=12,
        error=TimeoutError("provider timed out"),
    )

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
