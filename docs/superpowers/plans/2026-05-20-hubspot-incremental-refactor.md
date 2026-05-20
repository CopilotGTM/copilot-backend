# HubSpot Incremental Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor HubSpot core incremental sync so the Celery task structure stays production-safe while object-specific refresh logic moves out of one oversized service file.

**Architecture:** Keep the existing two-tier task split: one all-org fan-out task and one per-integration execution task. Move per-object refresh logic into focused refresher classes under a new `hubspot/incremental/` package, while the core service only orchestrates token loading, cursor calculation, changed-id search, refresher dispatch, and metadata persistence.

**Tech Stack:** Python 3.11, FastAPI service layer, Celery integrations worker, SQLModel/SQLAlchemy, HubSpot CRM v3 Search/Object APIs, Redis/Celery queueing, Ruff, pytest.

---

## Current State

The current runtime path is:

1. `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_core_incremental_all_orgs_task.py`
   - Scans approved organisations.
   - Finds active HubSpot integrations.
   - Queues one per-integration child task.

2. `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_core_incremental_task.py`
   - Receives one `organisation_id` and one `integration_id`.
   - Calls `HubSpotCoreIncrementalSyncService.run(...)`.
   - Requests `("deals", "companies", "contacts", "tasks", "notes")`.

3. `src/service/integrations/crm/records/hubspot/core_incremental_sync_service.py`
   - Loads token context and metadata.
   - Loops every object type.
   - Searches changed HubSpot ids.
   - Fetches full object snapshots.
   - Persists through object-specific sync services.
   - Saves incremental cursor metadata.

The task split is good. The service shape is the problem: one file owns orchestration plus every object-specific fetch/write branch.

## Target File Structure

Create a focused incremental package:

```text
src/service/integrations/crm/records/hubspot/
  core_incremental_sync_service.py
  incremental_cursor_metadata.py
  incremental/
    __init__.py
    object_refresher.py
    registry.py
    deal_incremental_refresher.py
    company_incremental_refresher.py
    contact_incremental_refresher.py
    task_incremental_refresher.py
    note_incremental_refresher.py
```

Responsibilities:

- `core_incremental_sync_service.py`
  - Keeps `HubSpotCoreIncrementalSyncService`.
  - Owns org/integration validation, token loading, metadata read/write, cursor calculation, changed-id search, and result aggregation.
  - Does not know how to fetch or persist a deal/company/contact/task/note.

- `incremental/object_refresher.py`
  - Defines the common result type and protocol/base contract for object refreshers.
  - Keeps the shared shape: input is `IntegrationTokenInfo + ChangedHubSpotObject`, output is `(written_count, skipped_stale_count)`.

- `incremental/registry.py`
  - Maps object keys to refresher classes.
  - Defines the supported object list in one place.

- `incremental/*_incremental_refresher.py`
  - Each file owns one object family.
  - Each refresher fetches the full object snapshot and calls the existing object sync service.
  - Each refresher keeps current behavior for vanished objects: return `(0, 0)` and leave deletes to webhook/delete reconciliation paths.

Do not split into separate Celery tasks per object in this refactor. We only need that later if product needs different schedules, timeouts, or retry policies per object family.

---

### Task 1: Add Object Refresher Contract

**Files:**
- Create: `src/service/integrations/crm/records/hubspot/incremental/__init__.py`
- Create: `src/service/integrations/crm/records/hubspot/incremental/object_refresher.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py`

- [ ] **Step 1: Create the incremental package marker**

Create `src/service/integrations/crm/records/hubspot/incremental/__init__.py`:

```python
"""HubSpot incremental sync object refreshers."""
```

- [ ] **Step 2: Create the shared refresher contract**

Create `src/service/integrations/crm/records/hubspot/incremental/object_refresher.py`:

```python
"""Contracts for HubSpot incremental object refreshers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)


@dataclass(slots=True, frozen=True)
class HubSpotIncrementalRefreshResult:
    """Write outcome for one changed HubSpot object snapshot."""

    written_count: int = 0
    skipped_stale_count: int = 0


class HubSpotIncrementalObjectRefresher(Protocol):
    """Fetch and persist one changed HubSpot object discovered by Search."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        """Refresh one provider object and return writer counters."""
```

