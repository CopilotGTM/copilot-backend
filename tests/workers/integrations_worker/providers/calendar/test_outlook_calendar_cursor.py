"""Cursor contract tests for the direct Microsoft Calendar provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
    fetch_events,
)


@pytest.mark.asyncio
async def test_completed_fetch_without_delta_link_fails() -> None:
    """A terminal Graph delta round must return the cursor for the next round."""
    with (
        patch(
            "src.workers.integrations_worker.providers.calendar.outlook_calendar.call_with_token_retry",
            AsyncMock(return_value=([], None)),
        ),
        pytest.raises(RuntimeError, match="without @odata.deltaLink"),
    ):
        await fetch_events(
            token_info=MagicMock(),
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )


@pytest.mark.asyncio
async def test_completed_fetch_returns_delta_link() -> None:
    """A valid Graph checkpoint remains available for sync-state persistence."""
    with patch(
        "src.workers.integrations_worker.providers.calendar.outlook_calendar.call_with_token_retry",
        AsyncMock(return_value=([], "https://graph.microsoft.com/delta?token=next")),
    ):
        result = await fetch_events(
            token_info=MagicMock(),
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )

    assert result.delta_link == "https://graph.microsoft.com/delta?token=next"
