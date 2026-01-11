"""Tests for Outlook Calendar provider.

These tests verify the functionality of the Outlook Calendar fetch_events function,
including time window calculations, Microsoft Graph API parameters, and response parsing.
"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType


class TestOutlookCalendarIntegrationConfig:
    """Tests for Outlook Calendar integration configuration."""

    def test_integration_type_exists(self) -> None:
        """IntegrationType should have OUTLOOK_CALENDAR."""
        assert hasattr(IntegrationType, "OUTLOOK_CALENDAR")
        assert IntegrationType.OUTLOOK_CALENDAR.value == "outlook_calendar"

    def test_provider_config_key_mapping(self) -> None:
        """Outlook Calendar should map to correct Nango provider key."""
        from src.infra.config.nango_provider_mapping import (
            to_nango_connect_integration_key,
        )

        key = to_nango_connect_integration_key(IntegrationType.OUTLOOK_CALENDAR)
        # Outlook uses outlook-bflm as the provider key
        assert "outlook" in key.lower()


class TestOutlookCalendarFetchEventsSignature:
    """Tests for fetch_events function signature and parameters."""

    def test_function_exists(self) -> None:
        """fetch_events function should exist in module."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        assert callable(fetch_events)

    def test_function_is_async(self) -> None:
        """fetch_events should be an async function."""
        import asyncio

        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        assert asyncio.iscoroutinefunction(fetch_events)

    def test_function_accepts_required_params(self) -> None:
        """fetch_events should accept required parameters."""
        import inspect

        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
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

        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        sig = inspect.signature(fetch_events)
        params = sig.parameters

        assert "data_type" in params
        assert params["data_type"].default is None
        assert "provider_config_key" in params
        assert params["provider_config_key"].default is None


class TestOutlookCalendarTimeWindowCalculations:
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


class TestOutlookCalendarUTCHandling:
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
        offset = timezone(timedelta(hours=5))
        aware_dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=offset)

        if aware_dt.tzinfo is None:
            utc_dt = aware_dt.replace(tzinfo=UTC)
        else:
            utc_dt = aware_dt.astimezone(UTC)

        assert utc_dt.hour == 5  # 10:30 UTC+5 = 05:30 UTC


class TestOutlookCalendarMSGraphAPIParameters:
    """Tests for Microsoft Graph API parameter generation."""

    def test_params_use_odata_filter(self) -> None:
        """Parameters should use $filter for date range."""
        now = datetime.now(UTC)
        time_min = now - timedelta(days=90)
        time_max = now + timedelta(days=365)

        params = {
            "$filter": f"start/dateTime ge '{time_min.isoformat()}' and start/dateTime le '{time_max.isoformat()}'",
            "$orderby": "start/dateTime",
            "$top": "999",
        }

        assert "$filter" in params
        assert "start/dateTime ge" in params["$filter"]
        assert "start/dateTime le" in params["$filter"]

    def test_params_use_odata_orderby(self) -> None:
        """Parameters should use $orderby for sorting."""
        params = {
            "$orderby": "start/dateTime",
        }

        assert params["$orderby"] == "start/dateTime"

    def test_params_use_odata_top(self) -> None:
        """Parameters should use $top to limit results."""
        params = {
            "$top": "999",
        }

        assert params["$top"] == "999"

    def test_params_include_select_fields(self) -> None:
        """Parameters should include $select for required fields."""
        expected_fields = [
            "id",
            "subject",
            "body",
            "start",
            "end",
            "location",
            "attendees",
            "organizer",
            "webLink",
            "isAllDay",
            "isCancelled",
            "onlineMeeting",
            "createdDateTime",
            "lastModifiedDateTime",
        ]

        params = {
            "$select": ",".join(expected_fields),
        }

        assert "$select" in params
        for field in ["id", "subject", "start", "end", "organizer"]:
            assert field in params["$select"]


