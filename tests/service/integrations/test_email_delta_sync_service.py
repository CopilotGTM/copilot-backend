"""Tests for auth failure propagation in EmailDeltaSyncService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.email_delta_sync_service import (
    EmailDeltaSyncService,
    FolderSyncData,
    IntegrationSyncData,
)


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


@pytest.mark.asyncio
async def test_folder_cursor_is_not_recorded_when_message_write_fails() -> None:
    service = EmailDeltaSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    folder_id = uuid4()
    cursor_updates: dict[str, str | None] = {}

    sync_data = IntegrationSyncData(
        integration_id=integration_id,
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        integration_type_value=IntegrationType.OUTLOOK_EMAIL.value,
        connection_id="conn-1",
        provider="outlook",
        provider_key="outlook",
        last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        connection_metadata={},
        credentials={"access_token": "token"},
        access_token_expires_at=None,
    )
    folder_data = FolderSyncData(
        folder_id=folder_id,
        external_folder_id="inbox",
        folder_name="Inbox",
        folder_type="inbox",
        last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        delta_link="old-delta",
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = MagicMock()
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def mock_acquire(*args, **kwargs):
        yield mock_session

    with (
        patch(
            "src.service.integrations.email_delta_sync_service._fetch_messages_for_provider",
            AsyncMock(return_value=([{"id": "message-1"}], "new-delta")),
        ),
        patch(
            "src.service.integrations.email_delta_sync_service.acquire_org_async_session",
            side_effect=mock_acquire,
        ),
        patch(
            "src.service.integrations.email_delta_sync_service.EmailSyncService"
        ) as mock_email_service_cls,
    ):
        mock_email_service = mock_email_service_cls.return_value
        mock_email_service.sync_messages_incremental = AsyncMock(
            side_effect=RuntimeError("db write failed")
        )

        result = await service._sync_folder_messages_fresh_session(
            token_info=MagicMock(),
            org_id=organisation_id,
            sync_data=sync_data,
            folder_data=folder_data,
            delta_link_updates=cursor_updates,
        )

    assert result["status"] == "failed"
    assert cursor_updates == {}
