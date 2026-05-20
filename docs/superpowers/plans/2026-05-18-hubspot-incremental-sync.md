# HubSpot Incremental Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe HubSpot incremental sync for the core CRM objects: deals, contacts, and companies/accounts.

**Architecture:** Keep the existing post-connect full sync as the baseline and add a recurring incremental path that discovers changed HubSpot object ids through CRM Search. Incremental sync and existing webhooks must converge into single-object fetch/persist paths so changed-only runs never behave like full snapshots.

**Tech Stack:** Python 3.11, FastAPI, Celery integrations worker, SQLModel/SQLAlchemy async sessions, PostgreSQL JSONB metadata, Redis locks, HubSpot CRM v3 Search/Object APIs, pytest, ruff.

---

## Current Repo State

The current worktree is `D:\CopilotGTM\copilot-backend-hubspot-deal-webhooks`.

Already implemented:

- HubSpot initial sync chain: `schema -> pipelines -> users -> companies -> contacts -> deals -> tasks -> notes -> finalize`.
- Deal webhooks:
  - `deal.creation`
  - `deal.propertyChange`
  - `deal.deletion`
- Company webhooks:
  - `company.creation`
  - `company.propertyChange`
  - `company.deletion`
- Single-record write path for deals:
  - `HubSpotSingleDealFetcher`
  - `HubSpotDealRecordSyncService._persist_raw_deals(...)`
- Single-record write path for companies:
  - `HubSpotSingleCompanyFetcher`
  - `HubSpotCompanyRecordSyncService.persist_raw_company(...)`
  - `HubSpotCompanyWriter.apply_single(...)`

Not implemented yet:

- Incremental search fetcher.
- Per-object incremental cursor helpers.
- Contact single-object fetch/persist path.
- Shared object refresh service.
- Incremental core task for deals, contacts, and companies.
- Kubernetes CronJob schedule for every 5 minutes.
- Freshness checks that skip older provider payloads.

## Implementation Boundaries

Implement now:

- Deals, contacts, companies incremental search every 5 minutes.
- Per-integration, per-object cursor metadata.
- 10-minute overlap window.
- Single-object refresh path for all three core objects.
- Redis object refresh lock.
- Tests proving changed-only sync does not deactivate unrelated rows.

Do not implement in this pass:

- Task and note incremental sync.
- Pipeline/stage or owner/user scheduled refresh.
- HubSpot contact webhooks.
- Restore, merge, or association-change webhooks.
- Full reconciliation job.
- Database schema changes.

## Cadence Decision

Initial production cadence:

| Data family | Mechanism | Frequency | Reason |
|-------------|-----------|-----------|--------|
| Deals | Webhooks plus incremental search | Webhooks immediate, search every 5 minutes | Deals are user-visible and already have webhooks. Search covers missed webhook deliveries and backfills changes not represented by subscribed properties. |
| Companies/accounts | Webhooks plus incremental search | Webhooks immediate, search every 5 minutes | Companies are core CRM context and already have webhooks. Search is the safety net. |
| Contacts | Incremental search | Search every 5 minutes | Contacts are core deal context, but contact webhooks are deferred to avoid expanding subscription surface before the incremental path is stable. |
| Tasks | Deferred incremental search | 15 to 30 minutes later | Useful but less urgent than core CRM identity and deal state. Add only after the core path proves cursor correctness. |
| Notes | Deferred incremental search | 15 to 30 minutes later | Same as tasks. |
| Pipelines and stages | Existing/full refresh or future scheduled refresh | Every 2 hours later | Low-change metadata. Avoid 5-minute schema-style pulls. |
| Owners/users | Existing/full refresh or future scheduled refresh | Every 2 hours later | Low-change metadata, but important for ownership mapping. |
| CRM schema/properties | Existing schema sync | Daily later | High-cost and low-change. Do not put in the 5-minute loop. |

The important rule is that 5-minute search is not a replacement for webhooks. It is the convergence layer. Webhooks reduce latency; incremental search provides completeness when webhooks are delayed, dropped, filtered, or not yet subscribed.

## Webhook and Incremental Convergence

Deal and company webhooks already exist in this worktree. Keep them enabled. The implementation must make webhook refresh and incremental refresh call the same single-object convergence service:

```text
HubSpot webhook event
    -> dedupe/coalesce event
    -> HubSpotObjectRefreshService.refresh_one(...)
    -> single-object fetch from HubSpot
    -> single-object writer

HubSpot incremental search
    -> changed object ids
    -> HubSpotObjectRefreshService.refresh_one(...)
    -> single-object fetch from HubSpot
    -> single-object writer
```

This makes duplicate processing acceptable:

- If webhook wins first, the later incremental run fetches the same current HubSpot state and writes an idempotent update.
- If incremental wins first, the later webhook does the same.
- If both run at the same time, the per-object Redis lock collapses duplicate work where Redis is available.
- If Redis is unavailable, the writers still have to be idempotent because lock failure should not become data loss.
- The payload timestamp from a webhook must not be trusted as the row state. Always fetch the current object from HubSpot before persisting.

## Critical Rules

1. Never treat changed-id search results as a full provider snapshot.
2. Never advance a cursor before all selected object writes commit.
3. Never use an external id without `organisation_id` and `integration_id`.
4. Do not use `KnowledgeIntegration.last_synced_at` as the object cursor.
5. Use `connection_metadata.record_sync.<object>.incremental.cursor_source_modified_at`.
6. Use a 10-minute lookback for the 5-minute job.
7. If the run fails or hits a page ceiling, keep the old cursor.
8. If HubSpot returns 404 from a single-object refresh, soft-delete the matching local row for that object and integration only.
9. Webhooks and incremental sync are allowed to process the same object. The writer path must be idempotent.
10. Do not advance a cursor from webhook processing. Only the scheduled incremental task owns cursors.

## File Map

Create:

- `src/workers/integrations_worker/providers/crm/hubspot/search_fetcher.py`
  - HubSpot CRM Search changed-id pagination.
- `src/service/integrations/crm/records/hubspot/incremental_cursor.py`
  - Cursor metadata read/write helpers.
- `src/workers/integrations_worker/providers/crm/hubspot/single_contact_fetcher.py`
  - Fetch one full contact by HubSpot object id.
- `src/service/integrations/crm/records/hubspot/object_refresh.py`
  - Shared object refresh service for deal/contact/company.
- `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_incremental_core_task.py`
  - One Celery task for core incremental sync.
- `tests/workers/integrations_worker/providers/crm/hubspot/test_search_fetcher.py`
- `tests/service/integrations/crm/records/hubspot/test_incremental_cursor.py`
- `tests/service/integrations/crm/records/hubspot/contacts/test_single_contact_sync.py`
- `tests/service/integrations/crm/records/hubspot/test_object_refresh.py`
- `tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_task.py`

Modify:

- `src/workers/integrations_worker/providers/crm/hubspot/contacts_fetcher.py`
  - Export contact property tuple for single-contact fetcher reuse.
- `src/service/integrations/crm/records/hubspot/contacts/contact_writer.py`
  - Load soft-deleted rows too and reactivate on upsert.
- `src/service/integrations/crm/records/hubspot/contacts/contact_sync_service.py`
  - Add `persist_raw_contact(...)` and `_persist_single(...)`.
- `src/workers/integrations_worker/tasks/crm/hubspot/__init__.py`
  - Export new task.
- `src/workers/integrations_worker/tasks/crm/__init__.py`
  - Export new task if task package uses central imports.
- `src/workers/integrations_worker/tasks/__init__.py`
  - Export new task if required by worker autodiscovery.
- `src/workers/integrations_worker/config.py`
  - Add task limit and route for new task.
- `docs/Technical Documentation/integrations/hubspot-webhooks-incremental-sync.md`
  - Update status from design to implementation once complete.

Optional infra follow-up:

- `copilot-infra` Kubernetes CronJob manifests for 5-minute schedule. Do not edit infra in this backend implementation pass unless the user explicitly asks.

---

### Task 1: Add Incremental Cursor Helpers

**Files:**

