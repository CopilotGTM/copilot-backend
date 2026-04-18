"""Tests for auth failure propagation in EmailDeltaSyncService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.email_delta_sync_service import EmailDeltaSyncService


@pytest.mark.asyncio
async def test_sync_integration_reraises_auth_failures() -> None:
    service = EmailDeltaSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()

    mock_integration = MagicMock()
    mock_integration.integration_id = integration_id
    mock_integration.integration_type = IntegrationType.GMAIL
    mock_integration.connection_id = "conn-1"
    mock_integration.last_synced_at = None
    mock_integration.connection_metadata = {}
    mock_integration.credentials = {"access_token": "token"}
    mock_integration.access_token_expires_at = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_integration
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def mock_acquire(*args, **kwargs):
        yield mock_session

    with (
        patch(
            "src.service.integrations.email_delta_sync_service.acquire_org_async_session",
            side_effect=mock_acquire,
        ),
        patch.object(
            service,
            "_sync_integration_fresh_session",
            AsyncMock(
                side_effect=IntegrationPermissionError(
                    "Gmail permission was not granted. Please reconnect and allow Gmail access."
                )
            ),
        ),
        pytest.raises(IntegrationPermissionError, match="Gmail permission was not granted"),
    ):
        await service.sync_integration(organisation_id, integration_id)
