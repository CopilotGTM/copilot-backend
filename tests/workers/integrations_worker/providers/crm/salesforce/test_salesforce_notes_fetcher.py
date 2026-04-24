from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher import (
    SalesforceNotesFetcher,
)


def _token_info() -> IntegrationTokenInfo:
    return IntegrationTokenInfo(
        access_token="access-token",
        refresh_token="refresh-token",
        integration_type=IntegrationType.SALESFORCE,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_fetcher_queries_links_then_hydrates_salesforce_notes() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [
                    {
                        "ContentDocumentId": "069NOTE",
                        "LinkedEntityId": "006OPPTY",
                    },
                    {
                        "ContentDocumentId": "069NOTE",
                        "LinkedEntityId": "003CONTACT",
                    },
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [
                    {
                        "Id": "069NOTE",
                        "Title": "QBR summary",
                        "Content": "Talked about renewal and pricing.",
                        "OwnerId": "005OWNER",
                        "CreatedDate": "2026-04-20T10:00:00.000+0000",
                        "LastModifiedDate": "2026-04-21T12:00:00.000+0000",
                    }
                ]
            },
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
            "src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceNotesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all(linked_entity_ids=["006OPPTY", "003CONTACT"])

    assert len(rows) == 2
    assert rows[0]["ContentDocumentId"] == "069NOTE"
    assert rows[0]["Title"] == "QBR summary"


@pytest.mark.asyncio
async def test_fetcher_raises_when_salesforce_query_returns_non_list_records() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={"records": {"bad": "shape"}},
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforceNotesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )
        with pytest.raises(ValueError, match="non-list records"):
            await fetcher.fetch_all(linked_entity_ids=["006OPPTY"])