- Create: `src/service/integrations/crm/records/hubspot/incremental_cursor.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_incremental_cursor.py`

- [ ] **Step 1: Write cursor helper tests**

Create `tests/service/integrations/crm/records/hubspot/test_incremental_cursor.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.service.integrations.crm.records.hubspot.incremental_cursor import (
    DEFAULT_CORE_LOOKBACK_MINUTES,
    HubSpotIncrementalCursor,
    get_incremental_cursor,
    record_incremental_failure,
    record_incremental_success,
    resolve_query_start,
)


def test_get_incremental_cursor_reads_existing_metadata() -> None:
    metadata = {
        "record_sync": {
            "deals": {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T10:06:43+00:00",
                    "lookback_minutes": 10,
                }
            }
        }
    }

    cursor = get_incremental_cursor(metadata, "deals")

    assert cursor == HubSpotIncrementalCursor(
        cursor_source_modified_at=datetime(2026, 5, 18, 10, 6, 43, tzinfo=UTC),
        lookback_minutes=10,
    )


def test_get_incremental_cursor_returns_default_when_absent() -> None:
    cursor = get_incremental_cursor({}, "contacts")

    assert cursor.cursor_source_modified_at is None
    assert cursor.lookback_minutes == DEFAULT_CORE_LOOKBACK_MINUTES


def test_resolve_query_start_uses_cursor_minus_lookback() -> None:
    cursor = HubSpotIncrementalCursor(
        cursor_source_modified_at=datetime(2026, 5, 18, 10, 6, 43, tzinfo=UTC),
        lookback_minutes=10,
    )

    query_start = resolve_query_start(
        cursor,
        fallback_start=datetime(2026, 5, 17, tzinfo=UTC),
    )

    assert query_start == datetime(2026, 5, 18, 9, 56, 43, tzinfo=UTC)


def test_resolve_query_start_uses_fallback_for_first_incremental_run() -> None:
    cursor = HubSpotIncrementalCursor(cursor_source_modified_at=None, lookback_minutes=10)
    fallback = datetime(2026, 5, 17, 10, 0, tzinfo=UTC)

    assert resolve_query_start(cursor, fallback_start=fallback) == fallback


def test_record_incremental_success_preserves_existing_metadata() -> None:
    metadata = {
        "provider_key": "hubspot",
        "record_sync": {
            "deals": {
                "status": "success",
                "fetched_deals": 20,
            }
        },
    }
    completed_at = datetime(2026, 5, 18, 10, 10, tzinfo=UTC)
    max_seen = datetime(2026, 5, 18, 10, 6, 43, tzinfo=UTC)

    updated = record_incremental_success(
        metadata,
        object_key="deals",
        completed_at=completed_at,
        cursor_source_modified_at=max_seen,
        query_start=datetime(2026, 5, 18, 9, 56, 43, tzinfo=UTC),
        ids_discovered=4,
        objects_written=3,
        objects_deleted=1,
        skipped_stale=2,
    )

    entry = updated["record_sync"]["deals"]["incremental"]
    assert updated["provider_key"] == "hubspot"
    assert updated["record_sync"]["deals"]["fetched_deals"] == 20
    assert entry["cursor_source_modified_at"] == "2026-05-18T10:06:43+00:00"
    assert entry["cursor_updated_at"] == "2026-05-18T10:10:00+00:00"
    assert entry["query_start"] == "2026-05-18T09:56:43+00:00"
    assert entry["ids_discovered"] == 4
    assert entry["objects_written"] == 3
    assert entry["objects_deleted"] == 1
    assert entry["skipped_stale"] == 2
    assert entry["status"] == "success"


def test_record_incremental_failure_keeps_cursor() -> None:
    metadata = {
        "record_sync": {
            "companies": {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T09:00:00+00:00",
                }
            }
        }
    }

    updated = record_incremental_failure(
        metadata,
        object_key="companies",
        completed_at=datetime(2026, 5, 18, 10, 10, tzinfo=UTC),
        error_class="HTTPStatusError",
        error_message="429",
    )

    entry = updated["record_sync"]["companies"]["incremental"]
    assert entry["cursor_source_modified_at"] == "2026-05-18T09:00:00+00:00"
    assert entry["status"] == "failed"
    assert entry["error_class"] == "HTTPStatusError"
    assert entry["error_message"] == "429"
```

- [ ] **Step 2: Run cursor tests and verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_incremental_cursor.py -v
```

Expected: FAIL because `incremental_cursor.py` does not exist.

- [ ] **Step 3: Implement cursor helper**

Create `src/service/integrations/crm/records/hubspot/incremental_cursor.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_CORE_LOOKBACK_MINUTES = 10


@dataclass(frozen=True, slots=True)
class HubSpotIncrementalCursor:
    cursor_source_modified_at: datetime | None
    lookback_minutes: int = DEFAULT_CORE_LOOKBACK_MINUTES


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def get_incremental_cursor(
    connection_metadata: dict[str, Any] | None,
    object_key: str,
) -> HubSpotIncrementalCursor:
    metadata = connection_metadata or {}
    record_sync = metadata.get("record_sync")
    if not isinstance(record_sync, dict):
        return HubSpotIncrementalCursor(cursor_source_modified_at=None)

    object_entry = record_sync.get(object_key)
    if not isinstance(object_entry, dict):
        return HubSpotIncrementalCursor(cursor_source_modified_at=None)

    incremental = object_entry.get("incremental")
    if not isinstance(incremental, dict):
        return HubSpotIncrementalCursor(cursor_source_modified_at=None)

    lookback = incremental.get("lookback_minutes")
    return HubSpotIncrementalCursor(
        cursor_source_modified_at=_parse_datetime(incremental.get("cursor_source_modified_at")),
        lookback_minutes=lookback if isinstance(lookback, int) and lookback > 0 else DEFAULT_CORE_LOOKBACK_MINUTES,
    )


def resolve_query_start(
    cursor: HubSpotIncrementalCursor,
    *,
    fallback_start: datetime,
) -> datetime:
    if cursor.cursor_source_modified_at is None:
        return fallback_start.astimezone(UTC) if fallback_start.tzinfo else fallback_start.replace(tzinfo=UTC)
    return cursor.cursor_source_modified_at - timedelta(minutes=cursor.lookback_minutes)


def record_incremental_success(
    connection_metadata: dict[str, Any] | None,
    *,
    object_key: str,
    completed_at: datetime,
    cursor_source_modified_at: datetime | None,
    query_start: datetime,
    ids_discovered: int,
    objects_written: int,
    objects_deleted: int,
    skipped_stale: int,
) -> dict[str, Any]:
    metadata = dict(connection_metadata or {})
    record_sync = dict(metadata.get("record_sync") or {})
    object_entry = dict(record_sync.get(object_key) or {})
    previous_incremental = dict(object_entry.get("incremental") or {})
    previous_cursor = previous_incremental.get("cursor_source_modified_at")

    object_entry["incremental"] = {
        **previous_incremental,
        "status": "success",
        "cursor_source_modified_at": (
            _iso(cursor_source_modified_at) if cursor_source_modified_at is not None else previous_cursor
        ),
        "cursor_updated_at": _iso(completed_at),
        "query_start": _iso(query_start),
        "lookback_minutes": previous_incremental.get(
            "lookback_minutes", DEFAULT_CORE_LOOKBACK_MINUTES
        ),
        "last_run_type": "incremental_search",
        "ids_discovered": ids_discovered,
        "objects_written": objects_written,
        "objects_deleted": objects_deleted,
        "skipped_stale": skipped_stale,
        "error_class": None,
        "error_message": None,
    }
    record_sync[object_key] = object_entry
    metadata["record_sync"] = record_sync
    return metadata


