"""Salesforce fetcher for one Opportunity snapshot."""

from __future__ import annotations

from typing import Any

from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher import (
    _API_VERSION,
    SalesforceOpportunitiesFetcher,
)


class SalesforceSingleOpportunityFetcher(SalesforceOpportunitiesFetcher):
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
        opportunity_id: str,
    ) -> None:
        super().__init__(token_info=token_info, instance_url=instance_url)
        self.opportunity_id = opportunity_id

    async def fetch(self) -> dict[str, Any] | None:
        field_names = await self._fetch_field_names()
        soql = (
            f"SELECT {', '.join(field_names)} FROM Opportunity "  # noqa: S608  # nosec B608
            f"WHERE Id = '{_escape_soql_literal(self.opportunity_id)}' LIMIT 1"
        )
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        payload = await self._get_json(url, params={"q": soql})
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Salesforce single Opportunity query API returned non-list records")
        if not raw_records:
            return None
        record = raw_records[0]
        if not isinstance(record, dict):
            raise ValueError("Salesforce single Opportunity query API returned non-dict record")
        return record


def _escape_soql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
