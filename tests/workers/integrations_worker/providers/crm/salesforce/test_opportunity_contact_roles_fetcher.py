from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.opportunity_contact_roles_fetcher import (
    SalesforceOpportunityContactRolesFetcher,
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


@pytest.mark.asyncio
async def test_fetcher_raises_when_salesforce_contact_role_query_returns_non_list_records() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        json={"records": {"bad": "shape"}},
    )

    client = _mock_client_with_response(response)

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunity_contact_roles_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunity_contact_roles_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
        pytest.raises(ValueError, match="non-list records"),
    ):
        await SalesforceOpportunityContactRolesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()


@pytest.mark.asyncio
async def test_get_json_includes_salesforce_error_body_in_http_status_error() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("GET", "https://acme.my.salesforce.com/services/data/v61.0/query"),
        text='[{"errorCode":"INVALID_TYPE","message":"sObject type not supported"}]',
    )
    client = _mock_client_with_response(response)

    with (
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunity_contact_roles_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunity_contact_roles_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await SalesforceOpportunityContactRolesFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        )._get_json("https://acme.my.salesforce.com/services/data/v61.0/query")

    assert "INVALID_TYPE" in str(exc_info.value)
    assert "sObject type not supported" in str(exc_info.value)
