# CRM Owner Account Contact Delete Mutation Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase 5B and Phase 5C by making owner, account, primary-contact, and delete deal writes emit production-grade `crm_mutation_events` rows without creating fake workflow triggers.

**Architecture:** Keep `DealWriteService` as the canonical write boundary for now, but stop adding one-off event builders. Replace the lifecycle-only event recorder with a neutral action-event recorder that can handle triggerable owner/delete actions and audit-only account/contact/display-refresh actions. Use diff-first writes: compute old/new evidence, skip no-op writes, then mutate the deal and insert one mutation event in the same transaction. API callers may default `source="api"`, but the service methods must accept source context so future agent/workflow writes are not mislabeled.

**Tech Stack:** Python 3.11, FastAPI service layer, SQLModel/SQLAlchemy async sessions, PostgreSQL JSONB-backed `crm_mutation_events`, pytest, Ruff.

Status on 2026-06-17: account/contact audit events are implemented. Owner/delete
events are implemented by the focused owner/delete plan.

---

## Current Code Map

**Main service file**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Relevant existing methods:
  - `assign_owner()` currently mutates `owner_id`, `owner_name`, `owner_email`, updates timestamps, and flushes.
  - `update_account()` currently upserts/links company data and may mutate `company_id`, `company_name`, `company_domain`.
  - `update_primary_contact()` currently upserts/links primary contact data and may mutate `primary_contact_id`, `primary_contact_name`, `primary_contact_email`.
  - `delete_deal()` currently soft-deletes by setting `is_deleted=True`, `deal_status=closed`, timestamps, and `properties.deleted_via`.
  - `_record_deal_lifecycle_event()` exists but is too narrowly named for owner/account/contact/delete.
  - `_build_deal_changed_fields()` is already the canonical direct deal-field diff helper.
  - `_derive_deal_semantic_event_types()` already maps `owner_id -> crm.deal.owner_changed`.

**Existing tests**

- Modify or extend: `tests/service/crm/test_deal_write_service_account_updates.py`
- Create: `tests/service/crm/test_deal_write_service_mutation_events.py`
- Keep existing lifecycle tests in `tests/service/crm/test_deal_write_service_stage_history.py`; do not add more non-stage tests there unless required by imports.

**Docs**

- Modify: `docs/superpowers/plans/2026-06-16-crm-write-mutation-contract.md`
- Add Phase 5B and Phase 5C status after implementation.

---

## Event Contract Decisions

### Owner Change

Owner identity assignment is triggerable only when `owner_id` changes.

```json
{
  "event_type": "crm.deal.owner_changed",
  "operation": "assign_owner",
  "semantic_event_types": ["crm.deal.owner_changed"],
  "changed_fields": {
    "owner_id": {"old": "old-user-id", "new": "new-user-id"},
    "owner_name": {"old": "Old Owner", "new": "New Owner"},
    "owner_email": {"old": "old@example.com", "new": "new@example.com"}
  }
}
```

Rules:

- Assigning a different owner creates one triggerable `crm.deal.owner_changed` event.
- Clearing an owner creates one triggerable `crm.deal.owner_changed` event with owner fields changed to `null`.
- Assigning the same owner with the same display snapshot is a no-op.
- Assigning the same `owner_id` but changed `owner_name` or `owner_email` is not an owner-change workflow trigger. It creates an audit-only `crm.deal.owner_snapshot_refreshed` event with `semantic_event_types=[]`.
- Workflow matching must use `semantic_event_types`; it must not infer an owner workflow from `owner_name` or `owner_email` changes.
- The frontend/API/agent must not split `owner_id`, `owner_name`, and `owner_email` into separate events.

Display-only owner refresh example:

```json
{
  "event_type": "crm.deal.owner_snapshot_refreshed",
  "operation": "assign_owner",
  "semantic_event_types": [],
  "changed_fields": {
    "owner_name": {"old": "Old Name", "new": "New Name"},
    "owner_email": {"old": "old@example.com", "new": "new@example.com"}
  }
}
```

### Account Update

Account update is audit-only in this phase.

```json
{
  "event_type": "crm.deal.account_updated",
  "operation": "update_account",
  "semantic_event_types": [],
  "changed_fields": {
    "company_id": {"old": "old-company-id", "new": "new-company-id"},
    "company_name": {"old": "Old Account", "new": "New Account"},
    "company_domain": {"old": "old.example", "new": "new.example"},
    "account.industry": {"old": "Finance", "new": "Telecom"}
  }
}
```

Rules:

- Direct deal snapshot fields use canonical deal field names: `company_id`, `company_name`, `company_domain`.
- Related account fields use namespaced audit fields: `account.name`, `account.domain`, `account.website`, `account.linkedin_url`, `account.industry`, `account.country`.
- Do not map account fields to workflow semantic event types in this phase.
- If no deal snapshot or account audit field changes, create no event and do not touch deal timestamps.
- This preserves one user/API action as one event while avoiding a fake `crm.deal.updated` event.

### Primary Contact Update

Primary contact update is audit-only in this phase.

```json
{
  "event_type": "crm.deal.primary_contact_updated",
  "operation": "update_primary_contact",
  "semantic_event_types": [],
  "changed_fields": {
    "primary_contact_id": {"old": "old-contact-id", "new": "new-contact-id"},
    "primary_contact_name": {"old": "Old Contact", "new": "New Contact"},
    "primary_contact_email": {"old": "old@example.com", "new": "new@example.com"}
  }
}
```

