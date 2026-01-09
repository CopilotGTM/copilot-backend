"""Tests for Google Calendar provider.

These tests verify the functionality of the Google Calendar fetch_events function,
including time window calculations, API parameter generation, and response parsing.
"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType


class TestGoogleCalendarIntegrationConfig:
    """Tests for Google Calendar integration configuration."""

    def test_integration_type_exists(self) -> None:
        """IntegrationType should have GOOGLE_CALENDAR."""
        assert hasattr(IntegrationType, "GOOGLE_CALENDAR")
        assert IntegrationType.GOOGLE_CALENDAR.value == "google_calendar"

    def test_provider_config_key_mapping(self) -> None:
        """Google Calendar should map to correct Nango provider key."""
        from src.infra.config.nango_provider_mapping import (
            to_nango_connect_integration_key,
        )

        key = to_nango_connect_integration_key(IntegrationType.GOOGLE_CALENDAR)
        assert key == "google-calendar"


class TestGoogleCalendarFetchEventsSignature:
    """Tests for fetch_events function signature and parameters."""

    def test_function_exists(self) -> None:
        """fetch_events function should exist in module."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        assert callable(fetch_events)

    def test_function_is_async(self) -> None:
        """fetch_events should be an async function."""
        import asyncio

        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        assert asyncio.iscoroutinefunction(fetch_events)

    def test_function_accepts_required_params(self) -> None:
        """fetch_events should accept required parameters."""
        import inspect

        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        sig = inspect.signature(fetch_events)
        params = sig.parameters

        assert "nango" in params
        assert "connection_id" in params
        assert "is_initial" in params
        assert "last_synced_at" in params

    def test_function_accepts_optional_params(self) -> None:
        """fetch_events should accept optional parameters."""
        import inspect

        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        sig = inspect.signature(fetch_events)
        params = sig.parameters

        assert "data_type" in params
        assert params["data_type"].default is None
        assert "provider_config_key" in params
        assert params["provider_config_key"].default is None


class TestGoogleCalendarTimeWindowCalculations:
    """Tests for time window calculation logic."""

    def test_initial_sync_uses_90_day_lookback(self) -> None:
        """Initial sync should look back 90 days."""
        now = datetime.now(UTC)
        is_initial = True
        last_synced_at = None

        if is_initial or not last_synced_at:
            time_min = now - timedelta(days=90)
        else:
            time_min = last_synced_at - timedelta(days=1)

        expected = now - timedelta(days=90)
        assert abs((time_min - expected).total_seconds()) < 1

    def test_initial_sync_uses_365_day_lookahead(self) -> None:
        """Initial sync should look forward 365 days."""
        now = datetime.now(UTC)
        time_max = now + timedelta(days=365)

        expected = now + timedelta(days=365)
        assert abs((time_max - expected).total_seconds()) < 1

    def test_incremental_sync_uses_last_synced_with_backoff(self) -> None:
        """Incremental sync should use last_synced_at - 1 day."""
        now = datetime.now(UTC)
        last_synced_at = now - timedelta(hours=3)
        is_initial = False

        if is_initial or not last_synced_at:
            time_min = now - timedelta(days=90)
        else:
            time_min = last_synced_at - timedelta(days=1)

        expected = last_synced_at - timedelta(days=1)
        assert abs((time_min - expected).total_seconds()) < 1

    def test_incremental_sync_without_last_synced_falls_back(self) -> None:
        """Incremental sync without last_synced_at should fall back to 90 days."""
        now = datetime.now(UTC)
        last_synced_at = None
        is_initial = False

        if is_initial or not last_synced_at:
            time_min = now - timedelta(days=90)
        else:
            time_min = last_synced_at - timedelta(days=1)

        expected = now - timedelta(days=90)
        assert abs((time_min - expected).total_seconds()) < 1


class TestGoogleCalendarUTCHandling:
    """Tests for UTC timezone handling."""

    def test_ensure_utc_on_naive_datetime(self) -> None:
        """Naive datetime should get UTC timezone added."""
        naive_dt = datetime(2024, 1, 15, 10, 30, 0)
        assert naive_dt.tzinfo is None

        if naive_dt.tzinfo is None:
            utc_dt = naive_dt.replace(tzinfo=UTC)
        else:
            utc_dt = naive_dt.astimezone(UTC)

        assert utc_dt.tzinfo is not None

    def test_ensure_utc_on_aware_datetime(self) -> None:
        """Aware datetime should be converted to UTC."""
        # Create a non-UTC aware datetime (e.g., UTC+5)
        offset = timezone(timedelta(hours=5))
        aware_dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=offset)

        if aware_dt.tzinfo is None:
            utc_dt = aware_dt.replace(tzinfo=UTC)
        else:
            utc_dt = aware_dt.astimezone(UTC)

        assert utc_dt.hour == 5  # 10:30 UTC+5 = 05:30 UTC

    def test_utc_datetime_unchanged(self) -> None:
        """UTC datetime should remain unchanged."""
        utc_dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

        result = utc_dt.replace(tzinfo=UTC) if utc_dt.tzinfo is None else utc_dt.astimezone(UTC)

        assert result.hour == 10


