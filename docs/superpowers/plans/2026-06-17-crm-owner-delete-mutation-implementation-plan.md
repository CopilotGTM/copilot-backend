# CRM Owner Delete Mutation Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the remaining CRM mutation phase by making owner assignment and soft-delete writes emit production-grade `crm_mutation_events` rows with correct workflow semantics and idempotent no-op behavior.

**Architecture:** Keep `DealWriteService` as the canonical write boundary. Reuse the existing `_record_deal_action_event()`, `_build_deal_changed_fields()`, and `_build_named_changed_fields()` helpers already added for lifecycle/account/contact events. Owner identity changes are workflow-triggerable; owner display refresh is audit-only. Delete is workflow-triggerable only as `crm.deal.deleted`; it must not leak `deal_status` into status-change workflow triggers.

**Tech Stack:** Python 3.11, FastAPI service layer, SQLModel/SQLAlchemy async sessions, PostgreSQL-backed `crm_mutation_events`, pytest, Ruff.

Status on 2026-06-17: implemented. `assign_owner()` now emits triggerable
owner-change events or audit-only owner display refresh events, and
`delete_deal()` now emits idempotent `crm.deal.deleted` events using the
delete-safe deal loader path.

---

## Current State

Implemented before this plan:

- `crm_mutation_events` model and migration.
- Creation, scalar/custom update, stage move, close/reopen lifecycle events.
- Account and primary-contact audit-only mutation events.
- Shared event helper: `DealWriteService._record_deal_action_event()`.
- Shared diff helpers: `_build_deal_changed_fields()` and `_build_named_changed_fields()`.

Implemented by this plan:

- `assign_owner()` records owner mutation events and skips same-snapshot retries.
- `delete_deal()` records delete mutation events and skips already-deleted retries.
- `_load_internal_deal()` keeps deleted-row rejection by default and supports
  `include_deleted=True` for delete idempotency only.

---

## Event Contract

### Owner Identity Change

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

- Different `owner_id` creates one triggerable event.
- Clearing owner creates one triggerable event with owner fields set to `null`.
- Same `owner_id` and same display fields is a no-op.
- Same `owner_id` but changed name/email is audit-only, not workflow-triggering.

### Owner Display Refresh

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

Rules:

- This event exists for CRM history/debugging only.
- It must not include `crm.deal.owner_changed` in `semantic_event_types`.
- If `owner_id` is already `null` and only stale owner display fields are
  cleared, treat it as display cleanup, not an owner identity change.

### Delete Deal

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

- Soft-delete creates one event.
- Already-deleted retry is a no-op: no timestamp update and no event.
- Keep `deal_status` in `changed_fields` for audit.
- Do not include `crm.deal.status_changed` in `semantic_event_types` for delete.

---

## Loopholes To Close

1. **Owner display drift**
   - Same `owner_id` with changed display fields should not trigger owner-change workflows.
   - Use `event_type=crm.deal.owner_snapshot_refreshed` with `semantic_event_types=[]`.

2. **Duplicate owner retries**
   - Same owner/snapshot should not touch timestamps or create events.
   - Compute `changed_fields` before assignment and return early on empty diff.

3. **Clear owner semantics**
   - Clearing owner is a real owner identity change.
   - It should emit `crm.deal.owner_changed`.

4. **Delete idempotency**
   - `_load_internal_deal()` currently rejects deleted deals.
   - Add `include_deleted=False` default and use `include_deleted=True` only in `delete_deal()`.

5. **Delete status-trigger leakage**
   - Delete changes `deal_status` to closed, but status workflows should not fire.
   - Call `_record_deal_action_event(..., include_field_semantics=False, base_semantic_event_types=["crm.deal.deleted"])`.

6. **Source attribution**
   - Preserve existing API calls with defaults.
   - Add optional `source` and `source_label` parameters to `assign_owner()` and `delete_deal()`.
   - Tests must assert default owner/delete events keep `source=api` and `source_label=api`.

7. **Owner resolver side effect boundary**
   - No-op means no deal mutation event and no deal timestamp update.
   - For non-null owner targets, `ensure_crm_user()` may still run before the
     no-op comparison because the canonical owner snapshot comes from CRM user
     resolution. If we later require zero DB work outside the deal row, add a
     separate read-only resolver path then.

8. **Already-deleted API behavior**
   - `delete_deal()` becomes idempotent at the service boundary and returns the
     existing deleted deal on retries.
   - All other write methods must keep rejecting deleted deals through the
     default `_load_internal_deal(include_deleted=False)` behavior.