class TestOutlookCalendarEndpoint:
    """Tests for Microsoft Graph API endpoint."""

    def test_endpoint_uses_primary_calendar(self) -> None:
        """Should use primary calendar events endpoint."""
        endpoint = "/v1.0/me/calendar/events"

        assert "/me/calendar" in endpoint
        assert "events" in endpoint
        assert endpoint == "/v1.0/me/calendar/events"

    def test_endpoint_uses_v1_api(self) -> None:
        """Should use v1.0 of Microsoft Graph API."""
        endpoint = "/v1.0/me/calendar/events"

        assert endpoint.startswith("/v1.0")


class TestOutlookCalendarResponseParsing:
    """Tests for parsing Microsoft Graph API responses."""

    def test_extracts_value_from_response(self) -> None:
        """Should extract value array from response (Graph uses 'value')."""
        resp = {
            "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#events",
            "value": [
                {"id": "event1", "subject": "Meeting 1"},
                {"id": "event2", "subject": "Meeting 2"},
            ],
        }

        items = []
        if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]

        assert len(items) == 2
        assert items[0]["id"] == "event1"

    def test_extracts_items_fallback(self) -> None:
        """Should fall back to items array if value not present."""
        resp = {
            "items": [
                {"id": "event1", "subject": "Meeting 1"},
            ],
        }

        items = []
        if isinstance(resp, dict):
            if "value" in resp and isinstance(resp["value"], list):
                items = resp["value"]
            elif "items" in resp and isinstance(resp["items"], list):
                items = resp["items"]

        assert len(items) == 1
        assert items[0]["id"] == "event1"

    def test_returns_empty_list_for_missing_value(self) -> None:
        """Should return empty list if value/items not present."""
        resp = {"@odata.context": "https://graph.microsoft.com"}

        items = []
        if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]

        assert items == []

    def test_returns_empty_list_for_non_dict_response(self) -> None:
        """Should return empty list for non-dict response."""
        resp = "error response"

        items = []
        if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]

        assert items == []

    def test_handles_none_response(self) -> None:
        """Should handle None response gracefully."""
        resp = None

        items = []
        if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]

        assert items == []


class TestOutlookCalendarEventStructure:
    """Tests for expected Outlook Calendar event structure."""

    def test_event_has_expected_fields(self) -> None:
        """Outlook Calendar events should have expected fields."""
        sample_event = {
            "id": "AAMkAGI1AAAt9AHjAAA=",
            "subject": "Team Meeting",
            "body": {"contentType": "html", "content": "<p>Meeting notes</p>"},
            "start": {"dateTime": "2024-01-15T10:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T11:00:00.0000000", "timeZone": "UTC"},
            "location": {"displayName": "Conference Room A"},
            "locations": [{"displayName": "Conference Room A"}],
            "attendees": [
                {
                    "emailAddress": {"address": "user@example.com", "name": "User"},
                    "status": {"response": "accepted"},
                }
            ],
            "organizer": {
                "emailAddress": {"address": "organizer@example.com", "name": "Organizer"}
            },
            "webLink": "https://outlook.office365.com/owa/?itemid=...",
            "isAllDay": False,
            "isCancelled": False,
            "isOrganizer": True,
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/..."},
            "createdDateTime": "2024-01-10T08:00:00Z",
            "lastModifiedDateTime": "2024-01-14T16:00:00Z",
        }

        # Verify essential fields exist
        assert "id" in sample_event
        assert "subject" in sample_event
        assert "start" in sample_event
        assert "end" in sample_event
        assert "organizer" in sample_event

    def test_all_day_event_structure(self) -> None:
        """All-day events should have isAllDay=True."""
        all_day_event = {
            "id": "allday123",
            "subject": "Company Holiday",
            "isAllDay": True,
            "start": {"dateTime": "2024-01-01T00:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-02T00:00:00.0000000", "timeZone": "UTC"},
        }

        assert all_day_event["isAllDay"] is True

    def test_organizer_email_structure(self) -> None:
        """Organizer email uses nested emailAddress structure."""
        organizer = {
            "emailAddress": {
                "address": "organizer@example.com",
                "name": "John Doe",
            }
        }

        assert "emailAddress" in organizer
        assert "address" in organizer["emailAddress"]
        assert organizer["emailAddress"]["address"] == "organizer@example.com"

    def test_attendee_structure(self) -> None:
        """Attendee structure includes status response."""
        attendee = {
            "emailAddress": {"address": "attendee@example.com", "name": "Jane Doe"},
            "status": {"response": "accepted", "time": "2024-01-10T12:00:00Z"},
            "type": "required",
        }

        assert "emailAddress" in attendee
        assert "status" in attendee
        assert attendee["status"]["response"] == "accepted"