def record_incremental_failure(
    connection_metadata: dict[str, Any] | None,
    *,
    object_key: str,
    completed_at: datetime,
    error_class: str,
    error_message: str,
) -> dict[str, Any]:
    metadata = dict(connection_metadata or {})
    record_sync = dict(metadata.get("record_sync") or {})
    object_entry = dict(record_sync.get(object_key) or {})
    previous_incremental = dict(object_entry.get("incremental") or {})
    previous_incremental.update(
        {
            "status": "failed",
            "cursor_updated_at": _iso(completed_at),
            "last_run_type": "incremental_search",
            "error_class": error_class,
            "error_message": error_message,
        }
    )
    object_entry["incremental"] = previous_incremental
    record_sync[object_key] = object_entry
    metadata["record_sync"] = record_sync
    return metadata
```

- [ ] **Step 4: Run cursor tests and verify they pass**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_incremental_cursor.py -v
```

Expected: PASS.

---

### Task 2: Add HubSpot CRM Search Changed-ID Fetcher

**Files:**

- Create: `src/workers/integrations_worker/providers/crm/hubspot/search_fetcher.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_search_fetcher.py`

- [ ] **Step 1: Write search fetcher tests**

Create `tests/workers/integrations_worker/providers/crm/hubspot/test_search_fetcher.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.workers.integrations_worker.providers.crm.hubspot.search_fetcher import (
    HubSpotChangedObject,
    HubSpotSearchFetcher,
)


@pytest.mark.asyncio
async def test_search_fetcher_posts_filter_and_paginates(monkeypatch) -> None:
    token_info = SimpleNamespace(integration_id="int-1")
    calls = []

    async def fake_call_with_token_retry(token, request_factory):
        assert token is token_info
        response = await request_factory("token")
        return response

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def fake_post(url, *, headers, json):
        calls.append({"url": url, "headers": headers, "json": json})
        if len(calls) == 1:
            return FakeResponse(
                {
                    "results": [
                        {
                            "id": "101",
                            "updatedAt": "2026-05-18T10:01:00Z",
                            "properties": {"hs_lastmodifieddate": "2026-05-18T10:01:00Z"},
                        }
                    ],
                    "paging": {"next": {"after": "next-1"}},
                }
            )
        return FakeResponse(
            {
                "results": [
                    {
                        "id": "102",
                        "updatedAt": "2026-05-18T10:02:00Z",
                        "properties": {"hs_lastmodifieddate": "2026-05-18T10:02:00Z"},
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.search_fetcher._post_json",
        fake_post,
    )

    result = await HubSpotSearchFetcher(token_info=token_info).fetch_changed_ids(
        object_type="deals",
        modified_property="hs_lastmodifieddate",
        query_start=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
    )

    assert result == [
        HubSpotChangedObject(
            external_id="101",
            source_modified_at=datetime(2026, 5, 18, 10, 1, tzinfo=UTC),
        ),
        HubSpotChangedObject(
            external_id="102",
            source_modified_at=datetime(2026, 5, 18, 10, 2, tzinfo=UTC),
        ),
    ]
    assert calls[0]["url"] == "https://api.hubapi.com/crm/v3/objects/deals/search"
    assert calls[0]["json"]["filterGroups"][0]["filters"][0] == {
        "propertyName": "hs_lastmodifieddate",
        "operator": "GTE",
        "value": "2026-05-18T10:00:00Z",
    }
    assert calls[1]["json"]["after"] == "next-1"


@pytest.mark.asyncio
async def test_search_fetcher_dedupes_ids_and_keeps_latest_modified(monkeypatch) -> None:
    token_info = SimpleNamespace(integration_id="int-1")

    async def fake_call_with_token_retry(token, request_factory):
        return await request_factory("token")

    class FakeResponse:
        def json(self):
            return {
                "results": [
                    {"id": "101", "updatedAt": "2026-05-18T10:01:00Z", "properties": {}},
                    {"id": "101", "updatedAt": "2026-05-18T10:03:00Z", "properties": {}},
                ]
            }

    async def fake_post(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.search_fetcher._post_json",
        fake_post,
    )

    result = await HubSpotSearchFetcher(token_info=token_info).fetch_changed_ids(
        object_type="contacts",
        modified_property="lastmodifieddate",
        query_start=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
    )

    assert result == [
        HubSpotChangedObject(
            external_id="101",
            source_modified_at=datetime(2026, 5, 18, 10, 3, tzinfo=UTC),
        )
    ]


@pytest.mark.asyncio
async def test_search_fetcher_raises_on_page_ceiling(monkeypatch) -> None:
    token_info = SimpleNamespace(integration_id="int-1")

    async def fake_call_with_token_retry(token, request_factory):
        return await request_factory("token")

    class FakeResponse:
        def json(self):
            return {"results": [], "paging": {"next": {"after": "again"}}}

    async def fake_post(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.search_fetcher.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.search_fetcher._post_json",
        fake_post,
    )

    with pytest.raises(RuntimeError, match="page ceiling"):
        await HubSpotSearchFetcher(token_info=token_info, max_pages=2).fetch_changed_ids(
            object_type="companies",
            modified_property="hs_lastmodifieddate",
            query_start=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        )
```

- [ ] **Step 2: Run search fetcher tests and verify they fail**

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_search_fetcher.py -v
```

Expected: FAIL because `search_fetcher.py` does not exist.

- [ ] **Step 3: Implement search fetcher**

Create `src/workers/integrations_worker/providers/crm/hubspot/search_fetcher.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/{object_type}/search"
_DEFAULT_TIMEOUT = 30.0
_PAGE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class HubSpotChangedObject:
    external_id: str
    source_modified_at: datetime | None


class HubSpotSearchFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        max_pages: int = 500,
    ) -> None:
        self.token_info = token_info
        self.max_pages = max_pages

    async def fetch_changed_ids(
        self,
        *,
        object_type: str,
        modified_property: str,
        query_start: datetime,
    ) -> list[HubSpotChangedObject]:
        after: str | None = None
        by_external_id: dict[str, datetime | None] = {}

        for page_no in range(self.max_pages):
            body = _build_search_body(
                modified_property=modified_property,
                query_start=query_start,
                after=after,
            )
            response = await call_with_token_retry(
                self.token_info,
                _make_request(object_type, body),
            )
            payload = response.json()
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("HubSpot search API returned non-list `results`")

            for item in results:
                if not isinstance(item, dict):
                    continue
                external_id = item.get("id")
                if not isinstance(external_id, str) or not external_id:
                    continue
                source_modified_at = _extract_modified_at(item, modified_property)
                existing = by_external_id.get(external_id)
                if existing is None or (
                    source_modified_at is not None and source_modified_at > existing
                ):
                    by_external_id[external_id] = source_modified_at

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                return [
                    HubSpotChangedObject(external_id=external_id, source_modified_at=modified_at)
                    for external_id, modified_at in sorted(
                        by_external_id.items(),
                        key=lambda item: (item[1] or datetime.min.replace(tzinfo=UTC), item[0]),
                    )
                ]
            after = next_cursor

        raise RuntimeError(
            f"HubSpot {object_type} search hit page ceiling after {self.max_pages} pages"
        )


def _build_search_body(
    *,
    modified_property: str,
    query_start: datetime,
    after: str | None,
) -> dict[str, Any]:
    if query_start.tzinfo is None:
        query_start = query_start.replace(tzinfo=UTC)
    body: dict[str, Any] = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": modified_property,
                        "operator": "GTE",
                        "value": query_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
        ],
        "sorts": [{"propertyName": modified_property, "direction": "ASCENDING"}],
        "properties": [modified_property],
        "limit": _PAGE_LIMIT,
    }
    if after is not None:
        body["after"] = after
    return body


def _make_request(object_type: str, body: dict[str, Any]):
    async def _request(access_token: str) -> httpx.Response:
        return await _post_json(
            _BASE_URL.format(object_type=object_type),
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
        )

    return _request


async def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=json)
        response.raise_for_status()
        return response


def _extract_modified_at(item: dict[str, Any], modified_property: str) -> datetime | None:
    props = item.get("properties")
    values: list[object] = [item.get("updatedAt")]
    if isinstance(props, dict):
        values.append(props.get(modified_property))
    for value in values:
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_next_cursor(payload: dict[str, Any]) -> str | None:
    paging = payload.get("paging")
    if not isinstance(paging, dict):
        return None
    nxt = paging.get("next")
    if not isinstance(nxt, dict):
        return None
    after = nxt.get("after")
    if isinstance(after, str) and after:
        return after
    return None
