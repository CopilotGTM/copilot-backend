"""Tests for Google Calendar provider syncToken incremental flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.calendar.google_calendar import (
    FetchEventsResult,
    fetch_events,
    fetch_events_with_cursor,
)


def _make_token_info() -> IntegrationTokenInfo:
    """Create a valid IntegrationTokenInfo for testing."""
    return IntegrationTokenInfo(
        access_token="test-token-123",
        refresh_token="test-refresh",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build a mock httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("GET", "https://www.googleapis.com/test"),
    )


@pytest.mark.asyncio
class TestGoogleCalendarFetcher:
    """Validate query mode behavior and cursor handling."""

    async def test_initial_sync_uses_cursor_compatible_time_window_query(self) -> None:
        """The bootstrap query must let Google return the next incremental cursor."""
        token_info = _make_token_info()

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
                token_info=token_info,
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
        assert "orderBy" not in params
        assert "syncToken" not in params

    async def test_incremental_sync_uses_sync_token_query(self) -> None:
        """Incremental requests use only the stored token and compatible flags."""
        token_info = _make_token_info()

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
                token_info=token_info,
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
        token_info = _make_token_info()

        gone_resp = httpx.Response(
            status_code=410,
            json={"error": {"code": 410, "message": "Sync token is no longer valid."}},
            request=httpx.Request(
                "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
            ),
        )

        call_count = 0

        async def mock_get(url, **kwargs):
            """Return one expired-token response before the bootstrap succeeds."""
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
                token_info=token_info,
                connection_id="conn-1",
                is_initial=False,
                last_synced_at=datetime.utcnow(),
                sync_token="tok-old",
            )

        assert result.events == [{"id": "full-1"}]
        assert result.sync_token == "tok-new"

    async def test_sync_token_invalid_exception_falls_back_to_full_sync(self) -> None:
        """Non-HTTP exceptions with 410 message should also fallback."""
        token_info = _make_token_info()

        call_count = 0

        async def mock_get(url, **kwargs):
            """Model an SDK-wrapped token expiry followed by a valid bootstrap."""
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
                token_info=token_info,
                connection_id="conn-1",
                is_initial=False,
                last_synced_at=datetime.utcnow(),
                sync_token="tok-old",
            )

        assert result.events == [{"id": "full-1"}]
        assert result.sync_token == "tok-new"

    async def test_pagination_aggregates_items_and_uses_page_token(self) -> None:
        """Only the final page cursor is returned after every event is collected."""
        token_info = _make_token_info()

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
                token_info=token_info,
                connection_id="conn-1",
                is_initial=True,
                last_synced_at=None,
            )

        assert [i["id"] for i in result.events] == ["e1", "e2"]
        assert result.sync_token == "tok-2"

        second_params = mock_client_instance.get.call_args_list[1].kwargs["params"]
        assert second_params["pageToken"] == "p2"

    async def test_completed_fetch_without_sync_token_fails(self) -> None:
        """A cursorless terminal page must not become another full sync later."""
        token_info = _make_token_info()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.return_value = _json_response({"items": []})
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            with pytest.raises(RuntimeError, match="without nextSyncToken"):
                await fetch_events_with_cursor(
                    token_info=token_info,
                    connection_id="conn-1",
                    is_initial=True,
                    last_synced_at=None,
                )

    async def test_backward_compatible_fetch_events_returns_list_only(self) -> None:
        """Registry callers still receive the legacy list-only result shape."""
        token_info = _make_token_info()

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
                token_info=token_info,
                connection_id="conn-1",
                is_initial=True,
                last_synced_at=None,
            )

        assert events == [{"id": "e1"}]