---

## Task 1: Owner Mutation Events

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Modify: `tests/service/crm/test_deal_write_service_mutation_events.py`

- [ ] **Step 1: Update test imports**

In `tests/service/crm/test_deal_write_service_mutation_events.py`, change:

```python
from unittest.mock import AsyncMock
```

to:

```python
from unittest.mock import AsyncMock, patch
```

- [ ] **Step 2: Add owner changed test**

Append this test to `tests/service/crm/test_deal_write_service_mutation_events.py`:

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
    assert event.source == "api"
    assert event.source_label == "api"
    assert event.changed_fields == {
        "owner_id": {"old": str(old_owner_id), "new": str(new_owner_id)},
        "owner_name": {"old": "Old Owner", "new": "New Owner"},
        "owner_email": {"old": "old@example.com", "new": "new@example.com"},
    }
```

- [ ] **Step 3: Add clear-owner test**

Append:

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
    assert event.semantic_event_types == ["crm.deal.owner_changed"]
    assert event.source == "api"
    assert event.source_label == "api"
    assert event.changed_fields == {
        "owner_id": {"old": str(old_owner_id), "new": None},
        "owner_name": {"old": "Old Owner", "new": None},
        "owner_email": {"old": "old@example.com", "new": None},
    }
```

- [ ] **Step 4: Add same-owner no-op test**

Append:

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

- [ ] **Step 5: Add display-refresh audit-only test**

Append:

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
    assert event.source == "api"
    assert event.source_label == "api"
    assert event.changed_fields == {
        "owner_name": {"old": "Old Name", "new": "New Name"},
        "owner_email": {"old": "old@example.com", "new": "new@example.com"},
    }
```

- [ ] **Step 6: Run owner tests and verify RED**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected before implementation:

```text
owner event tests fail because assign_owner() does not emit CRMDealMutationEvent
```

- [ ] **Step 7: Implement owner diff-first write**

Change `assign_owner()` signature in `src/service/crm/internal/deal_write_service.py`:

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

Replace the mutation body after `deal = await self._load_internal_deal(deal_id)` with:

```python
if owner_user_id:
    user_service = InternalCRMUserService(
        org_session=self.session,
        master_session=self.master_session,
        organisation_id=self.organisation_id,
        actor_user_id=self.actor_user_id,
    )
    crm_user = await user_service.ensure_crm_user(owner_user_id)
    target_values = {
        "owner_id": crm_user.user_id,
        "owner_name": crm_user.full_name,
        "owner_email": crm_user.email,
    }
else:
    target_values = {
        "owner_id": None,
        "owner_name": None,
        "owner_email": None,
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
logger.info(
    "Assigned owner %s to deal %s",
    owner_user_id or "None",
    deal.deal_id,
)
return deal
```

- [ ] **Step 8: Run owner tests and verify GREEN**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected:

```text
all owner/account/contact mutation tests pass
```

---

## Task 2: Delete Deal Mutation Event

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Modify: `tests/service/crm/test_deal_write_service_mutation_events.py`

- [ ] **Step 1: Update test imports**

In `tests/service/crm/test_deal_write_service_mutation_events.py`, change:

```python
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
```

to:

```python
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealMutationEvent,
    CRMDealStatus,
)
```

- [ ] **Step 2: Add delete event test**

Append:

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
    assert "crm.deal.status_changed" not in event.semantic_event_types
    assert event.source == "api"
    assert event.source_label == "api"
    assert event.changed_fields == {
        "is_deleted": {"old": False, "new": True},
        "deal_status": {"old": "open", "new": "closed"},
    }
```

- [ ] **Step 3: Add delete no-op test**

Append:

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

Expected before implementation:

```text
delete tests fail because delete_deal() does not accept include_deleted and does not emit CRMDealMutationEvent
```

- [ ] **Step 5: Add `is_deleted` to auditable fields**

In `_AUDITABLE_DEAL_FIELD_NAMES`, add:

```python
"is_deleted",
```

- [ ] **Step 6: Make `_load_internal_deal()` delete-safe**

Change `_load_internal_deal()` signature:

```python
async def _load_internal_deal(
    self,
    deal_id: UUID,
    *,
    include_deleted: bool = False,
) -> CRMDeal:
```

Change:

```python
if deal.is_deleted:
    raise DealWriteServiceError("Deal not found", status_code=404)
```