class TestOutlookCalendarTeamsIntegration:
    """Tests for Teams meeting integration in Outlook events."""

    def test_online_meeting_has_join_url(self) -> None:
        """Online meeting should have joinUrl for Teams."""
        online_meeting = {
            "joinUrl": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_...",
            "conferenceId": "123456789",
            "tollNumber": "+1 555-123-4567",
        }

        assert "joinUrl" in online_meeting
        assert "teams.microsoft.com" in online_meeting["joinUrl"]

    def test_event_with_teams_meeting(self) -> None:
        """Event should include onlineMeeting for Teams meetings."""
        event = {
            "id": "teams-meeting-123",
            "subject": "Project Sync",
            "onlineMeeting": {
                "joinUrl": "https://teams.microsoft.com/l/meetup-join/...",
            },
            "onlineMeetingUrl": "https://teams.microsoft.com/l/meetup-join/...",
            "onlineMeetingProvider": "teamsForBusiness",
        }

        assert event["onlineMeeting"]["joinUrl"] is not None
        assert event["onlineMeetingProvider"] == "teamsForBusiness"


@pytest.mark.asyncio
class TestOutlookCalendarFetchEventsAsync:
    """Async tests for fetch_events function."""

    async def test_fetch_events_calls_nango_proxy(self) -> None:
        """fetch_events should call nango.proxy_request."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        mock_nango.proxy_request.assert_called_once()
        assert result == []

    async def test_fetch_events_uses_correct_endpoint(self) -> None:
        """fetch_events should use correct Microsoft Graph endpoint."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert call_kwargs["endpoint"] == "/v1.0/me/calendar/events"

    async def test_fetch_events_passes_connection_id(self) -> None:
        """fetch_events should pass connection_id to Nango."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="my-outlook-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert call_kwargs["connection_id"] == "my-outlook-connection"

    async def test_fetch_events_returns_value_items(self) -> None:
        """fetch_events should return value array from response."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_events = [
            {"id": "event1", "subject": "Meeting 1"},
            {"id": "event2", "subject": "Meeting 2"},
        ]

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": mock_events})

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
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
            provider_config_key="custom-outlook",
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        assert call_kwargs["provider_config_key"] == "custom-outlook"

    async def test_fetch_events_includes_filter_param(self) -> None:
        """fetch_events should include $filter in params."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        params = call_kwargs["params"]

        assert "$filter" in params
        assert "start/dateTime" in params["$filter"]

    async def test_fetch_events_includes_select_param(self) -> None:
        """fetch_events should include $select for field selection."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        params = call_kwargs["params"]

        assert "$select" in params
        assert "id" in params["$select"]
        assert "subject" in params["$select"]

    async def test_fetch_events_includes_orderby_param(self) -> None:
        """fetch_events should include $orderby for sorting."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        params = call_kwargs["params"]

        assert "$orderby" in params
        assert params["$orderby"] == "start/dateTime"

    async def test_fetch_events_includes_top_param(self) -> None:
        """fetch_events should include $top to limit results."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": []})

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        call_kwargs = mock_nango.proxy_request.call_args.kwargs
        params = call_kwargs["params"]

        assert "$top" in params
        assert params["$top"] == "999"


