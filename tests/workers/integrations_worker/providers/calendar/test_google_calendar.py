"""Tests for Google Calendar provider syncToken incremental flow."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import (
    to_nango_connect_integration_key,
    to_nango_provider_key,
)
from src.workers.integrations_worker.providers.calendar.google_calendar import (
    FetchEventsResult,
    fetch_events,
    fetch_events_with_cursor,
)


class TestGoogleCalendarMapping:
    """Validate expected Nango mapping keys for Google Calendar."""

    def test_connect_key_for_oauth_session(self) -> None:
        assert (
            to_nango_connect_integration_key(IntegrationType.GOOGLE_CALENDAR)
            == "google-calendar-getting-started"
        )

    def test_provider_key_for_proxy_requests(self) -> None:
        assert to_nango_provider_key(IntegrationType.GOOGLE_CALENDAR) == "google-calendar"


@pytest.mark.asyncio
class TestGoogleCalendarFetcher:
    """Validate query mode behavior and cursor handling."""

    async def test_initial_sync_uses_time_window_query(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": [], "nextSyncToken": "tok-1"})

        result = await fetch_events_with_cursor(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )

        assert isinstance(result, FetchEventsResult)
        assert result.events == []
        assert result.sync_token == "tok-1"

        params = mock_nango.proxy_request.call_args.kwargs["params"]
        assert "timeMin" in params
        assert "timeMax" in params
        assert params["orderBy"] == "startTime"
        assert "syncToken" not in params

    async def test_incremental_sync_uses_sync_token_query(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(
            return_value={"items": [{"id": "e1"}], "nextSyncToken": "tok-2"}
        )

        result = await fetch_events_with_cursor(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=datetime.utcnow(),
            sync_token="tok-1",
        )

        assert result.events == [{"id": "e1"}]
        assert result.sync_token == "tok-2"

        params = mock_nango.proxy_request.call_args.kwargs["params"]
        assert params["syncToken"] == "tok-1"
        assert "timeMin" not in params
        assert "timeMax" not in params
        assert "updatedMin" not in params
        assert "orderBy" not in params

    async def test_sync_token_invalid_payload_falls_back_to_full_sync(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(
            side_effect=[
                {"error": {"code": 410, "message": "Sync token is no longer valid."}},
                {"items": [{"id": "full-1"}], "nextSyncToken": "tok-new"},
            ]
        )

        result = await fetch_events_with_cursor(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=datetime.utcnow(),
            sync_token="tok-old",
        )

        assert result.events == [{"id": "full-1"}]
        assert result.sync_token == "tok-new"
        assert mock_nango.proxy_request.call_count == 2

        first_params = mock_nango.proxy_request.call_args_list[0].kwargs["params"]
        second_params = mock_nango.proxy_request.call_args_list[1].kwargs["params"]
        assert first_params.get("syncToken") == "tok-old"
        assert "syncToken" not in second_params
        assert "timeMin" in second_params
        assert "timeMax" in second_params

    async def test_sync_token_invalid_exception_falls_back_to_full_sync(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(
            side_effect=[
                RuntimeError("410 Gone: sync token invalid"),
                {"items": [{"id": "full-1"}], "nextSyncToken": "tok-new"},
            ]
        )

        result = await fetch_events_with_cursor(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=False,
            last_synced_at=datetime.utcnow(),
            sync_token="tok-old",
        )

        assert result.events == [{"id": "full-1"}]
        assert result.sync_token == "tok-new"
        assert mock_nango.proxy_request.call_count == 2

    async def test_pagination_aggregates_items_and_uses_page_token(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(
            side_effect=[
                {"items": [{"id": "e1"}], "nextPageToken": "p2"},
                {"items": [{"id": "e2"}], "nextSyncToken": "tok-2"},
            ]
        )

        result = await fetch_events_with_cursor(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )

        assert [i["id"] for i in result.events] == ["e1", "e2"]
        assert result.sync_token == "tok-2"

        second_params = mock_nango.proxy_request.call_args_list[1].kwargs["params"]
        assert second_params["pageToken"] == "p2"

    async def test_custom_provider_config_key_is_used(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        await fetch_events_with_cursor(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
            provider_config_key="custom-google-provider",
        )

        kwargs = mock_nango.proxy_request.call_args.kwargs
        assert kwargs["provider_config_key"] == "custom-google-provider"

    async def test_backward_compatible_fetch_events_returns_list_only(self) -> None:
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(
            return_value={"items": [{"id": "e1"}], "nextSyncToken": "tok-2"}
        )

        events = await fetch_events(
            nango=mock_nango,
            connection_id="conn-1",
            is_initial=True,
            last_synced_at=None,
        )

        assert events == [{"id": "e1"}]
