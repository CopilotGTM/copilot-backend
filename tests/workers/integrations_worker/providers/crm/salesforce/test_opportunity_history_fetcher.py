from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.opportunity_history_fetcher import (
    SalesforceOpportunityHistoryFetcher,
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
async def test_fetcher_paginates_salesforce_opportunity_history() -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query"
            ),
            json={
                "records": [{"OpportunityId": "006A", "StageName": "Prospecting"}],
                "nextRecordsUrl": "/services/data/v61.0/query/01gNEXT",
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "GET", "https://acme.my.salesforce.com/services/data/v61.0/query/01gNEXT"
            ),
            json={"records": [{"OpportunityId": "006A", "StageName": "Closed Won"}]},
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
            "src.workers.integrations_worker.providers.crm.salesforce.opportunity_history_fetcher.call_with_token_retry",
            side_effect=_fake_retry,
        ),
        patch(
            "src.workers.integrations_worker.providers.crm.salesforce.opportunity_history_fetcher.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        rows = await SalesforceOpportunityHistoryFetcher(
            token_info=_token_info(),
            instance_url="https://acme.my.salesforce.com",
        ).fetch_all()

    assert [row["StageName"] for row in rows] == ["Prospecting", "Closed Won"]