```

- [ ] **Step 4: Run search fetcher tests and verify they pass**

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_search_fetcher.py -v
```

Expected: PASS.

---

### Task 3: Add Contact Single-Object Fetch and Persist

**Files:**

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/contacts_fetcher.py`
- Create: `src/workers/integrations_worker/providers/crm/hubspot/single_contact_fetcher.py`
- Modify: `src/service/integrations/crm/records/hubspot/contacts/contact_writer.py`
- Modify: `src/service/integrations/crm/records/hubspot/contacts/contact_sync_service.py`
- Test: `tests/service/integrations/crm/records/hubspot/contacts/test_single_contact_sync.py`

- [ ] **Step 1: Export contact properties from the bulk fetcher**

Modify `src/workers/integrations_worker/providers/crm/hubspot/contacts_fetcher.py`:

```python
HUBSPOT_CONTACT_PROPERTIES: tuple[str, ...] = (
    "firstname",
    "lastname",
    "jobtitle",
    "email",
    "hs_additional_emails",
    "phone",
    "mobilephone",
    "fax",
    "website",
    "company",
    "associatedcompanyid",
    "hubspot_owner_id",
    "lifecyclestage",
    "hs_lead_status",
    "hs_marketable_status",
    "address",
    "city",
    "state",
    "zip",
    "country",
    "hs_timezone",
    "notes_last_contacted",
    "notes_last_updated",
    "createdate",
    "lastmodifieddate",
    "hs_createdate",
    "hs_lastmodifieddate",
)

_CONTACT_PROPERTIES = HUBSPOT_CONTACT_PROPERTIES
```

Keep `_CONTACT_PROPERTIES` as an alias so existing code continues to work.

- [ ] **Step 2: Write tests for single-contact persistence**

Create `tests/service/integrations/crm/records/hubspot/contacts/test_single_contact_sync.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.service.integrations.crm.records.hubspot.contacts.contact_types import (
    NormalizedHubSpotContact,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_writer import (
    HubSpotContactWriter,
)


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    async def execute(self, statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def _contact(external_id: str) -> NormalizedHubSpotContact:
    return NormalizedHubSpotContact(
        external_id=external_id,
        first_name="Ada",
        last_name="Lovelace",
        full_name="Ada Lovelace",
        job_title="Buyer",
        company_name="Acme",
        company_external_id=None,
        email="ada@example.com",
        additional_emails=[],
        phone=None,
        mobile_phone=None,
        fax=None,
        website=None,
        owner_external_id=None,
        lifecycle_stage=None,
        lead_status=None,
        marketing_status=None,
        address=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
        timezone=None,
        last_contacted_at=None,
        last_activity_at=None,
        source_created_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
        source_modified_at=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        properties={},
        data_payload={"id": external_id},
    )


@pytest.mark.asyncio
async def test_apply_single_reactivates_soft_deleted_contact() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    existing = SimpleNamespace(
        external_id="123",
        is_deleted=True,
        contact_id=uuid4(),
    )
    session = FakeSession([existing])
    writer = HubSpotContactWriter(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
    )

    result = await writer.apply_single(
        contact=_contact("123"),
        company_map={},
        owner_map={},
    )

    assert existing.is_deleted is False
    assert existing.email == "ada@example.com"
    assert result.contacts_updated == 1


@pytest.mark.asyncio
async def test_apply_single_creates_new_contact_without_snapshot_deactivation() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    unrelated = SimpleNamespace(external_id="999", is_deleted=False, contact_id=uuid4())
    session = FakeSession([unrelated])
    writer = HubSpotContactWriter(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
    )

    result = await writer.apply_single(
        contact=_contact("123"),
        company_map={},
        owner_map={},
    )

    assert unrelated.is_deleted is False
    assert result.contacts_created == 1
    assert len(session.added) == 1
```

- [ ] **Step 3: Run contact tests and verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/contacts/test_single_contact_sync.py -v
```

Expected: FAIL because `apply_single(...)` does not exist.

- [ ] **Step 4: Implement single-contact fetcher**

Create `src/workers/integrations_worker/providers/crm/hubspot/single_contact_fetcher.py`:

```python
from __future__ import annotations

from typing import Any

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers.crm.hubspot.contacts_fetcher import (
    HUBSPOT_CONTACT_PROPERTIES,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
_DEFAULT_TIMEOUT = 60.0


class HubSpotSingleContactFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        contact_id: str,
        properties: tuple[str, ...] = HUBSPOT_CONTACT_PROPERTIES,
    ) -> None:
        if not properties:
            raise ValueError("HubSpotSingleContactFetcher requires a non-empty properties list")
        self.token_info = token_info
        self.contact_id = contact_id
        self._properties = tuple(sorted(set(properties)))

    async def fetch(self) -> dict[str, Any] | None:
        response = await call_with_token_retry(
            self.token_info,
            _make_request(
                self.contact_id,
                {
                    "archived": "false",
                    "properties": ",".join(self._properties),
                },
            ),
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HubSpot single contact API returned a non-object payload")
        return payload


def _make_request(contact_id: str, params: dict[str, str]):
    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.get(
                _BASE_URL.format(contact_id=contact_id),
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if response.status_code != 404:
                response.raise_for_status()
            return response

    return _request
```

- [ ] **Step 5: Implement contact writer `apply_single` and soft-delete reactivation**

Modify `src/service/integrations/crm/records/hubspot/contacts/contact_writer.py`:

```python
async def apply_single(
    self,
    *,
    contact: NormalizedHubSpotContact,
    company_map: dict[str, UUID],
    owner_map: dict[str, UUID],
) -> ContactWriteResult:
    result = ContactWriteResult()
    now = datetime.now(UTC)
    existing_by_external = await self._load_existing_rows()
    resolved_company_id = self._resolve_company_id(contact.company_external_id, company_map, result)
    resolved_owner_id = self._resolve_owner_id(contact.owner_external_id, owner_map, result)
    existing = existing_by_external.get(contact.external_id)
    if existing is None:
        row = self._new_row(contact, resolved_company_id, resolved_owner_id, now=now)
        self.session.add(row)
        result.contacts_created += 1
    else:
        self._apply_updates(existing, contact, resolved_company_id, resolved_owner_id, now=now)
        result.contacts_updated += 1
    await self.session.flush()
    return result
```

Change `_load_existing_rows` to load deleted rows too:

```python
async def _load_existing_rows(self) -> dict[str, CRMContact]:
    stmt = select(CRMContact).where(
        col(CRMContact.organisation_id) == self.organisation_id,
        col(CRMContact.integration_id) == self.integration_id,
    )
    rows = (await self.session.execute(stmt)).scalars().all()
    return {r.external_id: r for r in rows if r.external_id is not None}
```

Add this at the top of `_apply_updates(...)`:

```python
row.is_deleted = False
```

- [ ] **Step 6: Add `persist_raw_contact` to contact sync service**

Modify `src/service/integrations/crm/records/hubspot/contacts/contact_sync_service.py`:

```python
async def persist_raw_contact(
    self,
    *,
    organisation_id: UUID,
    integration_id: UUID,
    raw_contact: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    normalized, skipped_malformed = normalize_contacts([raw_contact])
    if not normalized:
        raise ValueError(
            f"HubSpot contact payload was malformed; skipped={skipped_malformed}"
        )
    return await self._persist_single(
        organisation_id=organisation_id,
        integration_id=integration_id,
        contact=normalized[0],
    )
```

Add `_persist_single(...)` mirroring the existing `_persist(...)`, but calling `writer.apply_single(...)` and backfilling only the one contact external id.

- [ ] **Step 7: Run contact tests and focused import checks**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/contacts/test_single_contact_sync.py -v
uv run pytest tests/service/integrations/crm/records/test_hubspot_record_imports.py -v
```

Expected: PASS.

---

### Task 4: Add Shared HubSpot Object Refresh Service

**Files:**

- Create: `src/service/integrations/crm/records/hubspot/object_refresh.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_object_refresh.py`

- [ ] **Step 1: Write object refresh tests**

Create `tests/service/integrations/crm/records/hubspot/test_object_refresh.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.service.integrations.crm.records.hubspot.object_refresh import (
    HubSpotObjectRefreshRequest,
    HubSpotObjectRefreshResult,
    HubSpotObjectRefreshService,
)


@pytest.mark.asyncio
async def test_refresh_deal_fetches_and_persists(monkeypatch) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    calls = []

    class FakeDealService:
        async def _load_token_info(self, organisation_id, integration_id):
            return SimpleNamespace(integration_id=integration_id)

        async def _build_property_list(self, *, token_info):
            return ["dealname", "hs_lastmodifieddate"]

        async def _persist_raw_deals(self, **kwargs):
            calls.append(kwargs)

    class FakeFetcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def fetch(self):
            return {"id": "123", "updatedAt": "2026-05-18T10:00:00Z", "properties": {}}

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.object_refresh.HubSpotDealRecordSyncService",
        FakeDealService,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.object_refresh.HubSpotSingleDealFetcher",
        FakeFetcher,
    )

    result = await HubSpotObjectRefreshService().refresh(
        HubSpotObjectRefreshRequest(
            organisation_id=org_id,
            integration_id=integration_id,
            object_type="deal",
            external_id="123",
            trigger="incremental_search",
        )
    )

    assert result.object_type == "deal"
    assert result.external_id == "123"
    assert result.action == "written"
    assert calls[0]["organisation_id"] == org_id
    assert calls[0]["integration_id"] == integration_id
    assert calls[0]["raw_deals"][0]["id"] == "123"


@pytest.mark.asyncio
async def test_refresh_company_uses_single_company_path(monkeypatch) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    persisted = []

    class FakeCompanyService:
        async def _load_token_info(self, organisation_id, integration_id):
            return SimpleNamespace(integration_id=integration_id)

        async def persist_raw_company(self, **kwargs):
            persisted.append(kwargs)

    class FakeFetcher:
        def __init__(self, **kwargs):
            pass

        async def fetch(self):
            return {"id": "456", "updatedAt": "2026-05-18T10:00:00Z", "properties": {}}

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.object_refresh.HubSpotCompanyRecordSyncService",
        FakeCompanyService,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.object_refresh.HubSpotSingleCompanyFetcher",
        FakeFetcher,
    )

    result = await HubSpotObjectRefreshService().refresh(
        HubSpotObjectRefreshRequest(
            organisation_id=org_id,
            integration_id=integration_id,
            object_type="company",
            external_id="456",
            trigger="incremental_search",
        )
    )

    assert result.action == "written"
    assert persisted[0]["raw_company"]["id"] == "456"