Rules:

- Link and display snapshot changes belong in the deal mutation event.
- Related contact entity details beyond primary display fields are out of scope for this phase.
- If the resolved/fallback primary contact is identical to the current snapshot and the active primary-contact participant link already exists, create no event and do not touch deal timestamps.
- If the snapshot is identical but the participant link is missing, repair the participant relationship without creating a deal mutation event because no deal-visible field changed.

### Delete Deal

Delete is a lifecycle event and can be triggerable.

```json
{
  "event_type": "crm.deal.deleted",
  "operation": "delete",
  "semantic_event_types": ["crm.deal.deleted"],
  "changed_fields": {
    "is_deleted": {"old": false, "new": true},
    "deal_status": {"old": "open", "new": "closed"}
  }
}
```

Rules:

- Soft-delete creates one mutation event.
- Already-deleted deal is a no-op: no timestamp update and no event. This requires a delete-specific load path because the current `_load_internal_deal()` rejects deleted deals.
- Include `is_deleted` in auditable deal fields.
- Keep `deal_status` in `changed_fields` for audit, but do not include `crm.deal.status_changed` in `semantic_event_types`; deleting a deal must not accidentally run close/status workflows.
- Keep `properties.deleted_via = "api"` as operational metadata, but do not make workflow matching depend on this property.

### Source Context

All methods changed in this plan should accept source metadata:

```python
source: str = "api"
source_label: str | None = None
```

Rules:

- Existing API handlers can keep calling the methods without passing source.
- Agent/tool callers should pass `source="agent"` or the existing tool source once they are wired.
- Workflow-originated writes should pass `source="workflow"` and later include explicit causality fields/runtime links such as `caused_by_workflow_run_id`, `caused_by_source_domain`, and `caused_by_source_event_id` when the runtime exists.
- The mutation helper should store `source_label=source_label or source`.

---

## Loopholes To Close Before Coding

1. **Same owner duplicate writes**
   - Risk: duplicate API retries create duplicate workflow events.
   - Fix: owner path computes `changed_fields` before applying timestamps.

2. **Owner display drift**
   - Risk: same `owner_id` with updated CRM user name/email either gets skipped or wrongly triggers owner workflows.
   - Fix: if `owner_id` is unchanged but display fields changed, emit audit-only `crm.deal.owner_snapshot_refreshed` with `semantic_event_types=[]`.

3. **Account helper side effects**
   - Risk: company helper mutates a related company object before audit can see old values.
   - Fix: capture account audit snapshot before `_upsert_company_from_account()`, then build direct deal changes and namespaced account changes from before/after snapshots.

4. **Fake account workflow triggers**
   - Risk: account update events accidentally become workflow-dispatched before product wants account triggers.
   - Fix: `semantic_event_types=[]` for account/contact.

5. **Contact fallback path**
   - Risk: no linked contact row exists, but fallback display values still change the deal.
   - Fix: compute the target primary-contact snapshot before mutating helpers where possible, skip the participant replacement helper on true no-ops, and record `primary_contact_id`, `primary_contact_name`, and `primary_contact_email` diff after fallback application.

6. **Delete idempotency**
   - Risk: repeated delete keeps changing timestamps and emits repeated delete events.
   - Fix: add a delete-safe load path that can load an already-deleted internal deal, then early no-op when `is_deleted=True` and lifecycle fields already match target values.

7. **Helper naming drift**
   - Risk: keeping `_record_deal_lifecycle_event()` and adding owner/account-specific helpers grows duplicate event-building code.
   - Fix: rename to a neutral helper and use it for close/reopen/owner/account/contact/delete.

8. **Large service file**
   - Risk: `deal_write_service.py` keeps growing.
   - Fix for this phase: no broad split during behavior work. Add small pure helpers only. After Phase 5C, extract event-building helpers into `src/service/crm/internal/deal_mutation_event_policy.py` as a separate refactor.
   - Status on 2026-06-17: cleanup refactor is implemented. Pure mutation policy
     code now lives in `src/service/crm/internal/deal_mutation_event_policy.py`;
     `DealWriteService` remains the single caller-facing write facade.

9. **Delete status-trigger leakage**
   - Risk: delete writes `deal_status=closed`, causing status-change workflows to fire.
   - Fix: delete event keeps `deal_status` in `changed_fields` but sets `semantic_event_types=["crm.deal.deleted"]` only.

10. **Source attribution drift**
    - Risk: agent/workflow writes get recorded as API writes.
    - Fix: add optional `source` and `source_label` parameters to write methods and pass them into `_record_deal_action_event()`.

11. **Task dependency drift**
    - Risk: contact/account tasks rely on shared snapshot/diff helpers that are easy to introduce too late.
    - Fix: define shared snapshot/diff helpers in Task 1 before owner/delete/account/contact behavior tasks.

---

## Task 1: Add A Neutral Event Recorder And Shared Diff Helpers

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_stage_history.py`
- Test: `tests/service/crm/test_deal_write_service_mutation_events.py`

- [ ] **Step 1: Create a new focused mutation-event test file**

Create `tests/service/crm/test_deal_write_service_mutation_events.py` with:

```python
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealMutationEvent,
    CRMDealStatus,
)
from src.service.crm.internal.deal_write_service import DealWriteService