- [ ] **Step 3: Run the existing focused service tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py -q
```

Expected: tests still pass because the new contract is not wired yet.

---

### Task 2: Move Deal Refresh Logic Into Its Own Refresher

**Files:**
- Create: `src/service/integrations/crm/records/hubspot/incremental/deal_incremental_refresher.py`
- Modify: `src/service/integrations/crm/records/hubspot/core_incremental_sync_service.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py`

- [ ] **Step 1: Create the deal refresher**

Create `src/service/integrations/crm/records/hubspot/incremental/deal_incremental_refresher.py`:

```python
"""Incremental refresh support for HubSpot deals."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.deals.deal_sync_service import (
    HubSpotDealRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_deal_fetcher import (
    HubSpotSingleDealFetcher,
)


class HubSpotDealIncrementalRefresher:
    """Refresh one deal through the same full-snapshot writer used by webhooks."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        deal_service = HubSpotDealRecordSyncService()
        property_list = await deal_service._build_property_list(token_info=token_info)
        raw_deal = await HubSpotSingleDealFetcher(
            token_info=token_info,
            deal_id=changed_object.object_id,
            properties=property_list,
        ).fetch()
        if raw_deal is None:
            return HubSpotIncrementalRefreshResult()

        result = await deal_service._persist_raw_deals(
            organisation_id=token_info.organisation_id,
            integration_id=token_info.integration_id,
            raw_deals=[raw_deal],
            property_list_size=len(property_list),
        )
        return HubSpotIncrementalRefreshResult(
            written_count=result.deals_created + result.deals_updated,
            skipped_stale_count=result.deals_skipped_stale,
        )
```

- [ ] **Step 2: Replace deal-specific methods in the core service**

In `core_incremental_sync_service.py`, import:

```python
from src.service.integrations.crm.records.hubspot.incremental.deal_incremental_refresher import (
    HubSpotDealIncrementalRefresher,
)
```

Replace the deal branch inside `sync_object(...)`:

```python
if object_key == "deals":
    refresh_result = await HubSpotDealIncrementalRefresher().refresh(
        token_info=token_info,
        changed_object=changed_object,
    )
    written = refresh_result.written_count
    skipped = refresh_result.skipped_stale_count
```

Remove these now-unused methods from `HubSpotCoreIncrementalSyncService`:

```python
_refresh_deal
_build_deal_property_list
_persist_deals
```

- [ ] **Step 3: Run the focused tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py -q
```

Expected: existing deal incremental behavior still passes.

---

### Task 3: Move Company And Contact Refresh Logic

**Files:**
- Create: `src/service/integrations/crm/records/hubspot/incremental/company_incremental_refresher.py`
- Create: `src/service/integrations/crm/records/hubspot/incremental/contact_incremental_refresher.py`
- Modify: `src/service/integrations/crm/records/hubspot/core_incremental_sync_service.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py`

- [ ] **Step 1: Create the company refresher**

Create `src/service/integrations/crm/records/hubspot/incremental/company_incremental_refresher.py`:

```python
"""Incremental refresh support for HubSpot companies."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.companies.company_sync_service import (
    HubSpotCompanyRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_company_fetcher import (
    HubSpotSingleCompanyFetcher,
)


class HubSpotCompanyIncrementalRefresher:
    """Refresh one company without triggering full-sync deactivation behavior."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_company = await HubSpotSingleCompanyFetcher(
            token_info=token_info,
            company_id=changed_object.object_id,
        ).fetch()
        if raw_company is None:
            return HubSpotIncrementalRefreshResult()

        write_result, _ = await HubSpotCompanyRecordSyncService().persist_raw_company(
            organisation_id=token_info.organisation_id,
            integration_id=token_info.integration_id,
            raw_company=raw_company,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.companies_created
                + write_result.companies_updated
                + write_result.companies_reactivated
            ),
            skipped_stale_count=write_result.companies_skipped_stale,
        )
```

- [ ] **Step 2: Create the contact refresher**

Create `src/service/integrations/crm/records/hubspot/incremental/contact_incremental_refresher.py`:

```python
"""Incremental refresh support for HubSpot contacts."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.contacts.contact_sync_service import (
    HubSpotContactRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_contact_fetcher import (
    HubSpotSingleContactFetcher,
)


class HubSpotContactIncrementalRefresher:
    """Refresh one contact through the single-record contact writer."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_contact = await HubSpotSingleContactFetcher(
            token_info=token_info,
            contact_id=changed_object.object_id,
        ).fetch()
        if raw_contact is None:
            return HubSpotIncrementalRefreshResult()

        write_result, _ = await HubSpotContactRecordSyncService().persist_raw_contact(
            organisation_id=token_info.organisation_id,
            integration_id=token_info.integration_id,
            raw_contact=raw_contact,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.contacts_created
                + write_result.contacts_updated
                + write_result.contacts_reactivated
            ),
            skipped_stale_count=write_result.contacts_skipped_stale,
        )
```

