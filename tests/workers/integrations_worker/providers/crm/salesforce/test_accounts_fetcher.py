from __future__ import annotations

import logging
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


async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
    return await request_fn("access-token")


def _mock_client_with_responses(responses: list[httpx.Response]) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@pytest.mark.asyncio
async def test_fetcher_describes_selectable_account_fields_and_paginates() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Account/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "type": "id"},
                    {"name": "Name", "type": "string"},
                    {"name": "Website", "type": "url"},
                    {"name": "BillingStreet", "type": "string"},
                    {"name": "BillingAddress", "type": "address"},
                    {"name": "Unused__c", "type": "string"},
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

    client = _mock_client_with_responses(responses)

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
    assert "Unused__c" in query_soql
    assert "Deleted_Field__c" not in query_soql
    assert "Secret__c" not in query_soql


@pytest.mark.asyncio
async def test_fetch_all_logs_account_query_progress(caplog: pytest.LogCaptureFixture) -> None:
    token_info = _token_info()
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Account/describe",
            ),
            json={
                "fields": [
                    {"name": "Id", "type": "id"},
                    {"name": "Name", "type": "string"},
                    {"name": "Custom_Field__c", "type": "string"},
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
    client = _mock_client_with_responses(responses)

    with (
        caplog.at_level(logging.INFO),
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
            token_info=token_info,
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["Id"] for row in rows] == ["001A", "001B"]
    account_log_records = [
        record
        for record in caplog.records
        if record.integration_id == str(token_info.integration_id)
    ]
    assert [record.message for record in account_log_records] == [
        "Salesforce Account describe started",
        "Salesforce Account describe completed",
        "Salesforce Account query started",
        "Fetching Salesforce accounts page",
        "Fetched Salesforce accounts page",
        "Fetching Salesforce accounts page",
        "Fetched Salesforce accounts page",
        "Fetched Salesforce accounts",
    ]
    assert account_log_records[1].field_count == 5
    assert account_log_records[3].page == 1
    assert account_log_records[3].has_next_page is False
    assert account_log_records[4].page == 1
    assert account_log_records[4].page_records == 1
    assert account_log_records[4].total_records == 1
    assert account_log_records[4].has_next_page is True
    assert account_log_records[5].page == 2
    assert account_log_records[5].has_next_page is True
    assert account_log_records[6].page == 2
    assert account_log_records[6].has_next_page is False


@pytest.mark.asyncio
async def test_fetcher_fetch_one_queries_by_id() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Account/describe",
            ),
            json={"fields": [{"name": "Id", "type": "id"}, {"name": "Name", "type": "string"}]},
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={"records": [{"Id": "001xx0000000001AAA", "Name": "Acme"}]},
        ),
    ]
    client = _mock_client_with_responses(responses)

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
        record = await SalesforceAccountsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_one("001xx0000000001'AAA")

    assert record is not None
    assert record["Id"] == "001xx0000000001AAA"
    query_soql = client.get.call_args_list[1].kwargs["params"]["q"]
    assert "WHERE Id = '001xx0000000001\\'AAA'" in query_soql
    assert "LIMIT 1" in query_soql


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

    client = _mock_client_with_responses(responses)

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


@pytest.mark.asyncio
async def test_get_json_includes_salesforce_error_body_in_http_status_error() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        text='[{"errorCode":"INVALID_FIELD","message":"No such column Website"}]',
    )
    client = _mock_client_with_responses([response])

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await SalesforceAccountsFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )._get_json("https://acme.my.salesforce.com/services/data/v61.0/query")

    assert "INVALID_FIELD" in str(exc_info.value)
    assert "No such column Website" in str(exc_info.value)