@pytest.mark.asyncio
async def test_refresh_contact_uses_single_contact_path(monkeypatch) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    persisted = []

    class FakeContactService:
        async def _load_token_info(self, organisation_id, integration_id):
            return SimpleNamespace(integration_id=integration_id)

        async def persist_raw_contact(self, **kwargs):
            persisted.append(kwargs)

    class FakeFetcher:
        def __init__(self, **kwargs):
            pass

        async def fetch(self):
            return {"id": "789", "updatedAt": "2026-05-18T10:00:00Z", "properties": {}}

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.object_refresh.HubSpotContactRecordSyncService",
        FakeContactService,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.object_refresh.HubSpotSingleContactFetcher",
        FakeFetcher,
    )

    result = await HubSpotObjectRefreshService().refresh(
        HubSpotObjectRefreshRequest(
            organisation_id=org_id,
            integration_id=integration_id,
            object_type="contact",
            external_id="789",
            trigger="incremental_search",
        )
    )

    assert result.action == "written"
    assert persisted[0]["raw_contact"]["id"] == "789"
```

- [ ] **Step 2: Run object refresh tests and verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_object_refresh.py -v
```

Expected: FAIL because `object_refresh.py` does not exist.

- [ ] **Step 3: Implement object refresh service**

Create `src/service/integrations/crm/records/hubspot/object_refresh.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.service.integrations.crm.records.hubspot.companies.company_sync_service import (
    HubSpotCompanyRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_sync_service import (
    HubSpotContactRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.deals.deal_sync_service import (
    HubSpotDealRecordSyncService,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_company_fetcher import (
    HubSpotSingleCompanyFetcher,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_contact_fetcher import (
    HubSpotSingleContactFetcher,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_deal_fetcher import (
    HubSpotSingleDealFetcher,
)

HubSpotObjectType = Literal["deal", "contact", "company"]
HubSpotRefreshTrigger = Literal["webhook", "incremental_search", "manual_reconcile"]
HubSpotRefreshAction = Literal["written", "deleted", "not_found", "locked"]


@dataclass(frozen=True, slots=True)
class HubSpotObjectRefreshRequest:
    organisation_id: UUID
    integration_id: UUID
    object_type: HubSpotObjectType
    external_id: str
    trigger: HubSpotRefreshTrigger
    event_id: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HubSpotObjectRefreshResult:
    object_type: HubSpotObjectType
    external_id: str
    action: HubSpotRefreshAction
    source_modified_at: datetime | None
    local_synced_at: datetime


class HubSpotObjectRefreshService:
    async def refresh(self, request: HubSpotObjectRefreshRequest) -> HubSpotObjectRefreshResult:
        lock_key = (
            f"hubspot:refresh:{request.integration_id}:{request.object_type}:{request.external_id}"
        )
        if not acquire_task_lock(lock_key, ttl=300):
            return HubSpotObjectRefreshResult(
                object_type=request.object_type,
                external_id=request.external_id,
                action="locked",
                source_modified_at=None,
                local_synced_at=datetime.now(UTC),
            )
        try:
            if request.object_type == "deal":
                return await self._refresh_deal(request)
            if request.object_type == "company":
                return await self._refresh_company(request)
            if request.object_type == "contact":
                return await self._refresh_contact(request)
            raise ValueError(f"Unsupported HubSpot object type: {request.object_type}")
        finally:
            release_task_lock(lock_key)

    async def _refresh_deal(self, request: HubSpotObjectRefreshRequest) -> HubSpotObjectRefreshResult:
        service = HubSpotDealRecordSyncService()
        token_info = await service._load_token_info(request.organisation_id, request.integration_id)
        property_list = await service._build_property_list(token_info=token_info)
        raw = await HubSpotSingleDealFetcher(
            token_info=token_info,
            deal_id=request.external_id,
            properties=property_list,
        ).fetch()
        if raw is None:
            from src.service.integrations.webhooks.hubspot.deals.handler import (
                HubSpotDealWebhookHandler,
            )

            await HubSpotDealWebhookHandler._soft_delete_deal(
                organisation_id=request.organisation_id,
                integration_id=request.integration_id,
                external_id=request.external_id,
            )
            return HubSpotObjectRefreshResult(
                object_type="deal",
                external_id=request.external_id,
                action="deleted",
                source_modified_at=None,
                local_synced_at=datetime.now(UTC),
            )
        await service._persist_raw_deals(
            organisation_id=request.organisation_id,
            integration_id=request.integration_id,
            raw_deals=[raw],
            property_list_size=len(property_list),
        )
        return _written_result("deal", request.external_id, raw)

    async def _refresh_company(
        self, request: HubSpotObjectRefreshRequest
    ) -> HubSpotObjectRefreshResult:
        service = HubSpotCompanyRecordSyncService()
        token_info = await service._load_token_info(request.organisation_id, request.integration_id)
        raw = await HubSpotSingleCompanyFetcher(
            token_info=token_info,
            company_id=request.external_id,
        ).fetch()
        if raw is None:
            from src.service.integrations.webhooks.hubspot.companies.handler import (
                HubSpotCompanyWebhookHandler,
            )

            await HubSpotCompanyWebhookHandler._soft_delete_company(
                organisation_id=request.organisation_id,
                integration_id=request.integration_id,
                external_id=request.external_id,
            )
            return HubSpotObjectRefreshResult(
                object_type="company",
                external_id=request.external_id,
                action="deleted",
                source_modified_at=None,
                local_synced_at=datetime.now(UTC),
            )
        await service.persist_raw_company(
            organisation_id=request.organisation_id,
            integration_id=request.integration_id,
            raw_company=raw,
        )
        return _written_result("company", request.external_id, raw)

    async def _refresh_contact(
        self, request: HubSpotObjectRefreshRequest
    ) -> HubSpotObjectRefreshResult:
        service = HubSpotContactRecordSyncService()
        token_info = await service._load_token_info(request.organisation_id, request.integration_id)
        raw = await HubSpotSingleContactFetcher(
            token_info=token_info,
            contact_id=request.external_id,
        ).fetch()
        if raw is None:
            # Contact full delete semantics are not implemented yet. Mark as
            # not_found for this pass; contact deletion webhook can own the
            # final local policy when it is added.
            return HubSpotObjectRefreshResult(
                object_type="contact",
                external_id=request.external_id,
                action="not_found",
                source_modified_at=None,
                local_synced_at=datetime.now(UTC),
            )
        await service.persist_raw_contact(
            organisation_id=request.organisation_id,
            integration_id=request.integration_id,
            raw_contact=raw,
        )
        return _written_result("contact", request.external_id, raw)


def _written_result(
    object_type: HubSpotObjectType,
    external_id: str,
    raw: dict,
) -> HubSpotObjectRefreshResult:
    return HubSpotObjectRefreshResult(
        object_type=object_type,
        external_id=external_id,
        action="written",
        source_modified_at=_parse_datetime(raw.get("updatedAt")),
        local_synced_at=datetime.now(UTC),
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
```

