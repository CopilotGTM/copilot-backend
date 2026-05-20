"""Tests for HubSpot changed-object search fetcher."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
    HubSpotChangedObjectsSearchFetcher,
)


def _token_info() -> SimpleNamespace:
    return SimpleNamespace(integration_id="integration-1")


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://x.test"))


@pytest.mark.parametrize("object_type", ["deals", "companies", "contacts", "tasks", "notes"])
def test_fetcher_supports_core_incremental_objects(object_type: str) -> None:
    """Regression test for all object families requested by core incremental sync."""

    fetcher = HubSpotChangedObjectsSearchFetcher(
        token_info=_token_info(),
        object_type=object_type,
        modified_since=datetime(2026, 5, 18, 10, tzinfo=UTC),
    )

    assert fetcher.object_type == object_type


@pytest.mark.asyncio
async def test_fetch_changed_deals_uses_hs_lastmodifieddate_search(monkeypatch) -> None:
    """Regression test for fetch changed deals uses hs lastmodifieddate search."""

    calls: list[dict] = []

    async def fake_call_with_token_retry(token_info, request_fn):  # noqa: ANN001
        """Execute fake call with token retry."""

        response = await request_fn("token-1")
        calls.append(response.request.extensions["json"])
        return _response(
            {
                "results": [
                    {
                        "id": "123",
                        "updatedAt": "2026-05-18T10:06:43.000Z",
                        "properties": {"hs_lastmodifieddate": "2026-05-18T10:06:43.000Z"},
                    }
                ]
            }
        )

    async def fake_post(self, url, *, headers, json):  # noqa: ANN001
        """Execute fake post."""

        request = httpx.Request("POST", url)
        request.extensions["json"] = json
        return httpx.Response(200, request=request, json={})

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await HubSpotChangedObjectsSearchFetcher(
        token_info=_token_info(),
        object_type="deals",
        modified_since=datetime(2026, 5, 18, 10, tzinfo=UTC),
    ).fetch_all()

    assert results == [
        ChangedHubSpotObject(
            object_id="123",
            source_modified_at=datetime(2026, 5, 18, 10, 6, 43, tzinfo=UTC),
            raw={
                "id": "123",
                "updatedAt": "2026-05-18T10:06:43.000Z",
                "properties": {"hs_lastmodifieddate": "2026-05-18T10:06:43.000Z"},
            },
        )
    ]
    assert calls == [
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GTE",
                            "value": "2026-05-18T10:00:00+00:00",
                        }
                    ]
                }
            ],
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            "properties": ["hs_lastmodifieddate"],
            "limit": 100,
        }
    ]


@pytest.mark.asyncio
async def test_fetch_changed_contacts_uses_lastmodifieddate_property(monkeypatch) -> None:
    """Regression test for fetch changed contacts uses lastmodifieddate property."""

    calls: list[dict] = []

    async def fake_call_with_token_retry(token_info, request_fn):  # noqa: ANN001
        """Execute fake call with token retry."""

        response = await request_fn("token-1")
        calls.append(response.request.extensions["json"])
        return _response(
            {
                "results": [
                    {
                        "id": "456",
                        "properties": {
                            "lastmodifieddate": "2026-05-18T10:07:00Z",
                            "hs_lastmodifieddate": "2026-05-18T10:06:00Z",
                        },
                    }
                ]
            }
        )

    async def fake_post(self, url, *, headers, json):  # noqa: ANN001
        """Execute fake post."""

        request = httpx.Request("POST", url)
        request.extensions["json"] = json
        return httpx.Response(200, request=request, json={})

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await HubSpotChangedObjectsSearchFetcher(
        token_info=_token_info(),
        object_type="contacts",
        modified_since=datetime(2026, 5, 18, 10, tzinfo=UTC),
    ).fetch_all()

    assert calls[0]["filterGroups"][0]["filters"][0]["propertyName"] == "lastmodifieddate"
    assert calls[0]["properties"] == ["lastmodifieddate", "hs_lastmodifieddate"]
    assert results[0].object_id == "456"
    assert results[0].source_modified_at == datetime(2026, 5, 18, 10, 7, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fetch_changed_objects_paginates_with_after(monkeypatch) -> None:
    """Regression test for fetch changed objects paginates with after."""

    calls: list[dict] = []
    responses = [
        _response(
            {
                "results": [
                    {"id": "1", "properties": {"hs_lastmodifieddate": "2026-05-18T10:01:00Z"}}
                ],
                "paging": {"next": {"after": "next-page"}},
            }
        ),
        _response(
            {
                "results": [
                    {"id": "2", "properties": {"hs_lastmodifieddate": "2026-05-18T10:02:00Z"}}
                ],
            }
        ),
    ]

    async def fake_call_with_token_retry(token_info, request_fn):  # noqa: ANN001
        """Execute fake call with token retry."""

        response = await request_fn("token-1")
        calls.append(response.request.extensions["json"])
        return responses.pop(0)

    async def fake_post(self, url, *, headers, json):  # noqa: ANN001
        """Execute fake post."""

        request = httpx.Request("POST", url)
        request.extensions["json"] = json
        return httpx.Response(200, request=request, json={})

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await HubSpotChangedObjectsSearchFetcher(
        token_info=_token_info(),
        object_type="companies",
        modified_since=datetime(2026, 5, 18, 10, tzinfo=UTC),
    ).fetch_all()

    assert [r.object_id for r in results] == ["1", "2"]
    assert "after" not in calls[0]
    assert calls[1]["after"] == "next-page"


@pytest.mark.asyncio
async def test_fetch_changed_objects_rejects_malformed_results(monkeypatch) -> None:
    """Regression test for fetch changed objects rejects malformed results."""

    async def fake_call_with_token_retry(token_info, request_fn):  # noqa: ANN001
        """Execute fake call with token retry."""

        return _response({"results": {"id": "not-a-list"}})

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )

    with pytest.raises(ValueError, match="non-list `results`"):
        await HubSpotChangedObjectsSearchFetcher(
            token_info=_token_info(),
            object_type="deals",
            modified_since=datetime(2026, 5, 18, 10, tzinfo=UTC),
        ).fetch_all()


def test_fetcher_rejects_unsupported_object_type() -> None:
    """Regression test for fetcher rejects unsupported object type."""

    with pytest.raises(ValueError, match="Unsupported HubSpot search object type"):
        HubSpotChangedObjectsSearchFetcher(
            token_info=_token_info(),
            object_type="tickets",
            modified_since=datetime(2026, 5, 18, 10, tzinfo=UTC),
        )