- [ ] **Step 3: Wire company and contact branches**

In `core_incremental_sync_service.py`, import both refreshers and replace the company/contact branches with:

```python
elif object_key == "companies":
    refresh_result = await HubSpotCompanyIncrementalRefresher().refresh(
        token_info=token_info,
        changed_object=changed_object,
    )
    written = refresh_result.written_count
    skipped = refresh_result.skipped_stale_count
elif object_key == "contacts":
    refresh_result = await HubSpotContactIncrementalRefresher().refresh(
        token_info=token_info,
        changed_object=changed_object,
    )
    written = refresh_result.written_count
    skipped = refresh_result.skipped_stale_count
```

Remove these methods from `HubSpotCoreIncrementalSyncService`:

```python
_refresh_company
_refresh_contact
_persist_company
_persist_contact
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py -q
```

Expected: company/contact incremental behavior still passes.

---

### Task 4: Move Task And Note Refresh Logic

**Files:**
- Create: `src/service/integrations/crm/records/hubspot/incremental/task_incremental_refresher.py`
- Create: `src/service/integrations/crm/records/hubspot/incremental/note_incremental_refresher.py`
- Modify: `src/service/integrations/crm/records/hubspot/core_incremental_sync_service.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py`

- [ ] **Step 1: Create the task refresher**

Create `src/service/integrations/crm/records/hubspot/incremental/task_incremental_refresher.py`:

```python
"""Incremental refresh support for HubSpot tasks."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
)
from src.service.integrations.crm.records.hubspot.tasks.task_sync_service import (
    HubSpotTaskRecordSyncService,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_task_fetcher import (
    HubSpotSingleTaskFetcher,
)


class HubSpotTaskIncrementalRefresher:
    """Refresh one HubSpot task activity from a full provider snapshot."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_task = await HubSpotSingleTaskFetcher(
            token_info=token_info,
            task_id=changed_object.object_id,
        ).fetch()
        if raw_task is None:
            return HubSpotIncrementalRefreshResult()

        write_result = await HubSpotTaskRecordSyncService().persist_raw_task(
            organisation_id=token_info.organisation_id,
            integration_id=token_info.integration_id,
            raw_task=raw_task,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.tasks_created
                + write_result.tasks_updated
                + write_result.tasks_reactivated
            ),
            skipped_stale_count=write_result.tasks_skipped_stale,
        )
```

- [ ] **Step 2: Create the note refresher**

Create `src/service/integrations/crm/records/hubspot/incremental/note_incremental_refresher.py`:

```python
"""Incremental refresh support for HubSpot notes."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
)
from src.service.integrations.crm.records.hubspot.notes.note_sync_service import (
    HubSpotNoteRecordSyncService,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_note_fetcher import (
    HubSpotSingleNoteFetcher,
)


class HubSpotNoteIncrementalRefresher:
    """Refresh one HubSpot note activity from a full provider snapshot."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_note = await HubSpotSingleNoteFetcher(
            token_info=token_info,
            note_id=changed_object.object_id,
        ).fetch()
        if raw_note is None:
            return HubSpotIncrementalRefreshResult()

        write_result = await HubSpotNoteRecordSyncService().persist_raw_note(
            organisation_id=token_info.organisation_id,
            integration_id=token_info.integration_id,
            raw_note=raw_note,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.notes_created
                + write_result.notes_updated
                + write_result.notes_reactivated
            ),
            skipped_stale_count=write_result.notes_skipped_stale,
        )
```

- [ ] **Step 3: Wire task and note branches**

In `core_incremental_sync_service.py`, import both refreshers and replace the task/note branches with:

```python
elif object_key == "tasks":
    refresh_result = await HubSpotTaskIncrementalRefresher().refresh(
        token_info=token_info,
        changed_object=changed_object,
    )
    written = refresh_result.written_count
    skipped = refresh_result.skipped_stale_count
else:
    refresh_result = await HubSpotNoteIncrementalRefresher().refresh(
        token_info=token_info,
        changed_object=changed_object,
    )
    written = refresh_result.written_count
    skipped = refresh_result.skipped_stale_count
```

Remove these methods from `HubSpotCoreIncrementalSyncService`:

```python
_refresh_task
_refresh_note
_persist_task
_persist_note
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py -q
```

Expected: task/note incremental behavior still passes if changed-object search supports those object types. If this fails with `Unsupported HubSpot search object type: tasks`, proceed to Task 6 before re-running the full focused test file.