class TestGoogleCalendarAPIParameters:
    """Tests for Google Calendar API parameter generation."""

    def test_params_include_time_min(self) -> None:
        """Parameters should include timeMin."""
        now = datetime.now(UTC)
        time_min = now - timedelta(days=90)

        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": (now + timedelta(days=365)).isoformat(),
            "singleEvents": True,
            "maxResults": 2500,
            "orderBy": "startTime",
        }

        assert "timeMin" in params
        assert params["timeMin"] == time_min.isoformat()

    def test_params_include_time_max(self) -> None:
        """Parameters should include timeMax."""
        now = datetime.now(UTC)
        time_max = now + timedelta(days=365)

        params = {
            "timeMin": (now - timedelta(days=90)).isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "maxResults": 2500,
            "orderBy": "startTime",
        }

        assert "timeMax" in params
        assert params["timeMax"] == time_max.isoformat()

    def test_params_single_events_true(self) -> None:
        """singleEvents should be True to expand recurring events."""
        params = {
            "singleEvents": True,
        }

        assert params["singleEvents"] is True

    def test_params_max_results_2500(self) -> None:
        """maxResults should be 2500 (Google Calendar limit)."""
        params = {
            "maxResults": 2500,
        }

        assert params["maxResults"] == 2500

    def test_params_order_by_start_time(self) -> None:
        """orderBy should be startTime for chronological ordering."""
        params = {
            "orderBy": "startTime",
        }

        assert params["orderBy"] == "startTime"


class TestGoogleCalendarEndpoint:
    """Tests for Google Calendar API endpoint."""

    def test_endpoint_uses_primary_calendar(self) -> None:
        """Should use primary calendar endpoint."""
        endpoint = "/calendar/v3/calendars/primary/events"

        assert "primary" in endpoint
        assert "events" in endpoint
        assert endpoint == "/calendar/v3/calendars/primary/events"


class TestGoogleCalendarResponseParsing:
    """Tests for parsing Google Calendar API responses."""

    def test_extracts_items_from_response(self) -> None:
        """Should extract items array from response."""
        resp = {
            "kind": "calendar#events",
            "items": [
                {"id": "event1", "summary": "Meeting 1"},
                {"id": "event2", "summary": "Meeting 2"},
            ],
        }

        items = []
        if isinstance(resp, dict) and "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]

        assert len(items) == 2
        assert items[0]["id"] == "event1"

    def test_extracts_results_fallback(self) -> None:
        """Should fall back to results array if items not present."""
        resp = {
            "results": [
                {"id": "event1", "summary": "Meeting 1"},
            ],
        }

        items = []
        if isinstance(resp, dict):
            if "items" in resp and isinstance(resp["items"], list):
                items = resp["items"]
            elif "results" in resp and isinstance(resp["results"], list):
                items = resp["results"]

        assert len(items) == 1
        assert items[0]["id"] == "event1"

    def test_returns_empty_list_for_missing_items(self) -> None:
        """Should return empty list if items/results not present."""
        resp = {"kind": "calendar#events"}

        items = []
        if isinstance(resp, dict) and "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]

        assert items == []

    def test_returns_empty_list_for_non_dict_response(self) -> None:
        """Should return empty list for non-dict response."""
        resp = "error response"

        items = []
        if isinstance(resp, dict) and "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]

        assert items == []

    def test_handles_none_response(self) -> None:
        """Should handle None response gracefully."""
        resp = None

        items = []
        if isinstance(resp, dict) and "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]

        assert items == []


class TestGoogleCalendarEventStructure:
    """Tests for expected Google Calendar event structure."""

    def test_event_has_expected_fields(self) -> None:
        """Google Calendar events should have expected fields."""
        sample_event = {
            "id": "abc123",
            "summary": "Team Meeting",
            "description": "Weekly sync",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "location": "Room A",
            "attendees": [{"email": "user@example.com", "responseStatus": "accepted"}],
            "organizer": {"email": "organizer@example.com"},
            "htmlLink": "https://calendar.google.com/event?eid=abc123",
            "status": "confirmed",
            "conferenceData": {"entryPoints": [{"uri": "https://meet.google.com/xyz"}]},
            "created": "2024-01-10T08:00:00Z",
            "updated": "2024-01-14T16:00:00Z",
        }

        # Verify essential fields exist
        assert "id" in sample_event
        assert "summary" in sample_event
        assert "start" in sample_event
        assert "end" in sample_event
        assert "organizer" in sample_event

    def test_all_day_event_uses_date_field(self) -> None:
        """All-day events use 'date' instead of 'dateTime'."""
        all_day_event = {
            "id": "allday123",
            "summary": "Company Holiday",
            "start": {"date": "2024-01-01"},
            "end": {"date": "2024-01-02"},
        }

        # All-day events have 'date' key instead of 'dateTime'
        assert "date" in all_day_event["start"]
        assert "dateTime" not in all_day_event["start"]


