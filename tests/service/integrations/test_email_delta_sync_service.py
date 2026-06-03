"""Tests for auth failure propagation in EmailDeltaSyncService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.email_delta_sync_service import (
    EMAIL_DELTA_LINKS_KEY,
    EmailDeltaSyncService,
    FolderSyncData,
    IntegrationSyncData,
)


@pytest.mark.asyncio
async def test_gmail_sync_skips_when_read_scope_is_missing() -> None:
    service = EmailDeltaSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    sync_data = IntegrationSyncData(
        integration_id=integration_id,
        integration_type=IntegrationType.GMAIL,
        integration_type_value=IntegrationType.GMAIL.value,
        connection_id="conn-1",
        provider="gmail",
        provider_key="gmail",
        last_synced_at=None,
        connection_metadata={},
        credentials={"access_token": "token"},
        access_token_expires_at=None,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )

    with patch(
        "src.service.integrations.email_delta_sync_service._fetch_folders_for_provider",
        AsyncMock(),
    ) as fetch_folders:
        result = await service._sync_integration_fresh_session(
            org_id=organisation_id,
            sync_data=sync_data,
        )

    assert result == {
        "status": "success",
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
        "integration_type": IntegrationType.GMAIL.value,
        "folders_synced": 0,
        "messages_synced": 0,
        "folder_results": [],
        "skipped": True,
        "skip_reason": "gmail_read_scope_missing",
    }
    fetch_folders.assert_not_awaited()


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
        scopes=["Mail.Read"],
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


@pytest.mark.asyncio
async def test_folder_cursor_is_persisted_after_successful_message_write() -> None:
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
        scopes=["Mail.Read"],
    )
    folder_data = FolderSyncData(
        folder_id=folder_id,
        external_folder_id="inbox",
        folder_name="Inbox",
        folder_type="inbox",
        last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        delta_link=None,
    )

    integration = MagicMock()
    integration.integration_id = integration_id
    integration.connection_metadata = {}
    folder = MagicMock()

    message_write_result = MagicMock()
    message_write_result.scalar_one_or_none.return_value = integration
    folder_result = MagicMock()
    folder_result.scalar_one_or_none.return_value = folder
    cursor_persist_result = MagicMock()
    cursor_persist_result.scalar_one_or_none.return_value = integration

    write_session = AsyncMock()
    write_session.execute = AsyncMock(side_effect=[message_write_result, folder_result])
    persist_session = AsyncMock()
    persist_session.execute = AsyncMock(return_value=cursor_persist_result)

    sessions = iter([write_session, persist_session])

    async def mock_acquire(*args, **kwargs):
        yield next(sessions)

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
        mock_email_service.sync_messages = AsyncMock(return_value={"records_processed": 1})

        result = await service._sync_folder_messages_fresh_session(
            token_info=MagicMock(),
            org_id=organisation_id,
            sync_data=sync_data,
            folder_data=folder_data,
            delta_link_updates=cursor_updates,
        )

    assert result["messages_synced"] == 1
    assert cursor_updates == {"inbox": "new-delta"}
    assert integration.connection_metadata[EMAIL_DELTA_LINKS_KEY]["inbox"] == "new-delta"
    persist_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_delta_result_updates_folder_timestamp_when_cursor_is_returned() -> None:
    service = EmailDeltaSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    folder_id = uuid4()
    old_synced_at = datetime(2024, 1, 1, tzinfo=UTC)
    cursor_updates: dict[str, str | None] = {}

    sync_data = IntegrationSyncData(
        integration_id=integration_id,
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        integration_type_value=IntegrationType.OUTLOOK_EMAIL.value,
        connection_id="conn-1",
        provider="outlook",
        provider_key="outlook",
        last_synced_at=old_synced_at,
        connection_metadata={},
        credentials={"access_token": "token"},
        access_token_expires_at=None,
        scopes=["Mail.Read"],
    )
    folder_data = FolderSyncData(
        folder_id=folder_id,
        external_folder_id="inbox",
        folder_name="Inbox",
        folder_type="inbox",
        last_synced_at=old_synced_at,
        delta_link="old-delta",
    )

    integration = MagicMock()
    integration.integration_id = integration_id
    integration.connection_metadata = {}
    folder = MagicMock()
    folder.last_synced_at = old_synced_at

    integration_result = MagicMock()
    integration_result.scalar_one_or_none.return_value = integration
    folder_result = MagicMock()
    folder_result.scalar_one_or_none.return_value = folder

    persist_session = AsyncMock()
    persist_session.execute = AsyncMock(side_effect=[integration_result, folder_result])

    async def mock_acquire(*args, **kwargs):
        yield persist_session

    with (
        patch(
            "src.service.integrations.email_delta_sync_service._fetch_messages_for_provider",
            AsyncMock(return_value=([], "new-delta")),
        ),
        patch(
            "src.service.integrations.email_delta_sync_service.acquire_org_async_session",
            side_effect=mock_acquire,
        ),
    ):
        result = await service._sync_folder_messages_fresh_session(
            token_info=MagicMock(),
            org_id=organisation_id,
            sync_data=sync_data,
            folder_data=folder_data,
            delta_link_updates=cursor_updates,
        )

    assert result["messages_synced"] == 0
    assert cursor_updates == {"inbox": "new-delta"}
    assert integration.connection_metadata[EMAIL_DELTA_LINKS_KEY]["inbox"] == "new-delta"
    assert folder.last_synced_at != old_synced_at
    persist_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_delta_result_without_cursor_does_not_persist_folder_checkpoint() -> None:
    service = EmailDeltaSyncService()
    old_synced_at = datetime(2024, 1, 1, tzinfo=UTC)
    cursor_updates: dict[str, str | None] = {}

    sync_data = IntegrationSyncData(
        integration_id=uuid4(),
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        integration_type_value=IntegrationType.OUTLOOK_EMAIL.value,
        connection_id="conn-1",
        provider="outlook",
        provider_key="outlook",
        last_synced_at=old_synced_at,
        connection_metadata={},
        credentials={"access_token": "token"},
        access_token_expires_at=None,
        scopes=["Mail.Read"],
    )
    folder_data = FolderSyncData(
        folder_id=uuid4(),
        external_folder_id="inbox",
        folder_name="Inbox",
        folder_type="inbox",
        last_synced_at=old_synced_at,
        delta_link="old-delta",
    )

    acquire_session = MagicMock()

    with (
        patch(
            "src.service.integrations.email_delta_sync_service._fetch_messages_for_provider",
            AsyncMock(return_value=([], None)),
        ),
        patch(
            "src.service.integrations.email_delta_sync_service.acquire_org_async_session",
            acquire_session,
        ),
    ):
        result = await service._sync_folder_messages_fresh_session(
            token_info=MagicMock(),
            org_id=uuid4(),
            sync_data=sync_data,
            folder_data=folder_data,
            delta_link_updates=cursor_updates,
        )

    assert result["messages_synced"] == 0
    assert cursor_updates == {}
    acquire_session.assert_not_called()