class _FakeOrgSession:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = rows or []
        self.added: list[object] = []

    async def execute(self, _stmt):
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(
                first=lambda: self._rows[0] if self._rows else None,
                all=lambda: self._rows,
            ),
        )

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None
```

- [ ] **Step 2: Add shared snapshot, diff, and delete-safe load helpers**

Add these helpers near the existing mutation helpers in `src/service/crm/internal/deal_write_service.py`:

```python
@staticmethod
def _snapshot_fields(source: object, field_names: tuple[str, ...]) -> dict[str, Any]:
    return {field_name: getattr(source, field_name, None) for field_name in field_names}

def _build_named_changed_fields(
    self,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    datetime_fields: set[str] | None = None,
    uuid_fields: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    changed_fields: dict[str, dict[str, Any]] = {}
    datetime_fields = datetime_fields or set()
    uuid_fields = uuid_fields or set()
    for field_name, new_value in after.items():
        old_value = before.get(field_name)
        if field_name in uuid_fields:
            old_json = str(old_value) if old_value is not None else None
            new_json = str(new_value) if new_value is not None else None
        elif field_name in datetime_fields:
            old_json = self._parse_datetime(old_value) if old_value is not None else None
            new_json = self._parse_datetime(new_value) if new_value is not None else None
        else:
            old_json = self._json_safe_value(old_value)
            new_json = self._json_safe_value(new_value)
        if old_json != new_json:
            changed_fields[field_name] = {"old": old_json, "new": new_json}
    return changed_fields
```

Change `_load_internal_deal()` so delete can load an already-deleted internal deal while all other callers keep the current 404 behavior:

```python
async def _load_internal_deal(
    self,
    deal_id: UUID,
    *,
    include_deleted: bool = False,
) -> CRMDeal:
    deal = await self.session.get(CRMDeal, deal_id)
    if not deal or deal.organisation_id != self.organisation_id:
        raise DealWriteServiceError("Deal not found", status_code=404)
    if deal.is_deleted and not include_deleted:
        raise DealWriteServiceError("Deal not found", status_code=404)

    setup_service = InternalCRMSetupService(
        self.session, self.organisation_id, self.actor_user_id
    )
    integration = await setup_service.get_internal_integration()
    if not integration or deal.integration_id != integration.integration_id:
        raise DealWriteServiceError("External CRM deals are read-only", status_code=409)

    return deal
```

Add a primary-contact participant helper so contact no-op tests can assert the real production path, not only a mocked helper path:

```python
async def _has_active_primary_contact_participant(
    self,
    *,
    deal: CRMDeal,
    contact_id: UUID | None,
) -> bool:
    if contact_id is None:
        return False

    stmt = (
        select(col(CRMDealParticipant.participant_id))
        .where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == deal.integration_id,
            col(CRMDealParticipant.deal_id) == deal.deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_primary_contact).is_(True),
            col(CRMDealParticipant.related_local_id) == contact_id,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        .limit(1)
    )
    result = await self.session.execute(stmt)
    return result.scalar_one_or_none() is not None
```

Add a small relationship-repair helper for the "same snapshot, missing participant link" path:

```python
async def _ensure_primary_contact_participant_link(
    self,
    *,
    deal: CRMDeal,
    contact: CRMContact,
) -> None:
    from src.service.crm.internal.deal_participant_service import DealParticipantService

    participant_service = DealParticipantService(
        self.session, self.organisation_id, self.actor_user_id, self.master_session
    )
    await participant_service.add_participants(
        deal.deal_id,
        [
            {
                "participant_type": "contact",
                "related_id": contact.contact_id,
                "relationship_role": "primary_contact",
            }
        ],
    )
```

- [ ] **Step 3: Refactor lifecycle event helper name**

In `src/service/crm/internal/deal_write_service.py`, rename `_record_deal_lifecycle_event()` to `_record_deal_action_event()` and update `close_deal()` / `reopen_deal()` call sites.

The new signature should be:

```python
async def _record_deal_action_event(
    self,
    *,
    deal: CRMDeal,
    changed_fields: dict[str, dict[str, Any]],
    occurred_at: datetime,
    source: str,
    source_label: str | None = None,
    event_type: str,
    operation: str,
    summary: str,
    base_semantic_event_types: list[str] | None = None,
    include_field_semantics: bool = True,
) -> CRMDealMutationEvent:
```

Build semantic types with this policy:

```python
semantic_event_types: list[str] = []
for semantic_event_type in base_semantic_event_types or []:
    if semantic_event_type not in semantic_event_types:
        semantic_event_types.append(semantic_event_type)
if include_field_semantics:
    for semantic_event_type in self._derive_deal_semantic_event_types(changed_fields):
        if semantic_event_type not in semantic_event_types:
            semantic_event_types.append(semantic_event_type)
