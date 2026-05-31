from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher import (
    SalesforceContactsFetcher,
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


def _mock_client_with_response(response: httpx.Response) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


def _mock_client_with_responses(responses: list[httpx.Response]) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@pytest.mark.asyncio
async def test_fetcher_describes_selectable_contact_fields_and_paginates() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Contact/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "type": "id"},
                    {"name": "FirstName", "type": "string"},
                    {"name": "Email", "type": "email"},
                    {"name": "MailingStreet", "type": "string"},
                    {"name": "MailingAddress", "type": "address"},
                    {"name": "Deleted_Field__c", "type": "string", "deprecatedAndHidden": True},
                    {"name": "Secret__c", "queryable": False, "type": "string"},
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": [{"Id": "003A", "FirstName": "Alex"}]},
        ),
    ]

    client = _mock_client_with_responses(responses)

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceContactsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["Id"] for row in rows] == ["003A"]
    query_soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert "Email" in query_soql
    assert "MailingStreet" in query_soql
    assert "MailingAddress" not in query_soql
    assert "Deleted_Field__c" not in query_soql
    assert "Secret__c" not in query_soql


@pytest.mark.asyncio
async def test_fetcher_fetch_one_returns_none_for_empty_result() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Contact/describe",
            ),
            json={"fields": [{"name": "Id", "type": "id"}, {"name": "Email", "type": "email"}]},
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
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        record = await SalesforceContactsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_one("003xx0000000001AAA")

    assert record is None
    query_soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert "WHERE Id = '003xx0000000001AAA'" in query_soql
    assert "LIMIT 1" in query_soql


@pytest.mark.asyncio
async def test_fetcher_raises_when_salesforce_contact_describe_returns_non_list_fields() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request(
            "GET", "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Contact/describe"
        ),
        json={"fields": {"bad": "shape"}},
    )

    client = _mock_client_with_response(response)

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforceContactsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )
        with pytest.raises(ValueError, match="non-list fields"):
            await fetcher.fetch_all()


@pytest.mark.asyncio
async def test_get_json_includes_salesforce_error_body_in_http_status_error() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        text='[{"errorCode":"INVALID_FIELD","message":"No such column Email"}]',
    )
    client = _mock_client_with_response(response)

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await SalesforceContactsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )._get_json("https://acme.my.salesforce.com/services/data/v61.0/query")

    assert "INVALID_FIELD" in str(exc_info.value)
    assert "No such column Email" in str(exc_info.value)
