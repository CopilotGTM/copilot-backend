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


async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
    return await request_fn("access-token")


def _mock_client_with_responses(responses: list[httpx.Response]) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


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

    client = _mock_client_with_responses(responses)

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
    assert "IsClosed" not in field_names


@pytest.mark.asyncio
async def test_fetcher_queries_only_described_opportunity_fields() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "type": "id"},
                    {"name": "Name", "type": "string"},
                    {"name": "Custom_Field__c", "type": "string"},
                    {"name": "Deleted_Field__c", "type": "string", "deprecatedAndHidden": True},
                    {"name": "Hidden_Field__c", "queryable": False, "type": "string"},
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": []},
        ),
    ]
    client = _mock_client_with_responses(responses)

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

    query_params = client.get.call_args_list[1].kwargs["params"]
    soql = query_params["q"]
    assert rows == []
    assert field_names == ["Custom_Field__c", "Id", "Name"]
    assert "CurrencyIsoCode" not in soql
    assert "StageName" not in soql
    assert "Custom_Field__c" in soql
    assert "Deleted_Field__c" not in soql
    assert "Hidden_Field__c" not in soql


@pytest.mark.asyncio
async def test_fetcher_omits_close_date_from_order_by_when_not_described() -> None:
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
                    {"name": "Name", "queryable": True, "type": "string"},
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": []},
        ),
    ]
    client = _mock_client_with_responses(responses)

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
        await SalesforceOpportunitiesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert "ORDER BY CloseDate" not in soql
    assert soql.endswith("ORDER BY Name, Id")


@pytest.mark.asyncio
async def test_fetcher_orders_by_id_when_only_id_is_described() -> None:
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
            json={"records": []},
        ),
    ]
    client = _mock_client_with_responses(responses)

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
        await SalesforceOpportunitiesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert soql == "SELECT Id FROM Opportunity ORDER BY Id"


@pytest.mark.asyncio
async def test_fetcher_keeps_id_when_describe_does_not_expose_queryable_id() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "queryable": False, "type": "id"},
                    {"name": "Name", "queryable": True, "type": "string"},
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": [{"Id": "006A", "Name": "Expansion"}]},
        ),
    ]
    client = _mock_client_with_responses(responses)

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

    soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert rows == [{"Id": "006A", "Name": "Expansion"}]
    assert field_names == ["Id", "Name"]
    assert soql == "SELECT Id, Name FROM Opportunity ORDER BY Name, Id"


@pytest.mark.asyncio
async def test_fetcher_keeps_id_when_describe_omits_id_field() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
            ),
            json={"fields": [{"name": "Name", "queryable": True, "type": "string"}]},
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": [{"Id": "006A", "Name": "Expansion"}]},
        ),
    ]
    client = _mock_client_with_responses(responses)

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

    soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert rows == [{"Id": "006A", "Name": "Expansion"}]
    assert field_names == ["Id", "Name"]
    assert soql == "SELECT Id, Name FROM Opportunity ORDER BY Name, Id"


@pytest.mark.asyncio
async def test_fetcher_includes_salesforce_error_body_in_http_status_error() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        text='[{"errorCode":"INVALID_FIELD","message":"No such column CurrencyIsoCode"}]',
    )
    client = _mock_client_with_responses([response])

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await SalesforceOpportunitiesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )._get_json("https://acme.my.salesforce.com/services/data/v61.0/query")

    assert "INVALID_FIELD" in str(exc_info.value)
    assert "No such column CurrencyIsoCode" in str(exc_info.value)


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

    client = _mock_client_with_responses(responses)

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