```

Set `source_label=source_label or source` on the event.

- [ ] **Step 4: Keep lifecycle behavior stable**

Update close/reopen calls:

```python
await self._record_deal_action_event(
    deal=deal,
    changed_fields=changed_fields,
    occurred_at=now,
    source="api",
    source_label=None,
    event_type="crm.deal.closed",
    operation="close",
    summary=f"Deal closed as {target_status.value}",
    base_semantic_event_types=["crm.deal.closed"],
)
```

```python
await self._record_deal_action_event(
    deal=deal,
    changed_fields=changed_fields,
    occurred_at=now,
    source="api",
    source_label=None,
    event_type="crm.deal.reopened",
    operation="reopen",
    summary="Deal reopened",
    base_semantic_event_types=["crm.deal.reopened"],
)
```

- [ ] **Step 5: Verify lifecycle tests still pass**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
```

Expected:

```text
15 passed
```

The exact warning count may vary because current test runs emit third-party `pyiceberg` deprecation warnings.

---

## Task 2: Implement Phase 5B Owner Mutation Events

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_mutation_events.py`

- [ ] **Step 1: Write failing owner assignment test**

Add:

```python
@pytest.mark.asyncio
async def test_assign_owner_records_owner_changed_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_owner_id = uuid4()
    new_owner_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=old_owner_id,
        owner_name="Old Owner",
        owner_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    crm_user = SimpleNamespace(
        user_id=new_owner_id,
        full_name="New Owner",
        email="new@example.com",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)

    with patch(
        "src.service.crm.internal.deal_write_service.InternalCRMUserService.ensure_crm_user",
        new=AsyncMock(return_value=crm_user),
    ):
        updated = await service.assign_owner(deal.deal_id, new_owner_id)

    event = next(obj for obj in session.added if isinstance(obj, CRMDealMutationEvent))
    assert updated is deal
    assert event.event_type == "crm.deal.owner_changed"
    assert event.operation == "assign_owner"
    assert event.semantic_event_types == ["crm.deal.owner_changed"]
    assert event.changed_fields == {
        "owner_id": {"old": str(old_owner_id), "new": str(new_owner_id)},
        "owner_name": {"old": "Old Owner", "new": "New Owner"},
        "owner_email": {"old": "old@example.com", "new": "new@example.com"},
    }
```

- [ ] **Step 2: Write failing clear-owner test**

Add:

```python
@pytest.mark.asyncio
async def test_assign_owner_clear_records_owner_changed_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_owner_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=old_owner_id,
        owner_name="Old Owner",
        owner_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)

    updated = await service.assign_owner(deal.deal_id, None)

    event = next(obj for obj in session.added if isinstance(obj, CRMDealMutationEvent))
    assert updated is deal
    assert event.event_type == "crm.deal.owner_changed"
    assert event.changed_fields == {
        "owner_id": {"old": str(old_owner_id), "new": None},
        "owner_name": {"old": "Old Owner", "new": None},
        "owner_email": {"old": "old@example.com", "new": None},
    }
```

- [ ] **Step 3: Write failing owner no-op test**

Add:

```python
@pytest.mark.asyncio
async def test_assign_owner_same_snapshot_is_noop_without_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    owner_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=owner_id,
        owner_name="Owner",
        owner_email="owner@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    crm_user = SimpleNamespace(user_id=owner_id, full_name="Owner", email="owner@example.com")
    service._load_internal_deal = AsyncMock(return_value=deal)

    with patch(
        "src.service.crm.internal.deal_write_service.InternalCRMUserService.ensure_crm_user",
        new=AsyncMock(return_value=crm_user),
    ):
        updated = await service.assign_owner(deal.deal_id, owner_id)

    assert updated is deal
    assert not any(isinstance(obj, CRMDealMutationEvent) for obj in session.added)
    assert deal.source_modified_at is None
```

- [ ] **Step 4: Write failing owner display-refresh test**

Add:

```python
@pytest.mark.asyncio
async def test_assign_owner_same_id_display_refresh_is_audit_only() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    owner_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=owner_id,
        owner_name="Old Name",
        owner_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    crm_user = SimpleNamespace(user_id=owner_id, full_name="New Name", email="new@example.com")
    service._load_internal_deal = AsyncMock(return_value=deal)

    with patch(
        "src.service.crm.internal.deal_write_service.InternalCRMUserService.ensure_crm_user",
        new=AsyncMock(return_value=crm_user),
    ):
        updated = await service.assign_owner(deal.deal_id, owner_id)

    event = next(obj for obj in session.added if isinstance(obj, CRMDealMutationEvent))
    assert updated is deal
    assert event.event_type == "crm.deal.owner_snapshot_refreshed"
    assert event.operation == "assign_owner"
    assert event.semantic_event_types == []
    assert event.changed_fields == {
        "owner_name": {"old": "Old Name", "new": "New Name"},
        "owner_email": {"old": "old@example.com", "new": "new@example.com"},
    }
```

- [ ] **Step 5: Run owner tests and verify RED**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected before implementation: owner event tests fail because `assign_owner()` does not create `CRMDealMutationEvent`.

- [ ] **Step 6: Implement owner diff-first write**

Change the method signature to:

```python
async def assign_owner(
    self,
    deal_id: UUID,
    owner_user_id: UUID | None,
    *,
    source: str = "api",
    source_label: str | None = None,
) -> CRMDeal:
```

Replace direct assignment in `assign_owner()` with:

```python
target_values = {
    "owner_id": crm_user.user_id if owner_user_id else None,
    "owner_name": crm_user.full_name if owner_user_id else None,
    "owner_email": crm_user.email if owner_user_id else None,
}
changed_fields = self._build_deal_changed_fields(deal, target_values)
if not changed_fields:
    return deal

