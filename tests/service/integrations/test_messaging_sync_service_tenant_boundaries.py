"""Regression tests for provider-scoped messaging channel resolution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.integrations.messaging_sync_service import MessagingSyncService


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalars(self) -> "_ScalarResult":
        return self

    def first(self) -> object:
        return self._value


@pytest.mark.asyncio
async def test_channel_resolution_keeps_provider_in_cache_and_query_identity() -> None:
    """The same external ID in Slack and Teams must resolve independently."""
    organisation_id = uuid4()
    slack_channel_id = uuid4()
    teams_channel_id = uuid4()
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(SimpleNamespace(channel_id=slack_channel_id)),
        _ScalarResult(SimpleNamespace(channel_id=teams_channel_id)),
    ]
    service = MessagingSyncService(session, organisation_id)

    slack_result = await service._resolve_channel_id("shared-id", "slack")
    teams_result = await service._resolve_channel_id("shared-id", "teams")

    assert slack_result == slack_channel_id
    assert teams_result == teams_channel_id
    assert session.execute.await_count == 2
    for call, provider in zip(session.execute.await_args_list, ("slack", "teams"), strict=True):
        compiled = call.args[0].compile()
        assert organisation_id in compiled.params.values()
        assert provider in compiled.params.values()
