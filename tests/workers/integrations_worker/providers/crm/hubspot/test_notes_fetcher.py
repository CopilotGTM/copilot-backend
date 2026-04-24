from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher import HubSpotNotesFetcher


def _token_info() -> IntegrationTokenInfo:
    return IntegrationTokenInfo(
        access_token="access-token",
        refresh_token="refresh-token",
        integration_type=IntegrationType.HUBSPOT,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_fetcher_paginates_hubspot_notes() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request("GET", "https://api.hubapi.com/crm/v3/objects/notes"),
            json={
                "results": [{"id": "note-1"}],
                "paging": {"next": {"after": "cursor-2"}},
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request("GET", "https://api.hubapi.com/crm/v3/objects/notes"),
            json={"results": [{"id": "note-2"}]},
        ),
    ]

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await HubSpotNotesFetcher(token_info=_token_info()).fetch_all()

    assert [row["id"] for row in rows] == ["note-1", "note-2"]


@pytest.mark.asyncio
async def test_fetcher_raises_when_hubspot_notes_results_is_not_a_list() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://api.hubapi.com/crm/v3/objects/notes"),
        json={"results": {"bad": "shape"}},
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = HubSpotNotesFetcher(token_info=_token_info())
        with pytest.raises(ValueError, match="non-list `results`"):
            await fetcher.fetch_all()