owner_identity_changed = "owner_id" in changed_fields

for field_name, value in target_values.items():
    setattr(deal, field_name, value)

now = datetime.now(UTC)
deal.source_modified_at = now
deal.synced_at = now
deal.last_seen_at = now

await self._record_deal_action_event(
    deal=deal,
    changed_fields=changed_fields,
    occurred_at=now,
    source=source,
    source_label=source_label,
    event_type=(
        "crm.deal.owner_changed"
        if owner_identity_changed
        else "crm.deal.owner_snapshot_refreshed"
    ),
    operation="assign_owner",
    summary=(
        "Deal owner changed"
        if owner_identity_changed and owner_user_id
        else "Deal owner cleared"
        if owner_identity_changed
        else "Deal owner display refreshed"
    ),
    base_semantic_event_types=(
        ["crm.deal.owner_changed"] if owner_identity_changed else []
    ),
    include_field_semantics=owner_identity_changed,
)
```

- [ ] **Step 7: Run owner tests and verify GREEN**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected: owner tests pass.

---

## Task 3: Implement Delete Deal Mutation Event

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_mutation_events.py`

- [ ] **Step 1: Add `is_deleted` to auditable fields**

In `_AUDITABLE_DEAL_FIELD_NAMES`, add:

```python
"is_deleted",
```

- [ ] **Step 2: Write failing delete event test**

Add:

```python
@pytest.mark.asyncio
async def test_delete_deal_records_deleted_mutation_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        is_deleted=False,
        deal_status=CRMDealStatus.OPEN,
        properties={"is_internal_crm": True},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)

    updated = await service.delete_deal(deal.deal_id)

    event = next(obj for obj in session.added if isinstance(obj, CRMDealMutationEvent))
    assert updated is deal
    service._load_internal_deal.assert_awaited_once_with(deal.deal_id, include_deleted=True)
    assert event.event_type == "crm.deal.deleted"
    assert event.operation == "delete"
    assert event.semantic_event_types == ["crm.deal.deleted"]
    assert event.changed_fields == {
        "is_deleted": {"old": False, "new": True},
        "deal_status": {"old": "open", "new": "closed"},
    }
```

- [ ] **Step 3: Write failing delete no-op test**

Add:

```python
@pytest.mark.asyncio
async def test_delete_deal_already_deleted_is_noop_without_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        is_deleted=True,
        deal_status=CRMDealStatus.CLOSED,
        properties={"is_internal_crm": True, "deleted_via": "api"},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)

    updated = await service.delete_deal(deal.deal_id)

    assert updated is deal
    service._load_internal_deal.assert_awaited_once_with(deal.deal_id, include_deleted=True)
    assert not any(isinstance(obj, CRMDealMutationEvent) for obj in session.added)
    assert deal.source_modified_at is None
```

- [ ] **Step 4: Run delete tests and verify RED**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected before implementation: delete event test fails because `delete_deal()` does not create a mutation event and no-op test fails because timestamps still change.

- [ ] **Step 5: Implement delete diff-first write**

Change the method signature to:

```python
async def delete_deal(
    self,
    deal_id: UUID,
    *,
    source: str = "api",
    source_label: str | None = None,
) -> CRMDeal:
```

Load with deleted rows included, then compute:

```python
deal = await self._load_internal_deal(deal_id, include_deleted=True)

target_values = {
    "is_deleted": True,
    "deal_status": CRMDealStatus.CLOSED,
}
changed_fields = self._build_deal_changed_fields(deal, target_values)
if not changed_fields:
    return deal
```

Then apply fields, timestamps, `properties.deleted_via`, and:

```python
await self._record_deal_action_event(
    deal=deal,
    changed_fields=changed_fields,
    occurred_at=now,
    source=source,
    source_label=source_label,
    event_type="crm.deal.deleted",
    operation="delete",
    summary="Deal deleted",
    base_semantic_event_types=["crm.deal.deleted"],
    include_field_semantics=False,
)
```

- [ ] **Step 6: Run delete tests and verify GREEN**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected: owner and delete tests pass.

---

## Task 4: Implement Account Update Audit Event

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_mutation_events.py`
- Test: `tests/service/crm/test_deal_write_service_account_updates.py`

- [ ] **Step 1: Add account audit snapshot helpers**

Add this constant near the other module-level constants:

```python
_ACCOUNT_AUDIT_FIELD_NAMES = (
    "name",
    "domain",
    "website",
    "linkedin_url",
    "industry",
    "country",
)
```

Add helpers near other account helpers:

```python
async def _get_account_audit_snapshot(self, deal: CRMDeal) -> dict[str, Any]:
    company = await self._get_current_company(deal.company_id, deal.integration_id)
    if company:
        return self._company_to_account_audit_snapshot(company)
    return self._company_to_account_audit_snapshot(
        None,
        fallback_name=deal.company_name,
        fallback_domain=deal.company_domain,
    )