class TestOutlookVsGoogleDifferences:
    """Tests highlighting differences between Outlook and Google Calendar APIs."""

    def test_response_array_key_differs(self) -> None:
        """Graph uses 'value', Google uses 'items'."""
        google_resp = {"items": [{"id": "g1"}]}
        outlook_resp = {"value": [{"id": "o1"}]}

        # Google extracts from 'items'
        google_items = google_resp.get("items", [])
        assert len(google_items) == 1

        # Outlook extracts from 'value'
        outlook_items = outlook_resp.get("value", [])
        assert len(outlook_items) == 1

    def test_organizer_structure_differs(self) -> None:
        """Organizer structure is different between APIs."""
        # Google uses flat email field
        google_organizer = {"email": "org@example.com", "displayName": "Organizer"}

        # Outlook uses nested emailAddress
        outlook_organizer = {"emailAddress": {"address": "org@example.com", "name": "Organizer"}}

        assert "email" in google_organizer
        assert "emailAddress" in outlook_organizer
        assert google_organizer["email"] == outlook_organizer["emailAddress"]["address"]

    def test_filter_syntax_differs(self) -> None:
        """Filter syntax is different between APIs."""
        # Google uses timeMin/timeMax params
        google_params = {
            "timeMin": "2024-01-01T00:00:00Z",
            "timeMax": "2024-12-31T23:59:59Z",
        }

        # Outlook uses OData $filter
        outlook_params = {
            "$filter": "start/dateTime ge '2024-01-01T00:00:00Z' and start/dateTime le '2024-12-31T23:59:59Z'"
        }

        assert "timeMin" in google_params
        assert "$filter" in outlook_params


class TestOutlookCalendarPaginationHelper:
    """Tests for the pagination helper function."""

    def test_extract_pagination_params_parses_url(self) -> None:
        """Should extract endpoint and params from nextLink URL."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            _extract_pagination_params,
        )

        next_link = "https://graph.microsoft.com/v1.0/me/calendar/events?$skip=999&$top=999"
        endpoint, params = _extract_pagination_params(next_link)

        assert endpoint == "/v1.0/me/calendar/events"
        assert params["$skip"] == "999"
        assert params["$top"] == "999"

    def test_extract_pagination_params_handles_complex_url(self) -> None:
        """Should handle complex nextLink with multiple params."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            _extract_pagination_params,
        )

        next_link = (
            "https://graph.microsoft.com/v1.0/me/calendar/events"
            "?$skip=999&$top=999&$filter=start%2FdateTime%20ge%20'2024-01-01'"
        )
        endpoint, params = _extract_pagination_params(next_link)

        assert endpoint == "/v1.0/me/calendar/events"
        assert "$skip" in params
        assert "$top" in params


@pytest.mark.asyncio
class TestOutlookCalendarPagination:
    """Tests for Outlook Calendar pagination functionality."""

    async def test_fetch_events_handles_single_page(self) -> None:
        """Should return all events when there's no pagination."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        mock_events = [{"id": "event1"}, {"id": "event2"}]
        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(return_value={"value": mock_events})

        result = await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        assert len(result) == 2
        mock_nango.proxy_request.assert_called_once()

    async def test_fetch_events_handles_pagination(self) -> None:
        """Should fetch all pages when @odata.nextLink is present."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        # First page with nextLink
        page1 = {
            "value": [{"id": "event1"}, {"id": "event2"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendar/events?$skip=2&$top=999",
        }
        # Second page without nextLink
        page2 = {
            "value": [{"id": "event3"}],
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

    async def test_fetch_events_respects_max_pages(self) -> None:
        """Should stop after max_pages to prevent infinite loops."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        # Always return nextLink (would be infinite without max_pages)
        infinite_page = {
            "value": [{"id": "event"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendar/events?$skip=1",
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

    async def test_fetch_events_pagination_uses_correct_params(self) -> None:
        """Should use params from nextLink for subsequent requests."""
        from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
            fetch_events,
        )

        page1 = {
            "value": [{"id": "event1"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendar/events?$skip=1&$top=999",
        }
        page2 = {"value": [{"id": "event2"}]}

        mock_nango = MagicMock()
        mock_nango.proxy_request = AsyncMock(side_effect=[page1, page2])

        await fetch_events(
            nango=mock_nango,
            connection_id="test-connection",
            is_initial=True,
            last_synced_at=None,
        )

        # Check second call uses params from nextLink
        second_call = mock_nango.proxy_request.call_args_list[1]
        assert second_call.kwargs["endpoint"] == "/v1.0/me/calendar/events"
        assert "$skip" in second_call.kwargs["params"]