- [ ] **Step 4: Run object refresh tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_object_refresh.py -v
```

Expected: PASS.

---

### Task 5: Add Core Incremental Sync Service

**Files:**

- Create or include in: `src/service/integrations/crm/records/hubspot/incremental_core_service.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_incremental_core_service.py`

- [ ] **Step 1: Implement object configuration**

Create object config in `incremental_core_service.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from src.service.integrations.crm.records.hubspot.incremental_cursor import (
    get_incremental_cursor,
    record_incremental_failure,
    record_incremental_success,
    resolve_query_start,
)
from src.service.integrations.crm.records.hubspot.object_refresh import (
    HubSpotObjectRefreshRequest,
    HubSpotObjectRefreshService,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.search_fetcher import (
    HubSpotSearchFetcher,
)

HubSpotCoreObjectType = Literal["deal", "contact", "company"]


@dataclass(frozen=True, slots=True)
class HubSpotCoreObjectConfig:
    object_type: HubSpotCoreObjectType
    object_key: str
    search_object_type: str
    modified_property: str
    first_run_lookback_days: int
    max_pages: int


CORE_OBJECT_CONFIGS: tuple[HubSpotCoreObjectConfig, ...] = (
    HubSpotCoreObjectConfig(
        object_type="deal",
        object_key="deals",
        search_object_type="deals",
        modified_property="hs_lastmodifieddate",
        first_run_lookback_days=1,
        max_pages=500,
    ),
    HubSpotCoreObjectConfig(
        object_type="contact",
        object_key="contacts",
        search_object_type="contacts",
        modified_property="lastmodifieddate",
        first_run_lookback_days=1,
        max_pages=500,
    ),
    HubSpotCoreObjectConfig(
        object_type="company",
        object_key="companies",
        search_object_type="companies",
        modified_property="hs_lastmodifieddate",
        first_run_lookback_days=1,
        max_pages=250,
    ),
)
```

- [ ] **Step 2: Implement service result types**

Add:

```python
@dataclass(frozen=True, slots=True)
class HubSpotObjectIncrementalRunResult:
    object_key: str
    ids_discovered: int
    objects_written: int
    objects_deleted: int
    skipped_locked: int
    skipped_not_found: int
    max_source_modified_at: datetime | None


@dataclass(frozen=True, slots=True)
class HubSpotCoreIncrementalRunResult:
    organisation_id: UUID
    integration_id: UUID
    started_at: datetime
    completed_at: datetime
    object_results: tuple[HubSpotObjectIncrementalRunResult, ...]
```

- [ ] **Step 3: Implement run logic**

Add:

```python
class HubSpotIncrementalCoreSyncService:
    def __init__(self, *, refresh_service: HubSpotObjectRefreshService | None = None) -> None:
        self.refresh_service = refresh_service or HubSpotObjectRefreshService()

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        token_info: IntegrationTokenInfo,
        connection_metadata: dict,
    ) -> tuple[HubSpotCoreIncrementalRunResult, dict]:
        started_at = datetime.now(UTC)
        updated_metadata = dict(connection_metadata or {})
        object_results: list[HubSpotObjectIncrementalRunResult] = []

        for config in CORE_OBJECT_CONFIGS:
            try:
                object_result = await self._run_object(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    token_info=token_info,
                    connection_metadata=updated_metadata,
                    config=config,
                )
                updated_metadata = record_incremental_success(
                    updated_metadata,
                    object_key=config.object_key,
                    completed_at=datetime.now(UTC),
                    cursor_source_modified_at=object_result.max_source_modified_at,
                    query_start=self._last_query_start,
                    ids_discovered=object_result.ids_discovered,
                    objects_written=object_result.objects_written,
                    objects_deleted=object_result.objects_deleted,
                    skipped_stale=object_result.skipped_locked + object_result.skipped_not_found,
                )
                object_results.append(object_result)
            except Exception as exc:
                updated_metadata = record_incremental_failure(
                    updated_metadata,
                    object_key=config.object_key,
                    completed_at=datetime.now(UTC),
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                )
                raise

        completed_at = datetime.now(UTC)
        return (
            HubSpotCoreIncrementalRunResult(
                organisation_id=organisation_id,
                integration_id=integration_id,
                started_at=started_at,
                completed_at=completed_at,
                object_results=tuple(object_results),
            ),
            updated_metadata,
        )

    async def _run_object(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        token_info: IntegrationTokenInfo,
        connection_metadata: dict,
        config: HubSpotCoreObjectConfig,
    ) -> HubSpotObjectIncrementalRunResult:
        now = datetime.now(UTC)
        cursor = get_incremental_cursor(connection_metadata, config.object_key)
        query_start = resolve_query_start(
            cursor,
            fallback_start=now - timedelta(days=config.first_run_lookback_days),
        )
        self._last_query_start = query_start
        changed = await HubSpotSearchFetcher(
            token_info=token_info,
            max_pages=config.max_pages,
        ).fetch_changed_ids(
            object_type=config.search_object_type,
            modified_property=config.modified_property,
            query_start=query_start,
        )

        written = 0
        deleted = 0
        locked = 0
        not_found = 0
        max_seen: datetime | None = None
        for item in changed:
            result = await self.refresh_service.refresh(
                HubSpotObjectRefreshRequest(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    object_type=config.object_type,
                    external_id=item.external_id,
                    trigger="incremental_search",
                )
            )
            if item.source_modified_at is not None and (
                max_seen is None or item.source_modified_at > max_seen
            ):
                max_seen = item.source_modified_at
            if result.action == "written":
                written += 1
            elif result.action == "deleted":
                deleted += 1
            elif result.action == "locked":
                locked += 1
            elif result.action == "not_found":
                not_found += 1

        return HubSpotObjectIncrementalRunResult(
            object_key=config.object_key,
            ids_discovered=len(changed),
            objects_written=written,
            objects_deleted=deleted,
            skipped_locked=locked,
            skipped_not_found=not_found,
            max_source_modified_at=max_seen,
        )
```

Note: In implementation, replace the temporary `self._last_query_start` with a field in `HubSpotObjectIncrementalRunResult` before committing. Add `query_start: datetime` to that dataclass and pass it into `record_incremental_success(...)`.

- [ ] **Step 4: Write tests for cursor advancement and failure behavior**

Create `tests/service/integrations/crm/records/hubspot/test_incremental_core_service.py` with:

- one test where changed deals/contacts/companies are refreshed and metadata cursors are advanced;
- one test where search raises and the previous cursor is preserved;
- one test where refresh returns `locked` and cursor still advances to avoid infinite duplicate work only if no hard failures occurred.

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_incremental_core_service.py -v
```

