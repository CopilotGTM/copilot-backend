from unittest.mock import AsyncMock, MagicMock

import pytest
from src.workers.integrations_worker.providers.calendar import outlook_calendar


@pytest.mark.asyncio
async def test_fetch_events_hydrates_missing_icaluid_from_event_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outlook_calendar, "get_access_token", AsyncMock(return_value="token"))

    initial_response = {
        "value": [
            {
                "id": "occ-1",
                "type": "occurrence",
                "seriesMasterId": "master-1",
                "start": {"dateTime": "2026-03-24T10:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": "2026-03-24T10:30:00Z", "timeZone": "UTC"},
            }
        ],
        "@odata.deltaLink": "delta-link",
    }
    detailed_event = {
        "id": "occ-1",
        "iCalUId": "uid-occ-1",
        "subject": "Recovered meeting",
        "start": {"dateTime": "2026-03-24T10:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-24T10:30:00Z", "timeZone": "UTC"},
    }
    graph_api_request = AsyncMock(side_effect=[initial_response, detailed_event])
    monkeypatch.setattr(outlook_calendar, "graph_api_request", graph_api_request)
    monkeypatch.setattr(outlook_calendar, "graph_api_request_url", AsyncMock())

    result = await outlook_calendar.fetch_events(
        nango=MagicMock(),
        connection_id="conn-1",
        is_initial=True,
        last_synced_at=None,
    )

    assert result.delta_link == "delta-link"
    assert result.events[0]["iCalUId"] == "uid-occ-1"
    endpoints = [call.args[1] for call in graph_api_request.await_args_list]
    assert endpoints == ["/v1.0/me/calendarView/delta", "/v1.0/me/events/occ-1"]


@pytest.mark.asyncio
async def test_fetch_events_leaves_event_unhydrated_when_occurrence_detail_still_lacks_icaluid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outlook_calendar, "get_access_token", AsyncMock(return_value="token"))

    initial_response = {
        "value": [
            {
                "id": "occ-2",
                "type": "occurrence",
                "seriesMasterId": "master-2",
                "originalStart": "2026-03-24T10:00:00Z",
                "start": {"dateTime": "2026-03-24T11:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": "2026-03-24T11:30:00Z", "timeZone": "UTC"},
            }
        ],
        "@odata.deltaLink": "delta-link",
    }
    occurrence_detail = {
        "id": "occ-2",
        "type": "occurrence",
        "seriesMasterId": "master-2",
        "start": {"dateTime": "2026-03-24T11:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-24T11:30:00Z", "timeZone": "UTC"},
    }
    graph_api_request = AsyncMock(side_effect=[initial_response, occurrence_detail])
    monkeypatch.setattr(outlook_calendar, "graph_api_request", graph_api_request)
    monkeypatch.setattr(outlook_calendar, "graph_api_request_url", AsyncMock())

    result = await outlook_calendar.fetch_events(
        nango=MagicMock(),
        connection_id="conn-1",
        is_initial=True,
        last_synced_at=None,
    )

    event = result.events[0]
    assert "iCalUId" not in event
    assert event["id"] == "occ-2"
    assert event["seriesMasterId"] == "master-2"
    assert event["originalStart"] == "2026-03-24T10:00:00Z"
    endpoints = [call.args[1] for call in graph_api_request.await_args_list]
    assert endpoints == [
        "/v1.0/me/calendarView/delta",
        "/v1.0/me/events/occ-2",
    ]
