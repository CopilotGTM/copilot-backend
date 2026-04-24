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


@pytest.mark.asyncio
async def test_fetcher_raises_when_salesforce_contact_role_query_returns_non_list_records() -> None:
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