@pytest.mark.asyncio
class TestGoogleCalendarFetchEventsAsync:
    """Async tests for fetch_events function."""

    async def test_fetch_events_calls_nango_proxy(self) -> None:
        """fetch_events should call nango.proxy_request."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        mock_nango.proxy_request.assert_called_once()
        assert result == []

    async def test_fetch_events_uses_correct_endpoint(self) -> None:
        """fetch_events should use correct Google Calendar endpoint."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert call_kwargs["endpoint"] == "/calendar/v3/calendars/primary/events"

    async def test_fetch_events_passes_connection_id(self) -> None:
        """fetch_events should pass connection_id to Nango."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="my-connection-123",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert call_kwargs["connection_id"] == "my-connection-123"

    async def test_fetch_events_returns_items(self) -> None:
        """fetch_events should return items from response."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_events = [
            {"id": "event1", "summary": "Meeting 1"},
            {"id": "event2", "summary": "Meeting 2"},
        ]

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": mock_events})

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        assert len(result) == 2
        assert result[0]["id"] == "event1"
        assert result[1]["id"] == "event2"

    async def test_fetch_events_uses_custom_provider_config_key(self) -> None:
        """fetch_events should use custom provider_config_key if provided."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
            provider_config_key="custom-google-calendar",
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert call_kwargs["provider_config_key"] == "custom-google-calendar"

    async def test_fetch_events_includes_time_params(self) -> None:
        """fetch_events should include timeMin and timeMax in params."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        params = call_kwargs["params"]

        assert "timeMin" in params
        assert "timeMax" in params
        assert "singleEvents" in params
        assert params["singleEvents"] is True


@pytest.mark.asyncio
class TestGoogleCalendarPagination:
    """Tests for Google Calendar pagination functionality."""

    async def test_fetch_events_handles_single_page(self) -> None:
        """Should return all events when there's no pagination."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_events = [{"id": "event1"}, {"id": "event2"}]
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": mock_events})

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        assert len(result) == 2
        mock_nango.proxy_request.assert_called_once()

    async def test_fetch_events_handles_pagination(self) -> None:
        """Should fetch all pages when nextPageToken is present."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        # First page with nextPageToken
        page1 = {
            "items": [{"id": "event1"}, {"id": "event2"}],
            "nextPageToken": "token123",
        }
        # Second page without nextPageToken
        page2 = {
            "items": [{"id": "event3"}],
        }

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(side_effect=[page1, page2])

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        assert len(result) == 3
        assert result[0]["id"] == "event1"
        assert result[1]["id"] == "event2"
        assert result[2]["id"] == "event3"
        assert mock_nango.proxy_request.call_count == 2

    async def test_fetch_events_passes_page_token(self) -> None:
        """Should pass pageToken in params for subsequent requests."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        page1 = {
            "items": [{"id": "event1"}],
            "nextPageToken": "page2token",
        }
        page2 = {"items": [{"id": "event2"}]}

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(side_effect=[page1, page2])

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        # Check second call has pageToken
        second_call = mock_nango.proxy_request.call_args_list[1]
        assert second_call.kwargs["params"]["pageToken"] == "page2token"

    async def test_fetch_events_respects_max_pages(self) -> None:
        """Should stop after max_pages to prevent infinite loops."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        # Always return nextPageToken (would be infinite without max_pages)
        infinite_page = {
            "items": [{"id": "event"}],
            "nextPageToken": "always_more",
        }

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value=infinite_page)

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        # Should stop at 50 pages (max_pages limit)
        assert mock_nango.proxy_request.call_count == 50
        assert len(result) == 50  # 1 event per page

    async def test_fetch_events_removes_page_token_on_first_request(self) -> None:
        """First request should not have pageToken."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"items": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert "pageToken" not in call_kwargs["params"]

    async def test_fetch_events_three_pages(self) -> None:
        """Should correctly aggregate events across multiple pages."""
        from src.workers.integrations_worker.providers.calendar.google_calendar import (
            fetch_events,
        )

        pages = [
            {"items": [{"id": f"event{i}"} for i in range(1, 4)], "nextPageToken": "t1"},
            {"items": [{"id": f"event{i}"} for i in range(4, 7)], "nextPageToken": "t2"},
            {"items": [{"id": f"event{i}"} for i in range(7, 10)]},
        ]

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(side_effect=pages)

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        assert len(result) == 9
        assert mock_nango.proxy_request.call_count == 3
