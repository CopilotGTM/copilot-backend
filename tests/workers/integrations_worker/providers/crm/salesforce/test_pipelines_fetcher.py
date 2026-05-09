from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher import (
    SalesforcePipelinesFetcher,
    _merge_record_type_metadata,
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
async def test_fetcher_returns_record_types_stage_values_and_stage_metadata() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
            ),
            json={
                "recordTypeInfos": [
                    {
                        "recordTypeId": "012000000000000AAA",
                        "name": "Master",
                        "developerName": "Master",
                        "master": True,
                    },
                    {
                        "recordTypeId": "012A",
                        "name": "Enterprise Sales",
                        "developerName": "Enterprise_Sales",
                        "master": False,
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
                        "Id": "012A",
                        "Name": "Enterprise Sales",
                        "DeveloperName": "Enterprise_Sales",
                        "Description": "Enterprise pipeline",
                        "IsActive": True,
                        "IsMaster": False,
                    }
                ]
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe/layouts/012A",
            ),
            json={
                "layouts": [
                    {
                        "detailLayoutSections": [
                            {
                                "layoutRows": [
                                    {
                                        "layoutItems": [
                                            {
                                                "layoutComponents": [
                                                    {
                                                        "valueList": {
                                                            "name": "StageName",
                                                            "valueListItems": [
                                                                {
                                                                    "value": "Prospecting",
                                                                    "label": "Prospecting",
                                                                },
                                                                {
                                                                    "value": "Closed Won",
                                                                    "label": "Closed Won",
                                                                },
                                                            ],
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
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
                        "Id": "OS1",
                        "MasterLabel": "Prospecting",
                        "ApiName": "Prospecting",
                        "SortOrder": 10,
                        "DefaultProbability": 20,
                        "IsClosed": False,
                        "IsWon": False,
                        "IsActive": True,
                        "ForecastCategory": "Pipeline",
                    },
                    {
                        "Id": "OS2",
                        "MasterLabel": "Closed Won",
                        "ApiName": "Closed Won",
                        "SortOrder": 99,
                        "DefaultProbability": 100,
                        "IsClosed": True,
                        "IsWon": True,
                        "IsActive": True,
                        "ForecastCategory": "Closed",
                    },
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
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforcePipelinesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )

        record_types = await fetcher.fetch_opportunity_record_types()
        stage_values = await fetcher.fetch_stage_values_for_record_type("012A")
        stage_metadata = await fetcher.fetch_opportunity_stage_metadata()

    assert [row["Id"] for row in record_types] == ["012000000000000AAA", "012A"]
    assert [row["value"] for row in stage_values] == ["Prospecting", "Closed Won"]
    assert [row["MasterLabel"] for row in stage_metadata] == ["Prospecting", "Closed Won"]


@pytest.mark.asyncio
async def test_fetcher_gets_master_record_type_from_describe_without_querying_ismaster() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET",
                "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe",
            ),
            json={
                "recordTypeInfos": [
                    {
                        "recordTypeId": "012000000000000AAA",
                        "name": "Master",
                        "developerName": "Master",
                        "master": True,
                    },
                    {
                        "recordTypeId": "012A",
                        "name": "Enterprise Sales",
                        "developerName": "Enterprise_Sales",
                        "master": False,
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
                        "Id": "012A",
                        "Name": "Enterprise Sales",
                        "DeveloperName": "Enterprise_Sales",
                        "Description": "Enterprise pipeline",
                        "IsActive": True,
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
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforcePipelinesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )

        record_types = await fetcher.fetch_opportunity_record_types()

    query_call = client.get.await_args_list[1]
    assert "IsMaster" not in query_call.kwargs["params"]["q"]
    assert [row["Id"] for row in record_types] == ["012000000000000AAA", "012A"]
    assert record_types[0]["IsMaster"] is True
    assert record_types[1]["IsMaster"] is False
    assert record_types[1]["Description"] == "Enterprise pipeline"


def test_merge_record_type_metadata_deduplicates_master_from_query_results() -> None:
    record_types = _merge_record_type_metadata(
        describe_record_types=[
            {
                "recordTypeId": "012000000000000AAA",
                "name": "Master",
                "developerName": "Master",
                "master": True,
            }
        ],
        active_custom_record_types=[
            {
                "Id": "012000000000000AAA",
                "Name": "Master",
                "DeveloperName": "Master",
                "IsActive": True,
            },
            {
                "Id": "012A",
                "Name": "Enterprise Sales",
                "DeveloperName": "Enterprise_Sales",
                "IsActive": True,
            },
        ],
    )

    assert [row["Id"] for row in record_types] == ["012000000000000AAA", "012A"]
    assert record_types[0]["IsMaster"] is True
    assert record_types[1]["IsMaster"] is False


@pytest.mark.asyncio
async def test_get_json_includes_salesforce_error_body_in_http_status_error() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json=[
            {
                "message": "No such column 'IsMaster' on entity 'RecordType'.",
                "errorCode": "INVALID_FIELD",
            }
        ],
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforcePipelinesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )

        with pytest.raises(httpx.HTTPStatusError, match="INVALID_FIELD"):
            await fetcher._get_json(
                "https://acme.my.salesforce.com/services/data/v61.0/query",
                params={"q": "SELECT IsMaster FROM RecordType"},
            )


@pytest.mark.asyncio
async def test_fetcher_raises_when_layout_has_no_usable_stagename_metadata() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request(
            "GET",
            "https://acme.my.salesforce.com/services/data/v61.0/sobjects/Opportunity/describe/layouts/012A",
        ),
        json={"layouts": [{"detailLayoutSections": []}]},
    )

    async def _fake_retry(token_info: IntegrationTokenInfo, request_fn):
        return await request_fn("access-token")

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.pipelines_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        fetcher = SalesforcePipelinesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )

        with pytest.raises(ValueError, match="StageName"):
            await fetcher.fetch_stage_values_for_record_type("012A")
