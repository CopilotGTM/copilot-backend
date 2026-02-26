"""Tests for provider-aware identity resolution in form handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.service.bots.form_handlers.identity import (
    get_provider_identity,
    resolve_user_from_activity,
)


def test_get_provider_identity_defaults_to_teams() -> None:
    activity = {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }

    provider, user_id, tenant_id = get_provider_identity(activity)

    assert provider == ExternalIdentityProvider.TEAMS
    assert user_id == "teams-user-123"
    assert tenant_id == "tenant-456"


def test_get_provider_identity_slack() -> None:
    activity = {
        "provider": "slack",
        "from": {"id": "U123"},
        "channelData": {"team": {"id": "T456"}},
    }

    provider, user_id, tenant_id = get_provider_identity(activity)

    assert provider == ExternalIdentityProvider.SLACK
    assert user_id == "U123"
    assert tenant_id == "T456"


@pytest.mark.asyncio
async def test_resolve_user_from_activity_uses_slack_provider() -> None:
    activity = {
        "provider": "slack",
        "from": {"id": "U123"},
        "channelData": {"team": {"id": "T456"}},
    }

    mock_user = MagicMock()
    mock_identity_service = MagicMock()
    mock_identity_service.get_user_for_external_identity = AsyncMock(return_value=(mock_user, None))
    mock_master_session = AsyncMock()

    async def mock_acquire_master():
        yield mock_master_session

    with (
        patch(
            "src.service.bots.form_handlers.identity.ExternalIdentityService",
            return_value=mock_identity_service,
        ),
        patch(
            "src.service.bots.form_handlers.identity.acquire_master_async_session",
            mock_acquire_master,
        ),
    ):
        user, mapping = await resolve_user_from_activity(activity)

    assert user == mock_user
    assert mapping is None
    mock_identity_service.get_user_for_external_identity.assert_awaited_once_with(
        mock_master_session,
        ExternalIdentityProvider.SLACK,
        "U123",
        "T456",
    )