---

### Task 5: Add A Refresher Registry And Remove The Remaining Branching

**Files:**
- Create: `src/service/integrations/crm/records/hubspot/incremental/registry.py`
- Modify: `src/service/integrations/crm/records/hubspot/core_incremental_sync_service.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py`

- [ ] **Step 1: Create the registry**

Create `src/service/integrations/crm/records/hubspot/incremental/registry.py`:

```python
"""Registry for HubSpot incremental object refreshers."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.incremental.company_incremental_refresher import (
    HubSpotCompanyIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.contact_incremental_refresher import (
    HubSpotContactIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.deal_incremental_refresher import (
    HubSpotDealIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.note_incremental_refresher import (
    HubSpotNoteIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalObjectRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.task_incremental_refresher import (
    HubSpotTaskIncrementalRefresher,
)

HUBSPOT_INCREMENTAL_OBJECT_KEYS = ("deals", "companies", "contacts", "tasks", "notes")


def build_hubspot_incremental_refresher_registry() -> dict[
    str,
    HubSpotIncrementalObjectRefresher,
]:
    """Create refreshers for one incremental run."""
    return {
        "deals": HubSpotDealIncrementalRefresher(),
        "companies": HubSpotCompanyIncrementalRefresher(),
        "contacts": HubSpotContactIncrementalRefresher(),
        "tasks": HubSpotTaskIncrementalRefresher(),
        "notes": HubSpotNoteIncrementalRefresher(),
    }
```

- [ ] **Step 2: Wire the registry into the core service**

In `core_incremental_sync_service.py`, import:

```python
from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
    build_hubspot_incremental_refresher_registry,
)
```

Replace:

```python
_SUPPORTED_OBJECTS = frozenset({"deals", "companies", "contacts", "tasks", "notes"})
```

with:

```python
_SUPPORTED_OBJECTS = frozenset(HUBSPOT_INCREMENTAL_OBJECT_KEYS)
```

Inside `run(...)`, create the registry once:

```python
refresher_registry = build_hubspot_incremental_refresher_registry()
```

Pass it to `sync_object(...)`:

```python
result = await self.sync_object(
    object_key=object_key,
    token_info=token_info,
    metadata=updated_metadata,
    refresher_registry=refresher_registry,
    cursor_updated_at=now,
)
```

Update `sync_object(...)` signature:

```python
async def sync_object(
    self,
    *,
    object_key: str,
    token_info: IntegrationTokenInfo,
    metadata: dict[str, Any],
    refresher_registry: dict[str, HubSpotIncrementalObjectRefresher],
    cursor_updated_at: datetime | None = None,
) -> HubSpotIncrementalObjectResult:
```

Replace the object-specific branch block with:

```python
refresher = refresher_registry[object_key]
refresh_result = await refresher.refresh(
    token_info=token_info,
    changed_object=changed_object,
)
written_count += refresh_result.written_count
skipped_stale_count += refresh_result.skipped_stale_count
```

- [ ] **Step 3: Remove now-unused imports**

Remove object-specific sync service and single-fetcher imports from `core_incremental_sync_service.py`. The core service should no longer import:

```python
HubSpotCompanyRecordSyncService
HubSpotContactRecordSyncService
HubSpotDealRecordSyncService
HubSpotNoteRecordSyncService
HubSpotTaskRecordSyncService
HubSpotSingleCompanyFetcher
HubSpotSingleContactFetcher
HubSpotSingleDealFetcher
HubSpotSingleNoteFetcher
HubSpotSingleTaskFetcher
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py -q
```

Expected: tests pass, and the core service is now orchestration-only.

---

### Task 6: Fix Or Explicitly Gate Tasks/Notes Changed-Object Search

**Files:**
- Modify: `src/workers/integrations_worker/providers/crm/hubspot/changed_objects_search_fetcher.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_changed_objects_search_fetcher.py`
- Test: `tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py`

- [ ] **Step 1: Verify whether a search fetcher test file already exists**

Run:

```bash
rg "HubSpotChangedObjectsSearchFetcher|ChangedHubSpotObject" tests/workers tests/service -n
```

Expected: find existing tests or confirm a new test file is needed.

- [ ] **Step 2: Add tests for supported object types**

In the changed-object search fetcher tests, add coverage that these object types can be constructed:

