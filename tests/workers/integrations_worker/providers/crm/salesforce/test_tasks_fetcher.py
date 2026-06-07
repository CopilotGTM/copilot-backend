from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher import (
    SalesforceTasksFetcher,
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
async def test_fetcher_paginates_salesforce_tasks() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [{"Id": "00T1", "Subject": "First task"}],
                "nextRecordsUrl": "/services/data/v61.0/query/01gNEXT",
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/query/01gNEXT",
            ),
            json={"records": [{"Id": "00T2", "Subject": "Second task"}]},
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
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["Id"] for row in rows] == ["00T1", "00T2"]


@pytest.mark.asyncio
async def test_fetcher_scopes_initial_sync_tasks_to_eligible_deals() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={"records": [{"Id": "00T1", "WhatId": "006A"}]},
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all(deal_external_ids=["006A", "006B"])

    assert [row["Id"] for row in rows] == ["00T1"]
    soql = client.get.call_args.kwargs["params"]["q"]
    assert "WHERE IsDeleted = false AND WhatId IN ('006A', '006B')" in soql


@pytest.mark.asyncio
async def test_fetcher_does_not_fetch_all_tasks_for_empty_initial_sync_scope() -> None:
    client = AsyncMock()

    with patch(
        "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
        return_value=client,
    ):
        rows = await SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all(deal_external_ids=[])

    assert rows == []
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_one_escapes_task_id_and_returns_first_record() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={"records": [{"Id": "00Txx0000000001AAA", "Subject": "Follow up"}]},
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        row = await SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_one("00Txx0000000001AAA' OR Name LIKE '%")

    assert row == {"Id": "00Txx0000000001AAA", "Subject": "Follow up"}
    assert (
        "WHERE Id = '00Txx0000000001AAA\\' OR Name LIKE \\'%' LIMIT 1"
        in (client.get.call_args.kwargs["params"]["q"])
    )


@pytest.mark.asyncio
async def test_fetch_one_returns_none_when_task_is_missing() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={"records": []},
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        row = await SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_one("00Txx0000000001AAA")

    assert row is None


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
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )
        with pytest.raises(ValueError, match="non-list records"):
            await fetcher.fetch_all()


@pytest.mark.asyncio
async def test_fetcher_retries_transient_salesforce_timeout() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={"records": [{"Id": "00T1", "Subject": "Follow up"}]},
    )
    attempts = 0

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.TimeoutException("Salesforce query timed out")
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceTasksFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["Id"] for row in rows] == ["00T1"]
    assert attempts == 2