@staticmethod
def _company_to_account_audit_snapshot(
    company: CRMCompany | None,
    *,
    fallback_name: str | None = None,
    fallback_domain: str | None = None,
) -> dict[str, Any]:
    if company is None:
        return {
            "name": fallback_name,
            "domain": fallback_domain,
            "website": None,
            "linkedin_url": None,
            "industry": None,
            "country": None,
        }
    return {
        field_name: getattr(company, field_name, None)
        for field_name in _ACCOUNT_AUDIT_FIELD_NAMES
    }
```

Use `_build_named_changed_fields()` from Task 1 for namespaced account audit fields.

- [ ] **Step 2: Write failing account event test**

Add:

```python
@pytest.mark.asyncio
async def test_update_account_records_audit_only_account_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_company_id = uuid4()
    new_company_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        company_id=old_company_id,
        company_name="Old Account",
        company_domain="old.example",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    old_company = SimpleNamespace(
        company_id=old_company_id,
        name="Old Account",
        domain="old.example",
        website="https://old.example",
        linkedin_url=None,
        industry="Finance",
        country="US",
    )
    new_company = SimpleNamespace(
        company_id=new_company_id,
        name="New Account",
        domain="new.example",
        website="https://new.example",
        linkedin_url=None,
        industry="Telecom",
        country="US",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._get_current_company = AsyncMock(return_value=old_company)
    service._upsert_company_from_account = AsyncMock(return_value=(new_company, None))

    async def _replace_company_side_effect(target_deal, company, now):
        target_deal.company_id = company.company_id
        target_deal.company_name = company.name
        target_deal.company_domain = company.domain
        target_deal.source_modified_at = now
        target_deal.synced_at = now
        target_deal.last_seen_at = now

    service._replace_company_participant = AsyncMock(side_effect=_replace_company_side_effect)

    updated = await service.update_account(
        deal.deal_id,
        {
            "name": "New Account",
            "domain": "new.example",
            "website": "https://new.example",
            "industry": "Telecom",
        },
    )

    event = next(obj for obj in session.added if isinstance(obj, CRMDealMutationEvent))
    assert updated is deal
    assert event.event_type == "crm.deal.account_updated"
    assert event.operation == "update_account"
    assert event.semantic_event_types == []
    assert event.changed_fields["company_id"] == {
        "old": str(old_company_id),
        "new": str(new_company_id),
    }
    assert event.changed_fields["company_name"] == {
        "old": "Old Account",
        "new": "New Account",
    }
    assert event.changed_fields["company_domain"] == {
        "old": "old.example",
        "new": "new.example",
    }
    assert event.changed_fields["account.industry"] == {
        "old": "Finance",
        "new": "Telecom",
    }
```

- [ ] **Step 3: Write failing account no-op test**

Add:

```python
@pytest.mark.asyncio
async def test_update_account_same_snapshot_has_no_mutation_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    company_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        company_id=company_id,
        company_name="Acme",
        company_domain="acme.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    company = SimpleNamespace(
        company_id=company_id,
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        linkedin_url=None,
        industry="Telecom",
        country="US",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._get_current_company = AsyncMock(return_value=company)
    service._upsert_company_from_account = AsyncMock(return_value=(company, None))
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._replace_company_participant = AsyncMock()

    updated = await service.update_account(
        deal.deal_id,
        {
            "name": "Acme",
            "domain": "acme.com",
            "website": "https://acme.com",
            "industry": "Telecom",
            "country": "US",
        },
    )

    assert updated is deal
    service._replace_company_participant.assert_not_called()
    assert not any(isinstance(obj, CRMDealMutationEvent) for obj in session.added)
    assert deal.source_modified_at is None
```

- [ ] **Step 4: Run account tests and verify RED**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected before implementation: new account event assertions fail because `update_account()` does not emit account mutation events.

- [ ] **Step 5: Implement account snapshots and event**

Inside `update_account()`:

1. Change the method signature to accept source context:

```python
async def update_account(
    self,
    deal_id: UUID,
    account_payload: dict[str, Any],
    *,
    source: str = "api",
    source_label: str | None = None,
) -> CRMDeal:
```

2. Capture direct deal account fields before mutation:

```python
deal_account_before = self._snapshot_fields(
    deal,
    ("company_id", "company_name", "company_domain"),
)
```

3. Capture current account entity fields before mutation:

```python
account_before = await self._get_account_audit_snapshot(deal)
```

4. Let existing company/participant logic run.

5. Build direct deal changes using `_build_named_changed_fields()` with UUID normalization for `company_id`:

```python
deal_account_after = self._snapshot_fields(
    deal,
    ("company_id", "company_name", "company_domain"),
)
changed_fields = self._build_named_changed_fields(
    deal_account_before,
    deal_account_after,
    uuid_fields={"company_id"},
)
```

6. Build account namespaced changes:

```python
account_after = self._company_to_account_audit_snapshot(
    company,
    fallback_name=fallback_name,
    fallback_domain=deal.company_domain,
)
account_changed_fields = self._build_named_changed_fields(
    account_before,
    account_after,
)
changed_fields.update(
    {f"account.{key}": value for key, value in account_changed_fields.items()}
)
```

7. If `changed_fields` is non-empty:

```python
await self._record_deal_action_event(
    deal=deal,
    changed_fields=changed_fields,
    occurred_at=now,
    source=source,
    source_label=source_label,
    event_type="crm.deal.account_updated",
    operation="update_account",
    summary="Deal account updated",
    include_field_semantics=False,
)
```

- [ ] **Step 6: Run account tests and verify GREEN**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected: new mutation tests and existing account tests pass.

---

## Task 5: Implement Primary Contact Audit Event

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_mutation_events.py`
- Test: `tests/service/crm/test_deal_write_service_account_updates.py`

- [ ] **Step 1: Write failing linked-contact event test**

Add:

```python
@pytest.mark.asyncio
async def test_update_primary_contact_records_audit_only_contact_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_contact_id = uuid4()
    new_contact_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        company_name="Acme",
        primary_contact_id=old_contact_id,
        primary_contact_name="Old Contact",
        primary_contact_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    contact = SimpleNamespace(
        contact_id=new_contact_id,
        full_name="New Contact",
        email="new@example.com",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._upsert_contact_from_primary = AsyncMock(return_value=(contact, None, None))

    async def _replace_contact_side_effect(target_deal, target_contact, now):
        target_deal.primary_contact_id = target_contact.contact_id
        target_deal.primary_contact_name = target_contact.full_name
        target_deal.primary_contact_email = target_contact.email
        target_deal.source_modified_at = now
        target_deal.synced_at = now
        target_deal.last_seen_at = now

    service._replace_primary_contact_participant = AsyncMock(
        side_effect=_replace_contact_side_effect
    )

    updated = await service.update_primary_contact(
        deal.deal_id,
        primary_contact_name="New Contact",
        primary_contact_email="new@example.com",
    )

    event = next(obj for obj in session.added if isinstance(obj, CRMDealMutationEvent))
    assert updated is deal
    assert event.event_type == "crm.deal.primary_contact_updated"
    assert event.operation == "update_primary_contact"
    assert event.semantic_event_types == []
    assert event.changed_fields == {
        "primary_contact_id": {"old": str(old_contact_id), "new": str(new_contact_id)},
        "primary_contact_name": {"old": "Old Contact", "new": "New Contact"},
        "primary_contact_email": {"old": "old@example.com", "new": "new@example.com"},
    }
```

- [ ] **Step 2: Write failing fallback-contact event test**

Add event assertions to the existing fallback test in `tests/service/crm/test_deal_write_service_account_updates.py` or add a new test in the mutation-event file with the same setup.

Expected:

```python
assert event.event_type == "crm.deal.primary_contact_updated"
assert event.changed_fields == {
    "primary_contact_id": {"old": str(old_contact_id), "new": None},
    "primary_contact_name": {"old": "Old Contact", "new": "New Contact"},
    "primary_contact_email": {"old": "old@example.com", "new": "new@example.com"},
}
```

- [ ] **Step 3: Write failing contact no-op test**

Add:

```python
@pytest.mark.asyncio
async def test_update_primary_contact_same_snapshot_has_no_event() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    contact_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        company_name="Acme",
        primary_contact_id=contact_id,
        primary_contact_name="Contact",
        primary_contact_email="contact@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    contact = SimpleNamespace(
        contact_id=contact_id,
        full_name="Contact",
        email="contact@example.com",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._upsert_contact_from_primary = AsyncMock(return_value=(contact, None, None))
    service._has_active_primary_contact_participant = AsyncMock(return_value=True)
    service._replace_primary_contact_participant = AsyncMock()

    updated = await service.update_primary_contact(
        deal.deal_id,
        primary_contact_name="Contact",
        primary_contact_email="contact@example.com",
    )

    assert updated is deal
    service._has_active_primary_contact_participant.assert_awaited_once_with(
        deal=deal,
        contact_id=contact_id,
    )
    service._replace_primary_contact_participant.assert_not_called()
    assert not any(isinstance(obj, CRMDealMutationEvent) for obj in session.added)
    assert deal.source_modified_at is None
```

- [ ] **Step 4: Implement contact snapshot/event**

In `update_primary_contact()`:

1. Change the method signature to accept source context:

```python
async def update_primary_contact(
    self,
    deal_id: UUID,
    *,
    primary_contact_name: str | None,
    primary_contact_email: str | None,
    company_name: str | None = None,
    source: str = "api",
    source_label: str | None = None,
) -> CRMDeal:
```

2. Capture before snapshot:

```python
before = self._snapshot_fields(
    deal,
    ("primary_contact_id", "primary_contact_name", "primary_contact_email"),
)
```

3. Resolve the target contact/fallback values.

4. If the resolved target snapshot is identical to the current deal snapshot and the active primary-contact participant link already exists, return before calling `_replace_primary_contact_participant()`:

```python
target_snapshot = (
    {
        "primary_contact_id": contact.contact_id,
        "primary_contact_name": contact.full_name or deal.primary_contact_name,
        "primary_contact_email": contact.email or deal.primary_contact_email,
    }
    if contact
    else {
        "primary_contact_id": None,
        "primary_contact_name": fallback_name or deal.primary_contact_name,
        "primary_contact_email": fallback_email or deal.primary_contact_email,
    }
)
target_changed_fields = self._build_named_changed_fields(
    before,
    target_snapshot,
    uuid_fields={"primary_contact_id"},
)
if (
    not target_changed_fields
    and contact
    and await self._has_active_primary_contact_participant(
        deal=deal,
        contact_id=contact.contact_id,
    )
):
    return deal
```

5. If the snapshot is identical but the participant link is missing, repair the participant relationship without creating a deal mutation event:

```python
if not target_changed_fields and contact:
    await self._ensure_primary_contact_participant_link(deal=deal, contact=contact)
    return deal
```

6. Run existing participant/fallback mutation logic for real snapshot changes.

7. Capture after snapshot and diff:

```python
after = self._snapshot_fields(
    deal,
    ("primary_contact_id", "primary_contact_name", "primary_contact_email"),
)
changed_fields = self._build_named_changed_fields(
    before,
    after,
    uuid_fields={"primary_contact_id"},
)
```

8. If `changed_fields` is empty after participant repair, return without recording a mutation event.

9. Only when `changed_fields` exists:

```python
await self._record_deal_action_event(
    deal=deal,
    changed_fields=changed_fields,
    occurred_at=now,
    source=source,
    source_label=source_label,
    event_type="crm.deal.primary_contact_updated",
    operation="update_primary_contact",
    summary="Deal primary contact updated",
    include_field_semantics=False,
)
```

- [ ] **Step 5: Verify contact tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected: account/contact tests pass.

---

## Task 6: Update The Contract Document

**Files:**

- Modify: `docs/superpowers/plans/2026-06-16-crm-write-mutation-contract.md`

- [ ] **Step 1: Update Phase 5 status**

Add a status paragraph under Phase 5:

```markdown
Status on 2026-06-17: Phase 5B/5C now covers owner, account, primary-contact,
and delete deal writes. Owner assignment emits triggerable
`crm.deal.owner_changed` events only when `owner_id` changes. Same-owner
display refresh emits audit-only `crm.deal.owner_snapshot_refreshed` events.
Account and primary-contact updates emit audit-only events with
`semantic_event_types=[]` unless future product workflow requirements make
those fields triggerable. Delete emits `crm.deal.deleted`, keeps
`deal_status` only in `changed_fields`, and is no-op/idempotent when the deal
is already deleted.
```

- [ ] **Step 2: Add event examples**

Add examples for:

- `crm.deal.owner_changed`
- `crm.deal.owner_snapshot_refreshed`
- `crm.deal.account_updated`
- `crm.deal.primary_contact_updated`
- `crm.deal.deleted`

- [ ] **Step 3: Note deliberate non-goals**

Add:

```markdown
Account/contact entity-wide audit is not fully generalized in this phase. This
phase records deal-write surface changes. A broader `crm.account.updated` or
`crm.contact.updated` source-of-truth event can be added when account/contact
write services become first-class workflow trigger sources.
```

---

## Task 7: Final Verification

**Files:**

- Verify: `src/service/crm/internal/deal_write_service.py`
- Verify: `tests/service/crm/test_deal_write_service_mutation_events.py`
- Verify: `tests/service/crm/test_deal_write_service_account_updates.py`
- Verify: `tests/service/crm/test_deal_write_service_stage_history.py`
- Verify: `tests/agents/tools/crm/test_deal_update_tools.py`

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_deal_write_service_stage_history.py tests/agents/tools/crm/test_deal_update_tools.py -q
```

Expected:

```text
all selected tests pass
```

- [ ] **Step 2: Run Ruff**

Run:

```bash
uv run ruff check src/service/crm/internal/deal_write_service.py tests/service/crm/test_deal_write_service_mutation_events.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_deal_write_service_stage_history.py
```

Expected:

```text
All checks passed!
```

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check
```

Expected:

```text
exit code 0
```

Existing LF-to-CRLF warnings may print in this repository; they are not whitespace errors.

---

## Execution Order Recommendation

1. Task 1: neutral event recorder.
2. Task 2: owner event, because it is triggerable and small.
3. Task 3: delete event, because it is lifecycle/idempotency-sensitive.
4. Task 5: primary contact event, because it is simpler than account.
5. Task 4: account event, because related account snapshot audit has more edge cases.
6. Task 6: document update.
7. Task 7: final verification.

This order gives working checkpoints and avoids mixing account helper complexity into the owner/delete work.

---

## Production Acceptance Criteria

- Owner assignment, clear owner, and same-owner retries behave deterministically.
- Same-owner display refresh is audit-only and cannot trigger owner-change workflows.
- Delete is idempotent and emits a lifecycle event only once per actual state transition.
- Delete stores `deal_status` in audit evidence but exposes only `crm.deal.deleted` as workflow semantics.
- Account/contact events are audit-only unless explicitly made triggerable later.
- Primary-contact same-snapshot updates skip `_replace_primary_contact_participant()` when the active participant link already exists.
- Missing primary-contact participant links can be repaired without creating a fake deal mutation event.
- No method emits multiple mutation events for one deal-write action in this phase.
- No same-value write updates timestamps just to satisfy an event path.
- All event rows include `organisation_id`, `entity_type`, `entity_id`, `deal_id`, `operation`, `changed_fields`, `source`, `source_label`, `actor_user_id`, `integration_id`, `payload_hash`, and the expected semantic event types for that event type.
- New write-method signatures preserve API defaults while allowing agent/workflow callers to pass source attribution.
- Existing API handlers and agent tools continue to call the same service methods.
- No direct workflow dispatch is added in this phase.
