"""Salesforce fetcher for Account record sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
_MAX_PAGES = 500_000
_QUERY_BATCH_SIZE = 2_000
_MAX_ERROR_BODY_CHARS = 500
_REQUIRED_FIELDS = {"Id", "CreatedDate", "LastModifiedDate"}
_ACCOUNT_FIELD_ALLOWLIST = (
    "Id",
    "Name",
    "Account_Name__c",
    "Website",
    "Domain__c",
    "Description",
    "company_description_lusha_enriched__c",
    "Industry",
    "Cohort_Industry__c",
    "LinkedIn_Industry__c",
    "Nature_of_Business_Category__c",
    "Type",
    "RecordTypeId",
    "RecordType_name__c",
    "Customer_Type__c",
    "Account_Status__c",
    "Lead_Status__c",
    "Customer_Stage__c",
    "Region__c",
    "Geography__c",
    "Cluster__c",
    "Sub_Cluster__c",
    "Country__c",
    "OwnerId",
    "Account_CSM_Name__c",
    "ParentId",
    "NumberOfEmployees",
    "LinkedIn_No_of_Employees__c",
    "AnnualRevenue",
    "Annual_Revenue__c",
    "Phone",
    "BillingStreet",
    "BillingCity",
    "BillingState",
    "BillingCountry",
    "BillingPostalCode",
    "ShippingStreet",
    "ShippingCity",
    "ShippingState",
    "ShippingCountry",
    "CreatedDate",
    "LastModifiedDate",
    "SystemModstamp",
    "LastActivityDate",
    "IsDeleted",
    "Product__c",
    "Business_Unit__c",
    "Account_AutoID__c",
    "Account_Category__c",
    "LinkedIn_URL__c",
)


class SalesforceAccountsFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        async for page_records in self.fetch_pages():
            records.extend(page_records)
        return records

    async def fetch_pages(self) -> AsyncIterator[list[dict[str, Any]]]:
        field_names = await self._fetch_field_names()
        # The field list comes only from Salesforce describe metadata after
        # filtering out known non-selectable field shapes; no user input reaches this SOQL.
        soql = f"SELECT {', '.join(field_names)} FROM Account ORDER BY Id"  # noqa: S608  # nosec B608
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        next_url: str | None = None
        page_count = 0
        total_records = 0
        logger.info(
            "Salesforce Account query started",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "field_count": len(field_names),
                "max_pages": _MAX_PAGES,
            },
        )

        while page_count < _MAX_PAGES:
            next_page = page_count + 1
            logger.info(
                "Fetching Salesforce accounts page",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "page": next_page,
                    "has_next_page": next_url is not None,
                    "records_so_far": total_records,
                },
            )
            payload = await self._get_json(
                next_url or url, params=None if next_url else {"q": soql}
            )
            raw_records = payload.get("records")
            if not isinstance(raw_records, list):
                raise ValueError("Salesforce account query API returned non-list records")
            page_records = [row for row in raw_records if isinstance(row, dict)]
            total_records += len(page_records)

            page_count += 1
            next_records_url = payload.get("nextRecordsUrl")
            has_next_page = isinstance(next_records_url, str) and bool(next_records_url)
            logger.info(
                "Fetched Salesforce accounts page",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "page": page_count,
                    "page_records": len(raw_records),
                    "total_records": total_records,
                    "has_next_page": has_next_page,
                },
            )
            yield page_records
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = (
                next_records_url
                if next_records_url.startswith("http")
                else f"{self.instance_url}{next_records_url}"
            )
        else:
            logger.warning(
                "Salesforce accounts query hit max page ceiling",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
            )

        logger.info(
            "Fetched Salesforce accounts",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": total_records,
                "pages": page_count,
                "field_count": len(field_names),
            },
        )

    async def fetch_one(self, account_id: str) -> dict[str, Any] | None:
        field_names = await self._fetch_field_names()
        soql = (
            f"SELECT {', '.join(field_names)} FROM Account "  # noqa: S608  # nosec B608
            f"WHERE Id = '{_escape_soql_literal(account_id)}' LIMIT 1"
        )
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        payload = await self._get_json(url, params={"q": soql})
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Salesforce single Account query API returned non-list records")
        if not raw_records:
            return None
        record = raw_records[0]
        if not isinstance(record, dict):
            raise ValueError("Salesforce single Account query API returned non-dict record")
        return record

    async def _fetch_field_names(self) -> list[str]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/sobjects/Account/describe"
        logger.info(
            "Salesforce Account describe started",
            extra={"integration_id": str(self.token_info.integration_id)},
        )
        payload = await self._get_json(url)
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, list):
            raise ValueError("Salesforce Account describe API returned non-list fields")

        queryable_field_names = {
            name
            for field in raw_fields
            if isinstance(field, dict)
            for name in [_extract_field_name(field)]
            if name is not None
        }
        queryable_field_names.update(_REQUIRED_FIELDS)
        field_names = [
            field_name
            for field_name in _ACCOUNT_FIELD_ALLOWLIST
            if field_name in queryable_field_names
        ]
        if "Id" not in field_names:
            raise ValueError("Salesforce Account describe API did not yield a usable Id field")
        missing_allowlist_fields = [
            field_name
            for field_name in _ACCOUNT_FIELD_ALLOWLIST
            if field_name not in queryable_field_names
        ]
        logger.info(
            "Salesforce Account describe completed",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "raw_field_count": len(raw_fields),
                "queryable_field_count": len(queryable_field_names),
                "field_count": len(field_names),
                "missing_allowlist_field_count": len(missing_allowlist_fields),
            },
        )
        return field_names

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
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Sforce-Query-Options": f"batchSize={_QUERY_BATCH_SIZE}",
                    },
                    params=params,
                )
                _raise_for_status_with_salesforce_body(response)
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce accounts API returned non-dict payload")
        return payload


def _extract_field_name(field: dict[str, Any]) -> str | None:
    name = field.get("name")
    if not isinstance(name, str) or not name:
        return None
    if field.get("queryable") is False:
        return None
    if field.get("deprecatedAndHidden") is True:
        return None
    return name


def _escape_soql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _raise_for_status_with_salesforce_body(response: httpx.Response) -> None:
    if not response.is_error:
        return

    body = response.text[:_MAX_ERROR_BODY_CHARS]
    message = (
        f"{response.status_code} {response.reason_phrase} for url '{response.url}'. "
        f"Salesforce response body: {body}"
    )
    raise httpx.HTTPStatusError(message, request=response.request, response=response)
