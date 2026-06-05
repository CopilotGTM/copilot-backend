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
async def test_iter_batches_yields_first_note_batch_before_querying_all_link_chunks() -> None:
    linked_entity_ids = [f"006OPPTY{i:03d}" for i in range(201)]

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    async def _fake_get(url, *, headers, params=None):
        query = params["q"]
        request = httpx.Request("GET", url)
        if "FROM ContentDocumentLink" in query and linked_entity_ids[-1] in query:
            return httpx.Response(
                200,
                request=request,
                json={
                    "records": [
                        {
                            "ContentDocumentId": "069NOTE2",
                            "LinkedEntityId": linked_entity_ids[-1],
                        }
                    ]
                },
            )
        if "FROM ContentDocumentLink" in query:
            return httpx.Response(
                200,
                request=request,
                json={
                    "records": [
                        {
                            "ContentDocumentId": "069NOTE1",
                            "LinkedEntityId": linked_entity_ids[0],
                        }
                    ]
                },
            )
        records = []
        if "069NOTE1" in query:
            records.append(
                {
                    "Id": "069NOTE1",
                    "Title": "First batch note",
                    "Content": "Persist this before querying every link chunk.",
                    "OwnerId": "005OWNER",
                }
            )
        if "069NOTE2" in query:
            records.append(
                {
                    "Id": "069NOTE2",
                    "Title": "Second batch note",
                    "Content": "This should not be fetched before the first yield.",
                    "OwnerId": "005OWNER",
                }
            )
        return httpx.Response(200, request=request, json={"records": records})

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_fake_get)
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
        batches = SalesforceNotesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).iter_batches(linked_entity_ids=linked_entity_ids)
        first_batch = await batches.__anext__()

    assert len(first_batch) == 1
    assert first_batch[0]["Id"] == "069NOTE1"
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_fetch_one_by_note_id_hydrates_note_with_scoped_links() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [
                    {
                        "Id": "06Axx0000000002AAA",
                        "ContentDocumentId": "069xx0000000001AAA",
                        "LinkedEntityId": "006xx0000000002AAA",
                    }
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
                        "Id": "069xx0000000001AAA",
                        "Title": "QBR summary",
                        "Content": "Talked about renewal.",
                        "OwnerId": "005OWNER",
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
        ).fetch_one_by_note_id(
            "069xx0000000001AAA' OR Title LIKE '%",
            linked_entity_ids=["006xx0000000002AAA"],
        )

    assert len(rows) == 1
    assert rows[0]["Id"] == "069xx0000000001AAA"
    assert rows[0]["ContentDocumentLinkId"] == "06Axx0000000002AAA"
    assert rows[0]["LinkedEntityId"] == "006xx0000000002AAA"
    first_query = client.get.call_args_list[0].kwargs["params"]["q"]
    assert "ContentDocumentId = '069xx0000000001AAA\\' OR Title LIKE \\'%'" in first_query


@pytest.mark.asyncio
async def test_fetch_one_by_link_id_returns_empty_for_out_of_scope_link() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={
            "records": [
                {
                    "Id": "06Axx0000000001AAA",
                    "ContentDocumentId": "069xx0000000001AAA",
                    "LinkedEntityId": "500xx0000000001AAA",
                }
            ]
        },
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
        rows = await SalesforceNotesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_one_by_link_id(
            "06Axx0000000001AAA",
            linked_entity_ids=["006xx0000000002AAA"],
        )

    assert rows == []
    assert client.get.await_count == 1


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


@pytest.mark.asyncio
async def test_fetcher_retries_transient_salesforce_network_error() -> None:
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
                    }
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
                        "Content": "Talked about renewal.",
                        "OwnerId": "005OWNER",
                    }
                ]
            },
        ),
    ]
    attempts = 0

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.NetworkError("Salesforce connection reset")
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
            "src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceNotesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all(linked_entity_ids=["006OPPTY"])

    assert len(rows) == 1
    assert rows[0]["Id"] == "069NOTE"
    assert attempts == 3
