from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.single_opportunity_fetcher import (
    SalesforceSingleOpportunityFetcher,
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
async def test_single_opportunity_fetcher_queries_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_params: list[dict[str, str]] = []

    async def fake_get_json(self, url: str, *, params=None):  # noqa: ANN001
        if url.endswith("/describe"):
            return {
                "fields": [
                    {"name": "Id", "queryable": True, "type": "id"},
                    {"name": "Name", "queryable": True, "type": "string"},
                    {"name": "StageName", "queryable": True, "type": "string"},
                ]
            }
        seen_params.append(params)
        return {"records": [{"Id": "006xx0000000001AAA", "Name": "Deal"}]}

    monkeypatch.setattr(SalesforceSingleOpportunityFetcher, "_get_json", fake_get_json)

    record = await SalesforceSingleOpportunityFetcher(
        token_info=_token_info(),
        instance_url="https://acme.my.salesforce.com",
        opportunity_id="006xx0000000001AAA",
    ).fetch()

    assert record is not None
    assert record["Id"] == "006xx0000000001AAA"
    assert "WHERE Id = '006xx0000000001AAA'" in seen_params[0]["q"]
    assert "LIMIT 1" in seen_params[0]["q"]
