"""Tests for Google Calendar provider syncToken incremental flow."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
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


def _mock_nango_with_token() -> AsyncMock:
    """Create a NangoClient mock that returns a valid access token."""
    nango = AsyncMock()
    nango.get_connection.return_value = {
        "credentials": {"access_token": "test-token-123"},
    }
    return nango


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build a mock httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("GET", "https://www.googleapis.com/test"),
    )


class TestGoogleCalendarMapping:
    """Validate expected Nango mapping keys for Google Calendar."""

    def test_connect_key_for_oauth_session(self) -> None:
        assert (
            to_nango_connect_integration_key(IntegrationType.GOOGLE_CALENDAR) == "google-calendar"
        )

    def test_provider_key_for_proxy_requests(self) -> None:
        assert to_nango_provider_key(IntegrationType.GOOGLE_CALENDAR) == "google-calendar"


@pytest.mark.asyncio
class TestGoogleCalendarFetcher:
    """Validate query mode behavior and cursor handling."""

    async def test_initial_sync_uses_time_window_query(self) -> None:
        nango = _mock_nango_with_token()

        responses = [
            _json_response({"items": [], "nextSyncToken": "tok-1"}),
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = responses
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            result = await fetch_events_with_cursor(
                nango=nango,
                connection_id="conn-1",
                is_initial=True,
                last_synced_at=None,
            )

        assert isinstance(result, FetchEventsResult)
        assert result.events == []
        assert result.sync_token == "tok-1"

        params = mock_client_instance.get.call_args.kwargs["params"]
        assert "timeMin" in params
        assert "timeMax" in params
        assert params["orderBy"] == "startTime"
        assert "syncToken" not in params

    async def test_incremental_sync_uses_sync_token_query(self) -> None:
        nango = _mock_nango_with_token()

        responses = [
            _json_response({"items": [{"id": "e1"}], "nextSyncToken": "tok-2"}),
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = responses
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            result = await fetch_events_with_cursor(
                nango=nango,
                connection_id="conn-1",
                is_initial=False,
                last_synced_at=datetime.utcnow(),
                sync_token="tok-1",
            )

        assert result.events == [{"id": "e1"}]
        assert result.sync_token == "tok-2"

        params = mock_client_instance.get.call_args.kwargs["params"]
        assert params["syncToken"] == "tok-1"
        assert "timeMin" not in params
        assert "timeMax" not in params
        assert "updatedMin" not in params
        assert "orderBy" not in params

    async def test_sync_token_invalid_payload_falls_back_to_full_sync(self) -> None:
        """410 HTTP status on first request should fallback to full sync."""
        nango = _mock_nango_with_token()

        gone_resp = httpx.Response(
            status_code=410,
            json={"error": {"code": 410, "message": "Sync token is no longer valid."}},
            request=httpx.Request(
                "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
            ),
        )

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.HTTPStatusError("410", request=gone_resp.request, response=gone_resp)
            return _json_response({"items": [{"id": "full-1"}], "nextSyncToken": "tok-new"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = mock_get
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            result = await fetch_events_with_cursor(
                nango=nango,
                connection_id="conn-1",
                is_initial=False,
                last_synced_at=datetime.utcnow(),
                sync_token="tok-old",
            )

        assert result.events == [{"id": "full-1"}]
        assert result.sync_token == "tok-new"

    async def test_sync_token_invalid_exception_falls_back_to_full_sync(self) -> None:
        """Non-HTTP exceptions with 410 message should also fallback."""
        nango = _mock_nango_with_token()

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("410 Gone: sync token invalid")
            return _json_response({"items": [{"id": "full-1"}], "nextSyncToken": "tok-new"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = mock_get
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            result = await fetch_events_with_cursor(
                nango=nango,
                connection_id="conn-1",
                is_initial=False,
                last_synced_at=datetime.utcnow(),
                sync_token="tok-old",
            )

        assert result.events == [{"id": "full-1"}]
        assert result.sync_token == "tok-new"

    async def test_pagination_aggregates_items_and_uses_page_token(self) -> None:
        nango = _mock_nango_with_token()

        responses = [
            _json_response({"items": [{"id": "e1"}], "nextPageToken": "p2"}),
            _json_response({"items": [{"id": "e2"}], "nextSyncToken": "tok-2"}),
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = responses
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            result = await fetch_events_with_cursor(
                nango=nango,
                connection_id="conn-1",
                is_initial=True,
                last_synced_at=None,
            )

        assert [i["id"] for i in result.events] == ["e1", "e2"]
        assert result.sync_token == "tok-2"

        second_params = mock_client_instance.get.call_args_list[1].kwargs["params"]
        assert second_params["pageToken"] == "p2"

    async def test_custom_provider_config_key_is_used(self) -> None:
        nango = _mock_nango_with_token()

        responses = [
            _json_response({"items": []}),
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = responses
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            await fetch_events_with_cursor(
                nango=nango,
                connection_id="conn-1",
                is_initial=True,
                last_synced_at=None,
                provider_config_key="custom-google-provider",
            )

        # Token retrieval should use the custom provider key
        nango.get_connection.assert_called_once_with(
            provider="custom-google-provider",
            connection_id="conn-1",
            force_refresh=True,
        )

    async def test_backward_compatible_fetch_events_returns_list_only(self) -> None:
        nango = _mock_nango_with_token()

        responses = [
            _json_response({"items": [{"id": "e1"}], "nextSyncToken": "tok-2"}),
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = responses
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            events = await fetch_events(
                nango=nango,
                connection_id="conn-1",
                is_initial=True,
                last_synced_at=None,
            )

        assert events == [{"id": "e1"}]
