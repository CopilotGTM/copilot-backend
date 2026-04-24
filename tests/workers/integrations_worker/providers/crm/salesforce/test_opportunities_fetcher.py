from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher import (
    SalesforceOpportunitiesFetcher,
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
async def test_fetcher_discovers_fields_and_paginates_opportunities() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "queryable": True, "type": "id"},
                    {"name": "Custom_Field__c", "queryable": True, "type": "string"},
                    {"name": "BillingAddress", "queryable": True, "type": "address"},
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [{"Id": "006A", "Name": "Big Deal"}],
                "nextRecordsUrl": "/services/data/v61.0/query/01gNEXT",
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query/01gNEXT"
            ),
            json={"records": [{"Id": "006B", "Name": "Expansion"}]},
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
            "src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows, field_names = await SalesforceOpportunitiesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["Id"] for row in rows] == ["006A", "006B"]
    assert "Custom_Field__c" in field_names
    assert "BillingAddress" not in field_names
    assert "IsClosed" in field_names


@pytest.mark.asyncio
async def test_fetcher_raises_when_salesforce_opportunity_query_returns_non_list_records() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
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
            "src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
        pytest.raises(ValueError, match="non-list records"),
    ):
        await SalesforceOpportunitiesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()