Expected: PASS.

---

### Task 6: Add Celery Task for Core Incremental Sync

**Files:**

- Create: `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_incremental_core_task.py`
- Modify: `src/workers/integrations_worker/config.py`
- Modify exports in task package files as needed.
- Test: `tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_task.py`

- [ ] **Step 1: Add task limits and routes**

Modify `src/workers/integrations_worker/config.py`:

```python
"integrations.sync_hubspot_incremental_core": {
    "soft": 240,
    "hard": 300,
    "retries": 2,
    "retry_delay": 60,
},
```

Add route:

```python
"integrations.sync_hubspot_incremental_core": {
    "queue": "integrations",
    "routing_key": "integrations",
},
```

- [ ] **Step 2: Implement single-integration Celery task**

Create `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_incremental_core_task.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.service.integrations.crm.records.hubspot.incremental_core_service import (
    HubSpotIncrementalCoreSyncService,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.sync_health import record_sync_failure
from src.service.integrations.sync_history_manager import SyncHistoryManager, datetime_now_naive
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

_TASK_NAME = "integrations.sync_hubspot_incremental_core"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_hubspot_incremental_core_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            token_info, metadata = await _load_incremental_context(org_id, int_id)
            result, updated_metadata = await HubSpotIncrementalCoreSyncService().run(
                organisation_id=org_id,
                integration_id=int_id,
                token_info=token_info,
                connection_metadata=metadata,
            )
        except Exception as exc:
            async for session in acquire_org_async_session(org_id):
                if sync_id is not None:
                    await SyncHistoryManager.complete_fresh(
                        session,
                        sync_id,
                        status=SyncStatus.FAILED,
                        records_processed=0,
                        error_details={"message": str(exc), "class": type(exc).__name__},
                    )
            await record_sync_failure(organisation_id=org_id, integration_id=int_id, error=exc)
            raise

        records_processed = sum(r.ids_discovered for r in result.object_results)
        async for session in acquire_org_async_session(org_id):
            await SyncHistoryManager.complete_fresh(
                session,
                sync_id,  # type: ignore[arg-type]
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                metadata={
                    "entity": "record",
                    "provider": "hubspot",
                    "sync_type": "incremental_core",
                    "objects": [r.__dict__ for r in result.object_results],
                },
            )
            integration = await session.get(KnowledgeIntegration, int_id)
            if integration is not None:
                integration.connection_metadata = updated_metadata
                integration.last_synced_at = datetime_now_naive()
                session.add(integration)
                await session.commit()

        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "records_processed": records_processed,
        }

    try:
        return run_async(_run())
    except SoftTimeLimitExceeded:
        run_async(
            record_sync_failure(
                organisation_id=org_id,
                integration_id=int_id,
                error=TimeoutError("HubSpot incremental core sync timed out"),
            )
        )
        raise


async def _load_incremental_context(
    organisation_id: UUID,
    integration_id: UUID,
) -> tuple[IntegrationTokenInfo, dict[str, Any]]:
    async for session in acquire_org_async_session(organisation_id):
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
            col(KnowledgeIntegration.organisation_id) == organisation_id,
        )
        integration = (await session.execute(stmt)).scalar_one_or_none()
        if integration is None:
            raise ValueError(f"Integration {integration_id} not found")
        if integration.integration_type != IntegrationType.HUBSPOT:
            raise ValueError(f"Integration {integration_id} is not HubSpot")
        if not integration.is_active:
            raise ValueError(f"Integration {integration_id} is inactive")
        metadata = dict(integration.connection_metadata or {})
        if metadata.get("initial_sync_status") != "success":
            raise ValueError("HubSpot incremental sync requires successful initial sync")
        return IntegrationTokenInfo.from_integration(integration, organisation_id), metadata
    raise ValueError("Could not acquire org session")
```

- [ ] **Step 3: Add imports so worker discovers the task**

Update package `__init__.py` files following existing HubSpot task import style.

- [ ] **Step 4: Add focused task tests**

Test at minimum:

- context loader rejects inactive integration;
- context loader rejects `initial_sync_status != "success"`;
- task writes updated connection metadata on success;
- task completes sync history as failed when service raises.

Run:

```bash
uv run pytest tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_task.py -v
```

Expected: PASS.

---

### Task 7: Add All-Orgs Dispatcher Task

**Files:**

- Create: `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_incremental_core_all_orgs_task.py`
- Modify: `src/workers/integrations_worker/config.py`
- Test: `tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_all_orgs_task.py`

- [ ] **Step 1: Implement all-org task**

Create a task that:

1. Reads active approved organisations from master schema.
2. For each org, reads active HubSpot integrations.
3. Filters to `connection_metadata.initial_sync_status == "success"`.
4. Enqueues `integrations.sync_hubspot_incremental_core` for each integration.
5. Returns counts: orgs scanned, integrations queued, skipped not initialized, skipped inactive.

Use `send_integrations_task_async` or `task_signature(...).apply_async()` consistent with existing task dispatch patterns.

- [ ] **Step 2: Add task config**

Add:

```python
"integrations.sync_hubspot_incremental_core_all_orgs": {
    "soft": 120,
    "hard": 180,
    "retries": 0,
},
```

Route to queue `integrations`.

- [ ] **Step 3: Test all-org filtering**

Tests must prove:

- inactive orgs are skipped;
- inactive integrations are skipped;
- non-HubSpot integrations are skipped;
- HubSpot integrations without successful initial sync are skipped;
- eligible integrations are queued exactly once.

Run:

```bash
uv run pytest tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_all_orgs_task.py -v
```

Expected: PASS.

---

### Task 8: Wire Documentation and Operational Schedule

**Files:**

- Modify: `docs/Technical Documentation/integrations/hubspot-webhooks-incremental-sync.md`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`
- Optional infra follow-up: separate infra plan/PR for CronJob.

- [ ] **Step 1: Update the design doc status**

Add an implementation status section:

```markdown
## Implementation Status

Implemented in this pass:

- HubSpot CRM Search changed-id fetcher.
- Per-object incremental cursor metadata.
- Single-contact fetch/persist path.
- Shared object refresh service for deals, contacts, companies.
- Core incremental Celery task.
- All-org dispatcher task for CronJob scheduling.

Still deferred:

- tasks and notes incremental sync
- contact webhooks
- restore/merge/associationChange webhook handling
- full reconciliation job
```

- [ ] **Step 2: Update provider sync doc**

In `docs/Technical Documentation/integrations/sync-by-provider.md`, update the HubSpot CRM section with:

```markdown
- **Incremental core sync**:
  - `integrations.sync_hubspot_incremental_core_all_orgs`
  - dispatches `integrations.sync_hubspot_incremental_core`
  - every 5 minutes when scheduled by Kubernetes CronJob
  - covers deals, contacts, companies/accounts
  - uses HubSpot CRM Search modified timestamps with a 10-minute overlap
```

- [ ] **Step 3: Add infra handoff note**

Document the CronJob requirement:

```text
Schedule: every 5 minutes
Task: integrations.sync_hubspot_incremental_core_all_orgs
Queue: integrations
Environment: dev first, then prod after observing cursor lag and API usage
```

Do not edit Terraform/Kubernetes manifests in this backend branch unless the user asks.

---

### Task 9: Verification Pass

**Files:** all touched files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_search_fetcher.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/test_incremental_cursor.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/contacts/test_single_contact_sync.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/test_object_refresh.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/test_incremental_core_service.py -v
uv run pytest tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_task.py -v
uv run pytest tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_incremental_core_all_orgs_task.py -v
```

Expected: PASS.

