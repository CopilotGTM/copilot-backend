"""Salesforce fetcher for CRM note record sync."""

from __future__ import annotations

import asyncio
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
_MAX_PAGES = 500_000
_MAX_REQUEST_ATTEMPTS = 3
_REQUEST_RETRY_DELAYS = (1.0, 3.0)
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
        records: list[dict[str, Any]] = []
        async for batch in self.iter_batches(linked_entity_ids=linked_entity_ids):
            records.extend(batch)
        return records

    async def iter_batches(self, *, linked_entity_ids: list[str]):
        scoped_ids = [entity_id for entity_id in linked_entity_ids if entity_id]
        if not scoped_ids:
            return

        link_count = 0
        hydrated_note_count = 0
        merged_row_count = 0

        for linked_entity_batch in _chunked(scoped_ids, _LINK_BATCH_SIZE):
            link_rows_by_document_id: dict[str, list[dict[str, Any]]] = {}
            # IDs come from our own synced CRM dimension tables, not from user input.
            soql = (
                "SELECT Id, ContentDocumentId, LinkedEntityId "  # noqa: S608  # nosec B608
                f"FROM ContentDocumentLink WHERE LinkedEntityId IN ({_quoted_csv(linked_entity_batch)})"
            )
            for link_row in await self._query_all(soql, error_label="notes link"):
                document_id = link_row.get("ContentDocumentId")
                if not isinstance(document_id, str) or not document_id:
                    continue
                link_rows_by_document_id.setdefault(document_id, []).append(link_row)
                link_count += 1

            for document_batch in _chunked(sorted(link_rows_by_document_id), _NOTE_BATCH_SIZE):
                # ContentDocumentIds also come from the provider-scoped link query above.
                soql = (
                    "SELECT Id, Title, Content, OwnerId, CreatedDate, LastModifiedDate "  # noqa: S608  # nosec B608
                    f"FROM ContentNote WHERE Id IN ({_quoted_csv(document_batch)})"
                )
                merged_rows: list[dict[str, Any]] = []
                for row in await self._query_all(soql, error_label="notes hydrate"):
                    note_id = row.get("Id")
                    if isinstance(note_id, str) and note_id:
                        hydrated_note_count += 1
                        for link_row in link_rows_by_document_id.get(note_id, []):
                            merged_rows.append(_merge_link_and_note(link_row, row))
                if merged_rows:
                    merged_row_count += len(merged_rows)
                    yield merged_rows

        logger.info(
            "Fetched Salesforce notes",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "linked_entity_count": len(scoped_ids),
                "link_count": link_count,
                "hydrated_note_count": hydrated_note_count,
                "merged_row_count": merged_row_count,
            },
        )

    async def fetch_one_by_note_id(
        self,
        content_note_id: str,
        *,
        linked_entity_ids: list[str],
    ) -> list[dict[str, Any]]:
        scoped_ids = [entity_id for entity_id in linked_entity_ids if entity_id]
        if not scoped_ids:
            return []

        link_rows: list[dict[str, Any]] = []
        for batch in _chunked(scoped_ids, _LINK_BATCH_SIZE):
            soql = (
                "SELECT Id, ContentDocumentId, LinkedEntityId "  # noqa: S608  # nosec B608
                "FROM ContentDocumentLink "
                f"WHERE ContentDocumentId = '{_escape_soql_literal(content_note_id)}' "
                f"AND LinkedEntityId IN ({_quoted_csv(batch)})"
            )
            link_rows.extend(await self._query_all(soql, error_label="notes single link"))
        if not link_rows:
            return []

        soql = (
            "SELECT Id, Title, Content, OwnerId, CreatedDate, LastModifiedDate "  # noqa: S608  # nosec B608
            f"FROM ContentNote WHERE Id = '{_escape_soql_literal(content_note_id)}' LIMIT 1"
        )
        note_rows = await self._query_all(soql, error_label="notes single hydrate")
        if not note_rows:
            return []
        note_row = note_rows[0]
        return [_merge_link_and_note(link_row, note_row) for link_row in link_rows]

    async def fetch_one_by_link_id(
        self,
        content_document_link_id: str,
        *,
        linked_entity_ids: list[str],
    ) -> list[dict[str, Any]]:
        scoped_ids = {entity_id for entity_id in linked_entity_ids if entity_id}
        if not scoped_ids:
            return []

        link_row = await self.fetch_link_by_id(content_document_link_id)
        if link_row is None:
            return []
        document_id = link_row.get("ContentDocumentId")
        linked_entity_id = link_row.get("LinkedEntityId")
        if (
            not isinstance(document_id, str)
            or not document_id
            or not isinstance(linked_entity_id, str)
            or linked_entity_id not in scoped_ids
        ):
            return []
        return await self.fetch_one_by_note_id(document_id, linked_entity_ids=linked_entity_ids)

    async def fetch_link_by_id(self, content_document_link_id: str) -> dict[str, Any] | None:
        soql = (
            "SELECT Id, ContentDocumentId, LinkedEntityId, IsDeleted "  # noqa: S608  # nosec B608
            "FROM ContentDocumentLink "
            f"WHERE Id = '{_escape_soql_literal(content_document_link_id)}' LIMIT 1"
        )
        link_rows = await self._query_all(
            soql,
            error_label="notes link by id",
            use_query_all=True,
        )
        return link_rows[0] if link_rows else None

    async def _query_all(
        self,
        soql: str,
        *,
        error_label: str,
        use_query_all: bool = False,
    ) -> list[dict[str, Any]]:
        resource = "queryAll" if use_query_all else "query"
        url = f"{self.instance_url}/services/data/{_API_VERSION}/{resource}"
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

        response = await _call_with_transient_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce notes API returned non-dict payload")
        return payload


async def _call_with_transient_retry(
    token_info: IntegrationTokenInfo,
    request_fn,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_REQUEST_ATTEMPTS):
        try:
            return await call_with_token_retry(token_info, request_fn)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
            last_error = exc

        if attempt < len(_REQUEST_RETRY_DELAYS):
            await asyncio.sleep(_REQUEST_RETRY_DELAYS[attempt])

    if last_error is None:
        raise RuntimeError("Salesforce notes request retry failed without an error")
    raise last_error


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
    return ", ".join("'" + _escape_soql_literal(value) + "'" for value in values)


def _escape_soql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _merge_link_and_note(link_row: dict[str, Any], note_row: dict[str, Any]) -> dict[str, Any]:
    merged = {**link_row, **note_row}
    link_id = link_row.get("Id")
    if isinstance(link_id, str) and link_id:
        merged["ContentDocumentLinkId"] = link_id
    return merged