```python
import pytest
from datetime import UTC, datetime

from src.workers.integrations_worker.providers.crm.hubspot.changed_objects_search_fetcher import (
    HubSpotChangedObjectsSearchFetcher,
)


@pytest.mark.parametrize(
    "object_type",
    ["deals", "companies", "contacts", "tasks", "notes"],
)
def test_changed_objects_search_fetcher_supports_core_incremental_objects(
    token_info,
    object_type,
):
    fetcher = HubSpotChangedObjectsSearchFetcher(
        token_info=token_info,
        object_type=object_type,
        modified_since=datetime(2026, 5, 20, tzinfo=UTC),
    )

    assert fetcher.object_type == object_type
```

If the existing test fixture is not called `token_info`, use the fixture already used in that test file.

- [ ] **Step 3: Add tasks and notes modified properties**

In `changed_objects_search_fetcher.py`, update `_MODIFIED_PROPERTIES`:

```python
_MODIFIED_PROPERTIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "deals": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
    "companies": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
    "contacts": ("lastmodifieddate", ("lastmodifieddate", "hs_lastmodifieddate")),
    "tasks": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
    "notes": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
}
```

- [ ] **Step 4: Run the search fetcher tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_changed_objects_search_fetcher.py -q
```

Expected: tasks and notes are now supported by the changed-object discovery layer.

- [ ] **Step 5: Run the core incremental service tests again**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py -q
```

Expected: task/note incremental paths no longer fail due to unsupported search object type.

---

### Task 7: Keep Celery Task Split, But Centralize Object Keys

**Files:**
- Modify: `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_core_incremental_task.py`
- Modify: `src/workers/integrations_worker/tasks/crm/hubspot/hubspot_core_incremental_all_orgs_task.py`
- Test: `tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_task.py`
- Test: `tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_all_orgs_task.py`

- [ ] **Step 1: Use the registry object list in the child task**

In `hubspot_core_incremental_task.py`, import:

```python
from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
)
```

Replace:

```python
_OBJECT_KEYS = ("deals", "companies", "contacts", "tasks", "notes")
```

with:

```python
_OBJECT_KEYS = HUBSPOT_INCREMENTAL_OBJECT_KEYS
```

- [ ] **Step 2: Use the registry object list in fan-out readiness**

In `hubspot_core_incremental_all_orgs_task.py`, import:

```python
from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
)
```

Replace:

```python
for object_key in ("deals", "companies", "contacts", "tasks", "notes")
```

with:

```python
for object_key in HUBSPOT_INCREMENTAL_OBJECT_KEYS
```

- [ ] **Step 3: Run task tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_task.py tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_all_orgs_task.py -q
```

Expected: task behavior stays the same, but supported object keys have one source of truth.

---

### Task 8: Final Verification

**Files:**
- Verify all changed source and tests.

- [ ] **Step 1: Run focused incremental tests**

Run:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py tests/workers/integrations_worker/providers/crm/hubspot/test_changed_objects_search_fetcher.py tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_task.py tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_all_orgs_task.py -q
```

Expected: all focused incremental tests pass.

- [ ] **Step 2: Run Ruff on touched files**

Run:

```bash
uv run ruff check src/service/integrations/crm/records/hubspot/core_incremental_sync_service.py src/service/integrations/crm/records/hubspot/incremental src/workers/integrations_worker/providers/crm/hubspot/changed_objects_search_fetcher.py src/workers/integrations_worker/tasks/crm/hubspot/hubspot_core_incremental_task.py src/workers/integrations_worker/tasks/crm/hubspot/hubspot_core_incremental_all_orgs_task.py tests/service/integrations/crm/records/hubspot/test_core_incremental_sync_service.py tests/workers/integrations_worker/providers/crm/hubspot/test_changed_objects_search_fetcher.py tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_task.py tests/workers/integrations_worker/tasks/crm/hubspot/test_hubspot_core_incremental_all_orgs_task.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Run mypy only when preparing PR/push**

Run at PR/push stage:

```bash
uv run mypy src/
```

Expected: no new type errors from the refactor.

---

## Review Checklist

- [ ] The all-org fan-out task remains separate from the per-integration child task.
- [ ] No object-specific fetch/write logic remains inside `core_incremental_sync_service.py`.
- [ ] Supported incremental object keys have one source of truth.
- [ ] Deals, companies, contacts, tasks, and notes all have explicit refreshers.
- [ ] Tasks and notes are either supported by changed-object search or explicitly removed from the supported object list. Do not leave the current mismatch.
- [ ] Incremental cursor behavior remains provider-timestamp based with the 10-minute lookback.
- [ ] Empty searches still preserve the previous provider cursor.
- [ ] Vanished objects during incremental refresh still return `(0, 0)` and do not soft-delete from this upsert repair path.
