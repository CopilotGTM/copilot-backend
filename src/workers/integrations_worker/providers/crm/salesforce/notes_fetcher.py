"""Salesforce fetcher for CRM note record sync."""

# ruff: noqa: S608

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
_MAX_PAGES = 500
_LINK_BATCH_SIZE = 200
_NOTE_BATCH_SIZE = 200


class SalesforceNotesFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self, *, linked_entity_ids: list[str]) -> list[dict[str, Any]]:
        scoped_ids = [entity_id for entity_id in linked_entity_ids if entity_id]
        if not scoped_ids:
            return []

        link_rows: list[dict[str, Any]] = []
        for batch in _chunked(scoped_ids, _LINK_BATCH_SIZE):
            # IDs come from our own synced CRM dimension tables, not from user input.
            soql = (
                "SELECT ContentDocumentId, LinkedEntityId "
                f"FROM ContentDocumentLink WHERE LinkedEntityId IN ({_quoted_csv(batch)})"  # nosec B608
            )
            link_rows.extend(await self._query_all(soql, error_label="notes link"))

        document_ids = sorted(
            {
                document_id
                for row in link_rows
                if isinstance(row, dict)
                for document_id in [row.get("ContentDocumentId")]
                if isinstance(document_id, str) and document_id
            }
        )
        if not document_ids:
            logger.info(
                "Fetched Salesforce note links but found no ContentDocumentIds",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "linked_entity_count": len(scoped_ids),
                    "link_count": len(link_rows),
                },
            )
            return []

        note_rows_by_id: dict[str, dict[str, Any]] = {}
        for batch in _chunked(document_ids, _NOTE_BATCH_SIZE):
            # ContentDocumentIds also come from the provider-scoped link query above.
            soql = (
                "SELECT Id, Title, Content, OwnerId, CreatedDate, LastModifiedDate "
                f"FROM ContentNote WHERE Id IN ({_quoted_csv(batch)})"  # nosec B608
            )
            for row in await self._query_all(soql, error_label="notes hydrate"):
                note_id = row.get("Id")
                if isinstance(note_id, str) and note_id:
                    note_rows_by_id[note_id] = row

        merged_rows: list[dict[str, Any]] = []
        for link_row in link_rows:
            document_id = link_row.get("ContentDocumentId")
            if not isinstance(document_id, str) or not document_id:
                continue
            note_row = note_rows_by_id.get(document_id)
            if note_row is None:
                continue
            merged_rows.append({**link_row, **note_row})

        logger.info(
            "Fetched Salesforce notes",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "linked_entity_count": len(scoped_ids),
                "link_count": len(link_rows),
                "hydrated_note_count": len(note_rows_by_id),
                "merged_row_count": len(merged_rows),
            },
        )
        return merged_rows

    async def _query_all(self, soql: str, *, error_label: str) -> list[dict[str, Any]]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        records: list[dict[str, Any]] = []
        next_url: str | None = None
        page_count = 0

        while page_count < _MAX_PAGES:
            payload = await self._get_json(
                next_url or url, params=None if next_url else {"q": soql}
            )
            raw_records = payload.get("records")
            if not isinstance(raw_records, list):
                raise ValueError(f"Salesforce {error_label} query API returned non-list records")
            records.extend(row for row in raw_records if isinstance(row, dict))

            page_count += 1
            next_records_url = payload.get("nextRecordsUrl")
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = (
                next_records_url
                if next_records_url.startswith("http")
                else f"{self.instance_url}{next_records_url}"
            )
        else:
            logger.warning(
                "Salesforce %s query hit max page ceiling",
                error_label,
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
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
                response.raise_for_status()
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce notes API returned non-dict payload")
        return payload


def _chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _quoted_csv(values: list[str]) -> str:
    return ", ".join("'" + value.replace("'", "\\'") + "'" for value in values)