- [ ] **Step 2: Run related existing tests**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/hubspot/deals/test_handler.py -v
uv run pytest tests/service/integrations/webhooks/hubspot/companies/test_handler.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/deals/test_deal_writer.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/companies/test_company_writer.py -v
uv run pytest tests/service/integrations/crm/records/test_hubspot_record_imports.py -v
```

Expected: PASS.

- [ ] **Step 3: Run lint and type checks before PR**

Run only once near the end:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

Expected: PASS.

---

## Edge Cases Covered

- HubSpot Search delayed indexing: 10-minute overlap.
- Duplicate changed ids in one search window: search fetcher dedupes ids.
- Duplicate webhook plus incremental refresh: object refresh lock plus idempotent writer.
- Redis unavailable: lock should fail open through existing task lock behavior.
- Search page ceiling: raise and keep cursor unchanged.
- Provider 429: task fails/retries and keeps cursor unchanged.
- Provider 404 during deal/company/contact refresh: soft-delete the scoped local row.
- Contact soft-deleted locally then returned by HubSpot: `apply_single` reactivates by clearing `is_deleted`.
- Company one-record refresh: uses `apply_single` so unrelated companies are not deactivated.
- Deal one-record refresh: uses `_persist_raw_deals(...)`, not a new writer.
- Initial sync incomplete: incremental task refuses to run.
- Non-HubSpot integration: incremental task refuses to run.
- Inactive integration: incremental task refuses to run.
- Inactive org: all-org task skips.
- Cursor partial failure: cursor remains unchanged.
- No changed ids: cursor behavior should preserve previous cursor unless implementation explicitly records run timestamp separately.
- Changed object has no modified timestamp: refresh still runs, but cursor only advances from max known modified timestamp.

## Full Failure and Possibility Matrix

### Provider API Possibilities

| Case | Expected behavior |
|------|-------------------|
| HubSpot Search returns 401 and token refresh works | Retry through `call_with_token_retry`; continue run. |
| HubSpot Search returns 401 after refresh | Mark run failed, do not advance cursor, surface auth failure. |
| HubSpot Search returns 403 | Mark run failed, do not advance cursor, likely scope/config issue. |
| HubSpot Search returns 429 | Let task retry through Celery policy, do not advance cursor. |
| HubSpot Search returns 5xx | Let task retry, do not advance cursor. |
| HubSpot Search response is malformed | Raise provider error, do not advance cursor. |
| HubSpot Search returns duplicate ids across overlap | Deduplicate before refresh. |
| HubSpot Search returns more pages than configured ceiling | Fail the object run, keep cursor unchanged, log page ceiling. |
| HubSpot object fetch returns 404 after search found id | Treat as deleted/archived and soft-delete scoped local row. |
| HubSpot object fetch returns object with missing properties | Persist what normalizer supports; do not clear unsupported fields unless writer already does so intentionally. |
| HubSpot object fetch returns no modified timestamp | Refresh object but do not use it to advance max cursor. |
| HubSpot object modified during the run | Single-object fetch gets current provider state; cursor advances from returned current modified timestamp. |
| HubSpot Search index lags behind object API | 10-minute overlap catches late-indexed records in later runs. |

### Cursor Possibilities

| Case | Expected behavior |
|------|-------------------|
| Existing incremental cursor exists | Query from cursor minus lookback. |
| No incremental cursor but full-sync object timestamp exists | Seed query from full-sync object timestamp. |
| No object timestamp but integration `last_synced_at` exists | Use as fallback only for first run and record that fallback was used. |
| No usable fallback exists | Skip object and log `missing_cursor_seed`; do not run a huge historical search. |
| Object run succeeds with changed ids | Advance cursor to max refreshed provider modified timestamp. |
| Object run succeeds with no ids | Record last checked time, but do not jump past the overlap in a way that can miss delayed Search indexing. |
| Some ids refresh and one id fails | Treat object run as failed, keep cursor unchanged. |
| Run is cancelled or hard-time-limited | Keep cursor unchanged. |
| Cursor value is malformed | Ignore malformed cursor, use fallback seed, and overwrite only after a successful run. |
| Clock skew between app and HubSpot | Cursor is based on HubSpot modified timestamps, not local wall-clock time except fallback metadata. |

### Webhook Possibilities

| Case | Expected behavior |
|------|-------------------|
| Webhook arrives before incremental search sees same change | Webhook refresh writes current object; later search refresh is idempotent. |
| Incremental search refreshes before webhook arrives | Search refresh writes current object; later webhook refresh is idempotent. |
| Webhook and search run concurrently | Per-object lock avoids duplicate fetch where possible; writer idempotency handles fail-open. |
| Webhook event is dropped by HubSpot or by our endpoint | Incremental search catches the change if the modified timestamp changes. |
| Webhook property is not subscribed | Incremental search catches the change if HubSpot updates the modified timestamp. |
| Webhook event payload is stale | Ignore payload values; fetch current object from HubSpot. |
| Webhook duplicate arrives inside dedupe TTL | Existing dedupe/coalescing drops or collapses duplicate. |
| Webhook duplicate arrives after dedupe TTL | Single-object refresh is safe and idempotent. |
| Webhook deletion arrives | Existing deal/company delete path soft-deletes immediately; future object refresh 404 path should match it. |
| Contact webhook desired later | Add after core incremental path, using the same object refresh service. |

### Data Model Possibilities

| Case | Expected behavior |
|------|-------------------|
| Local row belongs to another organisation | Never touched because all queries include `organisation_id`. |
| Same HubSpot external id appears in another integration | Never touched because all writes are scoped by `integration_id`. |
| Local row is soft-deleted and provider returns object again | Reactivate only the matching row. |
| Changed company/deal/contact has association changes only | Single-object fetch should request associations; if HubSpot modified timestamp does not change for association-only edits, this remains a deferred reconciliation risk. |
| Deal primary contact changes because contact data changed | Existing contact sync backfill should run in single-contact path where applicable. |
| Owner/user reference is unknown | Preserve existing writer behavior; later owner refresh can resolve it. |
| Pipeline/stage reference is unknown | Preserve existing writer behavior; two-hour pipeline refresh is a follow-up. |
| Custom property is newly added in HubSpot | Core incremental uses current dynamic/full property list where existing deal/company code supports it; schema sync remains daily/deferred. |
| Property removed in HubSpot | Existing normalizer/writer behavior decides whether missing clears or preserves. Do not invent clear semantics in incremental code. |

### Operational Possibilities

| Case | Expected behavior |
|------|-------------------|
| All-org dispatcher finds many integrations | Fan out one task per integration instead of doing all work in one long task. |
| One integration fails | It must not block other integrations from being queued or completed. |
| Worker restarts mid-run | Cursor remains old because success metadata was not committed. |
| Redis lock is stale | TTL releases it; later run catches changes due overlap. |
| CronJob overlaps with prior run | Integration-level or object-level locks prevent excessive duplicate work; writer idempotency remains required. |
| API usage approaches HubSpot limits | Increase cadence, lower page ceiling, or add per-integration throttling before enabling deferred tasks/notes. |
| Initial deployment in dev | Run CronJob every 5 minutes in dev first, observe logs, cursor lag, and HubSpot usage before prod. |
| Bad cursor written | Disable CronJob and reset only `connection_metadata.record_sync.<object>.incremental`. |

## Rollback Plan

If incremental sync causes load or bad writes:

1. Disable the Kubernetes CronJob or stop dispatching `sync_hubspot_incremental_core_all_orgs`.
2. Existing initial sync and webhooks continue to work.
3. Cursor metadata can remain in `connection_metadata`; it is ignored when task is disabled.
4. If a bad cursor is written, reset only:

```json
connection_metadata.record_sync.<object>.incremental
```

Do not clear the full `record_sync` object because it contains initial/full phase status.

## Execution Order

Recommended order:

1. Task 1: Cursor helpers.
2. Task 2: Search fetcher.
3. Task 3: Contact single-object path.
4. Task 4: Object refresh service.
5. Task 5: Core incremental service.
6. Task 6: Single-integration Celery task.
7. Task 7: All-org dispatcher.
8. Task 8: Docs.
9. Task 9: Verification.

Commit after each logical task if executing manually.
