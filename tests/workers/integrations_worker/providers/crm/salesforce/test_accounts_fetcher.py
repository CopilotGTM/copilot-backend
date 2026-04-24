from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher import (
    SalesforceAccountsFetcher,
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
async def test_fetcher_describes_queryable_account_fields_and_paginates() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Account/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "queryable": True, "type": "id"},
                    {"name": "Name", "queryable": True, "type": "string"},
                    {"name": "Website", "queryable": True, "type": "url"},
                    {"name": "BillingStreet", "queryable": True, "type": "string"},
                    {"name": "BillingAddress", "queryable": True, "type": "address"},
                    {"name": "Secret__c", "queryable": False, "type": "string"},
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [{"Id": "001A", "Name": "Acme"}],
                "nextRecordsUrl": "/services/data/v61.0/query/01gNEXT",
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/query/01gNEXT",
            ),
            json={"records": [{"Id": "001B", "Name": "Beta"}]},
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
            "src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceAccountsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["Id"] for row in rows] == ["001A", "001B"]
    query_soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert "Name" in query_soql
    assert "Website" in query_soql
    assert "BillingStreet" in query_soql
    assert "BillingAddress" not in query_soql
    assert "Secret__c" not in query_soql


@pytest.mark.asyncio
async def test_fetcher_raises_when_salesforce_account_query_returns_non_list_records() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Account/describe",
            ),
            json={"fields": [{"name": "Id", "queryable": True, "type": "id"}]},
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": {"bad": "shape"}},
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
            "src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforceAccountsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )
        with pytest.raises(ValueError, match="non-list records"):
            await fetcher.fetch_all()