to:

```python
if deal.is_deleted and not include_deleted:
    raise DealWriteServiceError("Deal not found", status_code=404)
```

- [ ] **Step 7: Implement delete diff-first write**

Change `delete_deal()` signature:

```python
async def delete_deal(
    self,
    deal_id: UUID,
    *,
    source: str = "api",
    source_label: str | None = None,
) -> CRMDeal:
```

Replace the method body after the docstring with:

```python
deal = await self._load_internal_deal(deal_id, include_deleted=True)

now = datetime.now(UTC)
target_values = {
    "is_deleted": True,
    "deal_status": CRMDealStatus.CLOSED,
}
changed_fields = self._build_deal_changed_fields(deal, target_values)
if not changed_fields:
    return deal

deal.is_deleted = True
deal.deal_status = CRMDealStatus.CLOSED
deal.source_modified_at = now
deal.synced_at = now
deal.last_seen_at = now
deal.properties = {**(deal.properties or {}), "deleted_via": source}

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
logger.info("Deleted internal CRM deal %s", deal.deal_id)
return deal
```

- [ ] **Step 8: Run delete tests and verify GREEN**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_mutation_events.py -q
```

Expected:

```text
owner/delete/account/contact mutation tests pass
```

---

## Task 3: Contract Documentation

**Files:**

- Modify: `docs/superpowers/plans/2026-06-16-crm-write-mutation-contract.md`
- Modify: `docs/superpowers/plans/2026-06-17-crm-owner-account-contact-delete-mutation-plan.md`

- [ ] **Step 1: Update Phase 5 status**

In `docs/superpowers/plans/2026-06-16-crm-write-mutation-contract.md`, replace
the final wrapped sentence in the Phase 5 status paragraph:

```markdown
when the active primary-contact link already exists. Owner/delete events remain
pending.
```

with:

```markdown
when the active primary-contact link already exists. Owner/delete events are
implemented. `assign_owner()` emits triggerable `crm.deal.owner_changed` events
only when `owner_id` changes, and emits audit-only
`crm.deal.owner_snapshot_refreshed` events for same-owner display refreshes.
`delete_deal()` emits triggerable `crm.deal.deleted` events, is idempotent for
already-deleted retries, and does not expose `crm.deal.status_changed`
semantics even though `deal_status` is retained in `changed_fields` for audit.
```

- [ ] **Step 2: Mark owner/delete task plan as implemented**

In `docs/superpowers/plans/2026-06-17-crm-owner-account-contact-delete-mutation-plan.md`, add a status note near the top:

```markdown
Status on 2026-06-17: account/contact audit events are implemented. Owner/delete
events are implemented by the focused owner/delete plan.
```

---

## Task 4: Final Verification

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
git diff --check -- src/service/crm/internal/deal_write_service.py tests/service/crm/test_deal_write_service_mutation_events.py docs/superpowers/plans/2026-06-16-crm-write-mutation-contract.md docs/superpowers/plans/2026-06-17-crm-owner-account-contact-delete-mutation-plan.md docs/superpowers/plans/2026-06-17-crm-owner-delete-mutation-implementation-plan.md
```

Expected:

```text
exit code 0
```

The repository may print LF-to-CRLF warnings for touched files; those are not whitespace errors.

---

## Execution Order

1. Task 1: owner events.
2. Task 2: delete events.
3. Task 3: docs.
4. Task 4: final verification.

Owner comes first because it does not require the deleted-row loader change. Delete comes second because it touches `_load_internal_deal()`, which is shared by every internal write method.

---

## Production Acceptance Criteria

- Different-owner assignment emits one `crm.deal.owner_changed` mutation event.
- Clear-owner emits one `crm.deal.owner_changed` mutation event.
- Same-owner retry with same display snapshot creates no event and does not touch timestamps.
- Same-owner display refresh emits audit-only `crm.deal.owner_snapshot_refreshed`.
- Owner events preserve default `source=api` and `source_label=api`.
- Soft-delete emits one `crm.deal.deleted` mutation event.
- Already-deleted retry returns the deal without timestamp changes or duplicate event.
- Delete does not include `crm.deal.status_changed` in `semantic_event_types`.
- Only `delete_deal()` uses `_load_internal_deal(include_deleted=True)`; other
  internal writes keep the default deleted-row rejection.
- Existing account/contact/stage/lifecycle mutation tests remain green.
- No direct workflow dispatch is added in this phase.
