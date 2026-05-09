"""Salesforce fetcher for Opportunity record types, stage values, and stage metadata."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 60.0
_API_VERSION = "v61.0"


class SalesforcePipelinesFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_opportunity_record_types(self) -> list[dict[str, Any]]:
        describe_record_types = await self._fetch_opportunity_record_type_infos()
        active_custom_record_types = await self._fetch_active_custom_opportunity_record_types()
        records = _merge_record_type_metadata(
            describe_record_types=describe_record_types,
            active_custom_record_types=active_custom_record_types,
        )
        logger.info(
            "Fetched Salesforce Opportunity record types",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": len(records),
            },
        )
        return records

    async def _fetch_active_custom_opportunity_record_types(self) -> list[dict[str, Any]]:
        soql = (
            "SELECT Id, Name, DeveloperName, Description, IsActive "
            "FROM RecordType "
            "WHERE SobjectType = 'Opportunity' AND IsActive = true "
            "ORDER BY Name"
        )
        return await self._query_records(soql)

    async def _fetch_opportunity_record_type_infos(self) -> list[dict[str, Any]]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/sobjects/Opportunity/describe"
        payload = await self._get_json(url)
        record_type_infos = payload.get("recordTypeInfos")
        if not isinstance(record_type_infos, list):
            raise ValueError("Salesforce Opportunity describe returned non-list recordTypeInfos")
        return [row for row in record_type_infos if isinstance(row, dict)]

    async def fetch_stage_values_for_record_type(self, record_type_id: str) -> list[dict[str, Any]]:
        url = (
            f"{self.instance_url}/services/data/{_API_VERSION}/sobjects/Opportunity/"
            f"describe/layouts/{record_type_id}"
        )
        payload = await self._get_json(url)
        values = _extract_stage_value_items(payload)
        if not values:
            raise ValueError(
                f"Salesforce layout describe returned no usable StageName metadata for {record_type_id}"
            )
        logger.info(
            "Fetched Salesforce StageName values for record type",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "record_type_id": record_type_id,
                "count": len(values),
            },
        )
        return values

    async def fetch_opportunity_stage_metadata(self) -> list[dict[str, Any]]:
        soql = (
            "SELECT Id, MasterLabel, ApiName, SortOrder, DefaultProbability, "
            "IsClosed, IsWon, IsActive, ForecastCategory "
            "FROM OpportunityStage ORDER BY SortOrder"
        )
        records = await self._query_records(soql)
        logger.info(
            "Fetched Salesforce OpportunityStage metadata",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": len(records),
            },
        )
        return records

    async def _query_records(self, soql: str) -> list[dict[str, Any]]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        records: list[dict[str, Any]] = []
        next_url: str | None = None

        while True:
            payload = await self._get_json(
                next_url or url, params=None if next_url else {"q": soql}
            )
            page_records = payload.get("records")
            if not isinstance(page_records, list):
                raise ValueError("Salesforce query API returned non-list records")
            records.extend(row for row in page_records if isinstance(row, dict))

            next_records_url = payload.get("nextRecordsUrl")
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = (
                next_records_url
                if next_records_url.startswith("http")
                else f"{self.instance_url}{next_records_url}"
            )

        return records

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async def _request(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                _raise_for_status_with_salesforce_body(response)
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce API returned non-dict payload")
        return payload


def _extract_stage_value_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen_values: set[str] = set()

    for items in _walk_stage_value_collections(payload):
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value:
                continue
            if item.get("active") is False:
                continue
            label = item.get("label")
            if not isinstance(label, str) or not label:
                label = value
            if value in seen_values:
                continue
            seen_values.add(value)
            found.append({"value": value, "label": label})

    return found


def _merge_record_type_metadata(
    *,
    describe_record_types: list[dict[str, Any]],
    active_custom_record_types: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    described_by_id = {
        record_type_id: _record_type_from_describe_info(row)
        for row in describe_record_types
        for record_type_id in [_coerce_text(row.get("recordTypeId"))]
        if record_type_id
    }

    merged: list[dict[str, Any]] = []
    master_records = [row for row in described_by_id.values() if bool(row.get("IsMaster"))]
    merged.extend(master_records)

    for custom_record in active_custom_record_types:
        record_type_id = _coerce_text(custom_record.get("Id"))
        if not record_type_id:
            continue
        described = described_by_id.get(record_type_id, {})
        if bool(described.get("IsMaster")):
            continue
        merged.append({**described, **custom_record, "IsMaster": False})

    return merged


def _raise_for_status_with_salesforce_body(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text.strip()
        if body:
            message = f"{exc}\nSalesforce response body: {body[:500]}"
            raise httpx.HTTPStatusError(
                message,
                request=exc.request,
                response=exc.response,
            ) from exc
        raise


def _record_type_from_describe_info(row: dict[str, Any]) -> dict[str, Any]:
    record_type_id = _coerce_text(row.get("recordTypeId")) or ""
    label = _coerce_text(row.get("name")) or record_type_id
    developer_name = _coerce_text(row.get("developerName")) or label
    return {
        "Id": record_type_id,
        "Name": label,
        "DeveloperName": developer_name,
        "Description": None,
        "IsActive": True,
        "IsMaster": bool(row.get("master")),
        "IsDefault": bool(row.get("defaultRecordTypeMapping")),
        "IsAvailable": bool(row.get("available")),
    }


def _coerce_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _walk_stage_value_collections(node: Any) -> Iterable[list[dict[str, Any]]]:
    if isinstance(node, dict):
        field_name = node.get("name") or node.get("apiName") or node.get("fieldApiName")
        if field_name == "StageName":
            for key in ("valueListItems", "picklistValues", "values"):
                raw_items = node.get(key)
                if isinstance(raw_items, list):
                    yield raw_items
        for value in node.values():
            yield from _walk_stage_value_collections(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_stage_value_collections(item)
