# CRM Deal Mutation Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring deal mutation events up to the same production contract as contact/account mutation events: one durable audit row per real deal write, curated workflow semantics, JSON-safe payloads, and no workflow runtime state.

**Architecture:** Keep the current modular deal services, but replace deal-specific event-writing behavior with the shared `CRMMutationEventService` contract. `DealWriteService` remains the top-level orchestrator that records one event per public deal write; stage, owner, lifecycle, relationship, and custom-field helpers return changed fields and do not dispatch workflows.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy async sessions, PostgreSQL `crm_mutation_events`, pytest, Ruff/Mypy at PR time.

---

## Current Repo Facts

The current checkout already has the modular deal package:

- `src/service/crm/deals/write_service.py`
- `src/service/crm/deals/stage_service.py`
- `src/service/crm/deals/owner_service.py`
- `src/service/crm/deals/lifecycle_service.py`
- `src/service/crm/deals/relationship_service.py`
- `src/service/crm/deals/mutation_event_service.py`
- `src/service/crm/deals/contracts.py`

Shared mutation infrastructure already exists and is used by contacts/accounts:

- `src/service/crm/mutations/contracts.py`
- `src/service/crm/mutations/event_service.py`
- `src/service/crm/mutations/json_safe.py`
- `src/service/crm/mutations/policies.py`

Deal mutation events are partially implemented today:

- `DealWriteService.create_deal()` records `crm.deal.created`.
- `DealWriteService.update_deal()` records `crm.deal.updated`.
- `DealWriteService.move_stage()` records `crm.deal.stage_changed`.
- `DealWriteService.close_deal()` records `crm.deal.closed`.
- `DealWriteService.reopen_deal()` records `crm.deal.reopened`.
- `DealWriteService.assign_owner()` records `crm.deal.owner_changed`.
- `DealWriteService.update_account()` records `crm.deal.account_changed`.
- `DealWriteService.update_primary_contact()` records `crm.deal.primary_contact_changed`.
- `DealWriteService.delete_deal()` records `crm.deal.deleted`.

But the current deal event path is not aligned with the production contract:

- `src/service/crm/deals/mutation_event_service.py` writes directly to `CRMDealMutationEvent` instead of delegating to `CRMMutationEventService`.
- Deal event idempotency stores the raw caller key, while shared mutation service derives a scoped key.
- Deal changed fields are not normalized through shared JSON-safe rules before hashing/storage.
- Deal semantic derivation currently maps many fields to workflow-triggerable events, including fallback events like `crm.deal.priority_changed`.
- Deal creation can produce stage/owner/account-style semantics if semantics are derived only from changed field names.
- `DealLifecycleService` exists but `DealWriteService.close_deal()` and `reopen_deal()` still implement lifecycle field mutation inline.
- `DealWriteService._write_context()` currently hardcodes `source="api"`, even though deal writes can come from API, agent tools, bot forms, import assistant, and future workflows.
- Deal creation/update can create or soft-delete participant rows as side effects, but this plan does not yet define participant/stakeholder mutation events.

## Product Decision

Deals should be the final and most carefully controlled CRM mutation phase.

Reason:

- Deal stage, amount, owner, lifecycle, account, contact, and custom fields are the highest-value workflow triggers.
- Deal update is also the highest-risk surface for noise, because users can update many fields in one save.
- Workflow semantics must be curated. Audit history can contain many fields, but workflows should not trigger on every changed scalar by default.

This plan covers deal-state mutation events only. It does not create standalone mutation events for `CRMDealParticipant` rows, even when a deal write creates, replaces, or soft-deletes participants as a side effect.

## Event Type Contract

Use event type as the action envelope:

| Public write action | `event_type` |
| --- | --- |
| Create deal | `crm.deal.created` |
| Update scalar/custom deal fields | `crm.deal.updated` |
| Move stage | `crm.deal.stage_changed` |
| Assign/clear owner | `crm.deal.owner_changed` |
| Close deal | `crm.deal.closed` |
| Reopen deal | `crm.deal.reopened` |
| Update account link/snapshot | `crm.deal.account_changed` |
| Update primary contact link/snapshot | `crm.deal.primary_contact_changed` |
| Soft delete deal | `crm.deal.deleted` |

Use semantic event types only for workflow-matchable business meanings:

| Condition | Semantic event |
| --- | --- |
| Deal created | `crm.deal.created` |
| `stage_id` changed on an update/stage/lifecycle action | `crm.deal.stage_changed` |
| `amount` changed | `crm.deal.amount_changed` |
| `owner_id` changed | `crm.deal.owner_changed` |
| Close action completed | `crm.deal.closed` |
| Reopen action completed | `crm.deal.reopened` |
| Account link changed through the account action | `crm.deal.account_changed` |
| Primary contact link changed through the primary-contact action | `crm.deal.primary_contact_changed` |
| Deal soft deleted | `crm.deal.deleted` |

Do not expose broad `crm.deal.updated` as a semantic workflow event in V1.

Do not expose every scalar/custom field as a semantic event in V1.

Reason: `priority`, `description`, `expected_close_date`, `forecast_category`, custom fields, and `lead_source` are important audit fields, but automatically making them workflow triggers will create noisy and surprising automations. Later workflow configuration can add explicit field-trigger support after the workflow builder can show and validate those fields.

## Audit Field Contract

Record audit-safe deal fields:

```text
name
amount
currency
pipeline_id
pipeline_name
stage_id
stage_name
deal_status
probability
forecast_category
owner_id
owner_name
owner_email
company_id
company_name
company_domain
primary_contact_id
primary_contact_name
primary_contact_email
close_date
expected_close_date
closed_won_at
closed_lost_at
closed_reason
crm_url
description
deal_type
priority
lead_source
is_deleted
custom_properties.<key>
```

Do not record:

```text
properties.updated_via
properties.deleted_via
properties.closed_via
properties.reopened_via
properties.closed_by_user_id
properties.reopened_by_user_id
properties.created_via
raw provider payloads
credentials
tokens
workflow runtime state
```

`custom_properties.<key>` is audit-only in this phase unless product later approves custom-field workflow triggers.

Delete metadata such as `properties.deleted_via` remains out of `changed_fields` in this slice. The delete audit event records the user-visible state changes: `is_deleted` and `deal_status`.

Custom field nulls mean explicit stored null values, not key deletion. Removing a custom field key from `deal.properties` is out of scope until the custom-field API exposes a clear/delete operation. This avoids silently treating omitted keys, null values, and deleted keys as the same user intent.

## Participant Boundary

Deal writes may create or update participant rows:

```text
owner participant
company participant
primary contact participant
team member participants
contact participants
stakeholder participants
```

These participant row changes are not separate mutation events in this deal mutation phase.

Rules:

- Deal mutation events record the deal snapshot effect, such as `owner_id`, `company_id`, or `primary_contact_id`.
- They do not record participant row payloads, participant IDs, decision power, stance, influence, relationship role, or stakeholder enrichment.
- A later participant/stakeholder mutation plan should define events such as `crm.deal.participant_added`, `crm.deal.participant_removed`, or `crm.deal.stakeholder_updated`.
- Owner participant synchronization remains a side effect of `crm.deal.owner_changed`; it should not create an additional participant mutation row in this phase.

This keeps “deal state changed” separate from “relationship/person row changed.”

## Source And Context Rule

Deal mutation events must preserve the real caller source.

Current behavior:

```text
DealWriteService._write_context() -> DealWriteContext(source="api", actor_user_id=...)
```

Target behavior:

```text
public deal write methods accept context: DealWriteContext | None = None
legacy callers omit context and get source="api"
agent tools pass source="agent"
bot forms pass source="bot_form"
import assistant passes source="import_assistant"
future workflow writes pass source="workflow"
```

Do not block this phase on migrating every caller. The minimum production correction is:

- Add optional `context` to public deal write methods.
- Use the provided context when present.
- Keep the current API default for backward compatibility.
- Add focused tests that direct calls can produce `source="agent"` or another non-API source.

Caller-level idempotency is also not guaranteed in this phase. The internal deal event service should support scoped idempotency through `DealWriteContext.idempotency_key`, but API/agent endpoints only get end-user retry safety after their request contracts expose and pass an idempotency key.

## Target Flow

Every public deal write should follow this model:

```text
public write method
  -> receive DealWriteContext or build legacy API context
  -> load and validate internal deal
  -> capture old audit-safe values
  -> apply one user action
  -> collect changed_fields from final normalized model state
  -> DealMutationPolicy derives semantic_event_types
  -> DealMutationEventService delegates to CRMMutationEventService
  -> caller commits or rolls back
```

No workflow dispatch happens here.

The event row exists so a future dispatcher can later do:

```text
crm_mutation_events
  -> workflow trigger matcher
  -> workflow run records
  -> workflow workers
```

## Multi-Field Update Rule

One API/agent save that changes five fields creates one mutation event.

Example:

```json
{
  "event_type": "crm.deal.updated",
  "entity_type": "deal",
  "entity_id": "deal_123",
  "semantic_event_types": ["crm.deal.amount_changed"],
  "changed_fields": {
    "amount": {"old": 50000, "new": 65000},
    "expected_close_date": {"old": "2026-07-01", "new": "2026-08-01"},
    "priority": {"old": "medium", "new": "high"},
    "description": {"old": "Old note", "new": "New note"},
    "custom_properties.procurement_stage": {"old": "legal", "new": "security"}
  }
}
```

Only `amount` is semantic-triggerable in V1. The other fields remain audit history.

Same-value update rule:

```text
if the final changed_fields payload is empty
  -> CRMMutationEventService skips inserting crm_mutation_events
```

Current code may still update sync timestamps for same-value payloads. This plan guarantees no mutation event for same-value writes, but does not change timestamp churn unless the implementation task explicitly adds a no-op guard. A later write-contract cleanup should decide whether same-value payloads should also skip timestamps.

## Create Event Rule

Deal create records initial values, but it should not imply update semantics.

Example:

```json
{
  "event_type": "crm.deal.created",
  "semantic_event_types": ["crm.deal.created"],
  "changed_fields": {
    "deal_id": {"old": null, "new": "deal_123"},
    "name": {"old": null, "new": "Enterprise Renewal"},
    "stage_id": {"old": null, "new": "stage_discovery"},
    "stage_name": {"old": null, "new": "Discovery"},
    "owner_id": {"old": null, "new": "user_123"}
  }
}
```

Do not add `crm.deal.stage_changed` or `crm.deal.owner_changed` to `semantic_event_types` on create. Creation-specific workflows can still match `crm.deal.created` and conditions like initial stage or owner later.

## Lifecycle Rule

Close and reopen are action events, not generic deal updates.

Close example:

```json
{
  "event_type": "crm.deal.closed",
  "semantic_event_types": ["crm.deal.closed", "crm.deal.stage_changed"],
  "changed_fields": {
    "stage_id": {"old": "stage_proposal", "new": "stage_won"},
    "deal_status": {"old": "open", "new": "won"},
    "close_date": {"old": null, "new": "2026-06-20T10:00:00+00:00"},
    "closed_won_at": {"old": null, "new": "2026-06-20T10:00:00+00:00"}
  }
}
```

If the close action moves stage, include `crm.deal.stage_changed` because users commonly attach workflows to entering a closed-won/closed-lost stage.

If close does not move stage, do not include stage semantics.

Lifecycle no-op rule:

- Closing an already-won deal as won with the same close fields should not create a mutation event.
- Closing an already-lost deal as lost with the same close fields should not create a mutation event.
- Reopening an already-open deal with the same stage and no closed-field changes should not create a mutation event.
- If the current implementation still mutates timestamps in those no-op cases, the mutation recorder must still receive empty `changed_fields` and skip the insert.

## Relationship Event Semantics

In V1, deal account and primary-contact actions are workflow-triggerable:

```text
crm.deal.account_changed
crm.deal.primary_contact_changed
```

This is intentionally limited to the public deal relationship actions:

```text
DealWriteService.update_account()
DealWriteService.update_primary_contact()
```

Deal creation may set initial account/contact snapshots, but it should still emit only `crm.deal.created` semantics. If product later wants “deal created with account” or “deal created with primary contact” triggers, add creation-condition matching in the workflow layer instead of emitting update-style semantics on create.

## Implementation Tasks

### Task 1: Add Deal Mutation Policy

**Files:**

- Modify: `src/service/crm/mutations/policies.py`
- Create or modify: `tests/service/crm/test_deal_mutation_policy.py`

- [ ] **Step 1: Write failing policy tests**

Create `tests/service/crm/test_deal_mutation_policy.py`:

```python
from __future__ import annotations

from uuid import uuid4

from src.service.crm.mutations.policies import DealMutationPolicy


def test_deal_create_semantics_are_creation_only() -> None:
    changed_fields = {
        "stage_id": {"old": None, "new": str(uuid4())},
        "owner_id": {"old": None, "new": str(uuid4())},
        "amount": {"old": None, "new": 1000},
    }

    assert DealMutationPolicy.semantic_for_event("crm.deal.created", changed_fields) == [
        "crm.deal.created"
    ]


def test_deal_update_semantics_are_curated() -> None:
    changed_fields = {
        "amount": {"old": 1000, "new": 2000},
        "priority": {"old": "medium", "new": "high"},
        "description": {"old": "old", "new": "new"},
        "custom_properties.procurement_stage": {"old": "legal", "new": "security"},
    }

    assert DealMutationPolicy.semantic_for_event("crm.deal.updated", changed_fields) == [
        "crm.deal.amount_changed"
    ]


def test_stage_and_owner_actions_have_specific_semantics() -> None:
    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.stage_changed",
        {"stage_id": {"old": "a", "new": "b"}},
    ) == ["crm.deal.stage_changed"]

    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.owner_changed",
        {"owner_name": {"old": "Old", "new": "New"}},
    ) == ["crm.deal.owner_changed"]


def test_lifecycle_action_semantics_include_stage_only_when_stage_changed() -> None:
    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.closed",
        {
            "deal_status": {"old": "open", "new": "won"},
            "stage_id": {"old": "proposal", "new": "won"},
        },
    ) == ["crm.deal.closed", "crm.deal.stage_changed"]

    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.reopened",
        {"deal_status": {"old": "won", "new": "open"}},
    ) == ["crm.deal.reopened"]
```

- [ ] **Step 2: Run the failing policy tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_policy.py -q
```

Expected before implementation:

```text
FAILED tests/service/crm/test_deal_mutation_policy.py
```

- [ ] **Step 3: Implement `DealMutationPolicy`**

Add to `src/service/crm/mutations/policies.py`:

```python
class DealMutationPolicy:
    """Derive workflow-matchable semantics from deal action events."""

    @staticmethod
    def semantic_for_event(
        event_type: str,
        changed_fields: dict[str, dict[str, Any]],
    ) -> list[str]:
        if not changed_fields:
            return []

        if event_type == "crm.deal.created":
            return ["crm.deal.created"]
        if event_type == "crm.deal.stage_changed":
            return ["crm.deal.stage_changed"]
        if event_type == "crm.deal.owner_changed":
            return ["crm.deal.owner_changed"]
        if event_type == "crm.deal.account_changed":
            return ["crm.deal.account_changed"]
        if event_type == "crm.deal.primary_contact_changed":
            return ["crm.deal.primary_contact_changed"]
        if event_type == "crm.deal.deleted":
            return ["crm.deal.deleted"]

        if event_type in {"crm.deal.closed", "crm.deal.reopened"}:
            semantics = [event_type]
            if "stage_id" in changed_fields:
                semantics.append("crm.deal.stage_changed")
            return semantics

        if event_type == "crm.deal.updated":
            semantics: list[str] = []
            if "stage_id" in changed_fields:
                semantics.append("crm.deal.stage_changed")
            if "amount" in changed_fields:
                semantics.append("crm.deal.amount_changed")
            if "owner_id" in changed_fields:
                semantics.append("crm.deal.owner_changed")
            return semantics

        return []
```

- [ ] **Step 4: Verify policy tests pass**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_policy.py -q
```

Expected:

```text
passed
```

### Task 2: Migrate Deal Event Service To Shared Mutation Recorder

**Files:**

- Modify: `src/service/crm/deals/mutation_event_service.py`
- Modify: `tests/service/crm/test_deal_mutation_event_service.py`

- [ ] **Step 1: Add failing shared-recorder behavior tests**

Extend `tests/service/crm/test_deal_mutation_event_service.py` with:

```python
def test_deal_event_service_uses_scoped_idempotency_key() -> None:
    source = Path("src/service/crm/deals/mutation_event_service.py").read_text()

    assert "CRMMutationEventService" in source
    assert "CRMMutationRecordCommand" in source
    assert "idempotency_scope=f\"deal:" in source


def test_deal_event_service_does_not_hash_payload_locally() -> None:
    source = Path("src/service/crm/deals/mutation_event_service.py").read_text()

    assert "hashlib" not in source
    assert "json.dumps" not in source
```

Add `from pathlib import Path` at the top of the test file.

- [ ] **Step 2: Run the failing event-service tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_event_service.py -q
```

Expected before implementation:

```text
FAILED tests/service/crm/test_deal_mutation_event_service.py::test_deal_event_service_uses_scoped_idempotency_key
FAILED tests/service/crm/test_deal_mutation_event_service.py::test_deal_event_service_does_not_hash_payload_locally
```

- [ ] **Step 3: Replace local insert/hash logic with shared service delegation**

Update `src/service/crm/deals/mutation_event_service.py` to keep the public deal-specific wrapper but delegate to shared mutation service:

```python
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError
from src.service.crm.mutations.contracts import (
    CRMMutationContext,
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
    CRMMutationRecordResult,
)
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.json_safe import changed_field


class DealMutationEventService:
    """Deal-specific facade over the shared CRM mutation event recorder."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._events = CRMMutationEventService(session, organisation_id)

    async def record_event(
        self,
        *,
        deal: CRMDeal,
        event_type: str,
        operation: str,
        changed_fields: dict[str, dict[str, Any]],
        semantic_event_types: list[str],
        context: DealWriteContext,
        summary: str | None,
    ) -> CRMMutationRecordResult:
        command = CRMMutationRecordCommand(
            entity_type="deal",
            entity_id=deal.deal_id,
            deal_id=deal.deal_id,
            event_type=event_type,
            operation=operation,
            changed_fields=changed_fields,
            semantic_event_types=semantic_event_types,
            context=CRMMutationContext(
                source=context.source,
                actor_user_id=context.actor_user_id,
                source_label=context.source_label,
                approval_state_id=context.approval_state_id,
                correlation_id=context.correlation_id,
                idempotency_key=context.idempotency_key,
            ),
            summary=summary,
            integration_id=deal.integration_id,
            idempotency_scope=f"deal:{operation}:{deal.deal_id}",
        )
        try:
            return await self._events.record_event(command)
        except CRMMutationEventServiceError as exc:
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc
```

Keep `changed_field` import/export from this file for current stage/owner/lifecycle imports. Moving those imports to `src.service.crm.mutations.json_safe` can be a later cleanup.

- [ ] **Step 4: Update tests that expected ORM event objects**

Where tests previously asserted `isinstance(event, CRMDealMutationEvent)`, update them to inspect the captured `CRMDealMutationEvent` in fake session `added`, or assert the returned `CRMMutationRecordResult`.

Example expected test assertion:

```python
result = await service.record_event(
    deal=deal,
    event_type="crm.deal.updated",
    operation="update_deal",
    changed_fields={"amount": {"old": 100, "new": 200}},
    semantic_event_types=["crm.deal.amount_changed"],
    context=_context(),
    summary="Updated deal",
)

assert result.changed is True
event = service.session.added[0]
assert isinstance(event, CRMDealMutationEvent)
assert event.entity_type == "deal"
assert event.deal_id == deal.deal_id
```

- [ ] **Step 5: Verify deal mutation event service tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_event_service.py -q
```

Expected:

```text
passed
```

### Task 3: Replace Broad Deal Semantic Derivation

**Files:**

- Modify: `src/service/crm/deals/write_service.py`
- Test: `tests/service/crm/test_deal_mutation_semantics.py`

- [ ] **Step 1: Write failing tests for current broad semantics**

Create `tests/service/crm/test_deal_mutation_semantics.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_create_deal_does_not_emit_stage_or_owner_semantics() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.created",
        operation="create_deal",
        changed_fields={
            "stage_id": {"old": None, "new": str(uuid4())},
            "owner_id": {"old": None, "new": str(uuid4())},
        },
        summary="Created deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["semantic_event_types"] == ["crm.deal.created"]


@pytest.mark.asyncio
async def test_update_deal_description_is_audit_only() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"description": {"old": "old", "new": "new"}},
        summary="Updated deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["semantic_event_types"] == []
```

- [ ] **Step 2: Run failing semantic tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_semantics.py -q
```

Expected before implementation:

```text
FAILED tests/service/crm/test_deal_mutation_semantics.py
```

- [ ] **Step 3: Use `DealMutationPolicy` in `_record_mutation_event()`**

Modify `src/service/crm/deals/write_service.py`:

```python
from src.service.crm.mutations.policies import DealMutationPolicy
```

Replace the current field-loop semantic derivation in `_record_mutation_event()` with:

```python
semantic_event_types = DealMutationPolicy.semantic_for_event(
    event_type,
    changed_fields,
)
```

Remove `_semantic_event_type()` if no code uses it after this change.

- [ ] **Step 4: Verify semantic tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_semantics.py -q
```

Expected:

```text
passed
```

### Task 4: Expand Create And Update Changed Fields

**Files:**

- Modify: `src/service/crm/deals/write_service.py`
- Modify: `tests/service/crm/test_deal_write_service_stage_history.py`
- Create or modify: `tests/service/crm/test_deal_mutation_semantics.py`

- [ ] **Step 1: Add failing create-field coverage**

Add this test to `tests/service/crm/test_deal_write_service_stage_history.py`:

```python
@pytest.mark.asyncio
async def test_create_deal_records_initial_core_fields_without_update_semantics() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._record_mutation_event = AsyncMock()

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, name="Sales")
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery")
    integration = SimpleNamespace(integration_id=integration_id)

    service._get_default_pipeline = AsyncMock(return_value=pipeline)
    service._get_first_stage = AsyncMock(return_value=stage)

    with patch(
        "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
        new=AsyncMock(return_value=integration),
    ):
        await service.create_deal(
            {
                "name": "New Deal",
                "amount": 25000,
                "currency": "USD",
                "priority": "high",
                "expected_close_date": "2026-07-31",
            }
        )

    event_kwargs = service._record_mutation_event.await_args.kwargs
    assert event_kwargs["event_type"] == "crm.deal.created"
    assert event_kwargs["changed_fields"]["name"] == {"old": None, "new": "New Deal"}
    assert event_kwargs["changed_fields"]["amount"] == {"old": None, "new": 25000}
    assert event_kwargs["changed_fields"]["currency"] == {"old": None, "new": "USD"}
    assert event_kwargs["changed_fields"]["pipeline_id"]["new"] == pipeline_id
    assert event_kwargs["changed_fields"]["pipeline_name"] == {"old": None, "new": "Sales"}
    assert event_kwargs["changed_fields"]["stage_id"]["new"] == stage_id
    assert event_kwargs["changed_fields"]["stage_name"] == {"old": None, "new": "Discovery"}
```

- [ ] **Step 2: Update create changed fields**

In `create_deal()`, include useful initial fields when non-null:

```text
deal_id
name
amount
currency
pipeline_id
pipeline_name
stage_id
stage_name
deal_status
owner_id
owner_name
owner_email
company_id
company_name
company_domain
primary_contact_id
primary_contact_name
primary_contact_email
close_date
expected_close_date
description
deal_type
priority
lead_source
probability
forecast_category
```

Keep `event_type="crm.deal.created"` and rely on `DealMutationPolicy` for creation-only semantics.

- [ ] **Step 3: Add failing update same-value/noise test**

Add to `tests/service/crm/test_deal_mutation_semantics.py`:

```python
@pytest.mark.asyncio
async def test_update_deal_same_value_records_no_mutation_event() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing",
        amount=1000,
        currency="USD",
        deal_status="open",
        close_date=None,
        expected_close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        crm_url=None,
        source_created_at=None,
        last_activity_at=None,
        description=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        probability=None,
        forecast_category=None,
        properties={},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._record_mutation_event = AsyncMock()

    await service.update_deal(deal.deal_id, {"amount": 1000})

    service._record_mutation_event.assert_awaited_once()
    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["changed_fields"] == {}
```

This test locks the current decision: a same-value payload may still pass through the endpoint, but it must not create a mutation event. A later cleanup can stop timestamp churn for same-value updates.

- [ ] **Step 4: Verify create/update tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py tests/service/crm/test_deal_mutation_semantics.py -q
```

Expected:

```text
passed
```

### Task 5: Preserve Caller Source And Context

**Files:**

- Modify: `src/service/crm/deals/write_service.py`
- Modify: `src/service/crm/deals/contracts.py` only if context shape needs a new optional field
- Create or modify: `tests/service/crm/test_deal_mutation_context.py`

- [ ] **Step 1: Add source propagation tests**

Create `tests/service/crm/test_deal_mutation_context.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.service.crm.deals.contracts import DealWriteContext
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_record_mutation_event_uses_supplied_context_source() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()
    context = DealWriteContext(source="agent", actor_user_id=uuid4(), source_label="Chat agent")

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        summary="Updated deal",
        context=context,
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["context"] is context


@pytest.mark.asyncio
async def test_record_mutation_event_defaults_to_api_context_for_legacy_callers() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        summary="Updated deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["context"].source == "api"
    assert kwargs["context"].actor_user_id == service.actor_user_id
```

- [ ] **Step 2: Update `_record_mutation_event()` signature**

Change `src/service/crm/deals/write_service.py`:

```python
async def _record_mutation_event(
    self,
    *,
    deal: CRMDeal,
    event_type: str,
    operation: str,
    changed_fields: dict[str, dict[str, Any]],
    summary: str | None,
    context: DealWriteContext | None = None,
) -> None:
    semantic_event_types = DealMutationPolicy.semantic_for_event(
        event_type,
        changed_fields,
    )
    await self._mutation_events.record_event(
        deal=deal,
        event_type=event_type,
        operation=operation,
        changed_fields=changed_fields,
        semantic_event_types=semantic_event_types,
        context=context or self._write_context(),
        summary=summary,
    )
```

- [ ] **Step 3: Add optional context to public write methods**

Add `context: DealWriteContext | None = None` to public deal write methods in a backward-compatible way:

```text
create_deal
update_deal
delete_deal
move_stage
close_deal
reopen_deal
assign_owner
update_account
update_primary_contact
```

Pass that context into `_record_mutation_event()`. Do not migrate every API/agent caller in this task; legacy defaults remain valid.

- [ ] **Step 4: Verify source propagation tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_context.py -q
```

Expected:

```text
passed
```

### Task 6: Lifecycle Mutation Tightening

**Files:**

- Modify: `src/service/crm/deals/write_service.py`
- Modify: `src/service/crm/deals/lifecycle_service.py` only if moving inline lifecycle mutation into the helper
- Create or modify: `tests/service/crm/test_deal_lifecycle_mutation_events.py`

- [ ] **Step 1: Add lifecycle tests**

Create `tests/service/crm/test_deal_lifecycle_mutation_events.py` with focused fake-service tests:

```python
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_close_deal_records_closed_and_stage_semantics_when_stage_changes() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=old_stage_id,
        stage_name="Proposal",
        deal_status=CRMDealStatus.OPEN,
        close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=new_stage_id, name="Closed Won", stage_state="won")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock(
        side_effect=lambda deal, stage, source: setattr(deal, "stage_id", stage.stage_id)
        or setattr(deal, "stage_name", stage.name)
    )
    service._record_mutation_event = AsyncMock()

    await service.close_deal(
        deal.deal_id,
        outcome="won",
        closed_reason=None,
        close_date=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.closed"
    assert "stage_id" in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]
```

Add a second test for reopen without stage change that asserts no `stage_id` in changed fields when the resolved stage equals the current stage.

Add a third test for lifecycle no-op behavior:

```python
@pytest.mark.asyncio
async def test_reopen_open_deal_records_no_changed_fields_when_nothing_changes() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=stage_id,
        stage_name="Discovery",
        deal_status=CRMDealStatus.OPEN,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery", stage_state="open")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.reopen_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.reopened"
    assert kwargs["changed_fields"] == {}
```

- [ ] **Step 2: Decide helper migration**

If lifecycle field mutation stays inline for this slice, add a code comment in `DealWriteService.close_deal()`:

```python
# Lifecycle mutation remains here until DealLifecycleService can also resolve
# close/reopen stages without duplicating stage-history behavior.
```

If moving it now, update `DealLifecycleService` so it returns `DealLifecycleChangeResult` and lets `DealWriteService` remain the only mutation-event recorder.

- [ ] **Step 3: Verify lifecycle tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_lifecycle_mutation_events.py -q
```

Expected:

```text
passed
```

### Task 7: Relationship Mutation Tightening

**Files:**

- Modify: `src/service/crm/deals/write_service.py`
- Modify: `tests/service/crm/test_deal_write_service_account_updates.py`
- Create or modify: `tests/service/crm/test_deal_relationship_mutation_events.py`

- [ ] **Step 1: Add account no-op event test**

Add to `tests/service/crm/test_deal_relationship_mutation_events.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_update_account_same_link_records_no_changed_fields() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
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
    )
    company = SimpleNamespace(company_id=company_id, name="Acme", domain="acme.com")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._account_writer.upsert_company_from_account = AsyncMock(return_value=(company, None))
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._sync_deal_company_snapshot = lambda deal, company, now: None
    service._record_mutation_event = AsyncMock()

    await service.update_account(deal.deal_id, {"name": "Acme", "domain": "acme.com"})

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.account_changed"
    assert kwargs["changed_fields"] == {}
```

- [ ] **Step 2: Add primary contact fallback event test**

Add a test that mirrors `test_update_primary_contact_clears_internal_link_when_only_fallback_values_exist()` and asserts `changed_fields` includes:

```text
primary_contact_id
primary_contact_name
primary_contact_email
```

- [ ] **Step 3: Add participant boundary source test**

Add a source-level boundary test so this deal slice does not accidentally start writing participant mutation events:

```python
from pathlib import Path


def test_deal_mutation_plan_does_not_emit_participant_mutation_events() -> None:
    source = Path("src/service/crm/deals/write_service.py").read_text()

    assert "crm.deal.participant_added" not in source
    assert "crm.deal.participant_removed" not in source
    assert "crm.deal.stakeholder_updated" not in source
```

- [ ] **Step 4: Verify relationship tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_relationship_mutation_events.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected:

```text
passed
```

### Task 8: Delete Event Semantics

**Files:**

- Modify: `src/service/crm/deals/write_service.py`
- Create or modify: `tests/service/crm/test_deal_delete_mutation_events.py`

- [ ] **Step 1: Add delete semantics test**

Create `tests/service/crm/test_deal_delete_mutation_events.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_delete_deal_uses_delete_semantic_only() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        is_deleted=False,
        deal_status=CRMDealStatus.OPEN,
        properties={},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
        name="Delete Me",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._record_mutation_event = AsyncMock()

    await service.delete_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.deleted"
    assert set(kwargs["changed_fields"]) == {"is_deleted", "deal_status"}
```

The semantic assertion is covered by `DealMutationPolicy`: delete means `crm.deal.deleted`, not lifecycle changed.

- [ ] **Step 2: Verify delete tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_delete_mutation_events.py tests/service/crm/test_deal_mutation_policy.py -q
```

Expected:

```text
passed
```

### Task 9: Focused Regression Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Run focused mutation tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_event_service.py tests/service/crm/test_deal_mutation_policy.py tests/service/crm/test_deal_mutation_semantics.py tests/service/crm/test_deal_mutation_context.py tests/service/crm/test_deal_lifecycle_mutation_events.py tests/service/crm/test_deal_relationship_mutation_events.py tests/service/crm/test_deal_delete_mutation_events.py -q
```

Expected:

```text
passed
```

- [ ] **Step 2: Run existing deal service regression tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py tests/service/crm/test_deal_owner_service.py tests/service/crm/test_deal_stage_service.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_deal_write_service_account_copy.py tests/service/crm/test_deal_write_service_utils.py tests/service/crm/test_deal_service_boundaries.py -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Run shared mutation/contact/account regression tests**

Run:

```bash
uv run pytest tests/service/crm/test_crm_mutation_event_service.py tests/service/crm/test_contact_mutation_events.py tests/service/crm/test_account_mutation_events.py -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Run focused Ruff and Mypy before PR**

Run:

```bash
uv run ruff check src/service/crm/deals src/service/crm/mutations tests/service/crm/test_deal_mutation_event_service.py tests/service/crm/test_deal_mutation_policy.py tests/service/crm/test_deal_mutation_semantics.py tests/service/crm/test_deal_mutation_context.py tests/service/crm/test_deal_lifecycle_mutation_events.py tests/service/crm/test_deal_relationship_mutation_events.py tests/service/crm/test_deal_delete_mutation_events.py
```

Expected:

```text
passed
```

Run:

```bash
uv run mypy --no-incremental src/service/crm/deals src/service/crm/mutations
```

Expected:

```text
passed
```

## Loophole Review

### Loophole 1: Create event triggers stage/owner workflows

Resolution: `DealMutationPolicy.semantic_for_event("crm.deal.created", ...)` returns only `crm.deal.created`, even if initial stage and owner are recorded in `changed_fields`.

### Loophole 2: Every changed scalar becomes a workflow trigger

Resolution: broad update events are audit envelopes. `priority`, `description`, `expected_close_date`, `forecast_category`, and custom fields are audit-only in V1.

### Loophole 3: Deal event service diverges from contact/account idempotency

Resolution: keep `DealMutationEventService` as a compatibility facade, but delegate inserts and scoped idempotency to `CRMMutationEventService`.

### Loophole 4: JSON values are stored differently across entities

Resolution: shared service normalizes `changed_fields`, so UUID/datetime/date/Decimal/list/dict values are stored consistently.

### Loophole 5: Lifecycle writes double emit stage and close events

Resolution: `close_deal()` and `reopen_deal()` remain one top-level action event. Stage changes inside those actions appear as `changed_fields` and optional semantic event types, not separate mutation rows.

### Loophole 6: Relationship actions create events when nothing changed

Resolution: existing `CRMMutationEventService.record_event()` returns `changed=False` and does not insert when `changed_fields` is empty. Tests lock the no-change payload behavior.

### Loophole 7: Workflow runtime leaks into mutation table

Resolution: do not add workflow run IDs, dispatch status, retry count, worker lease fields, or workflow errors to deal mutation events.

### Loophole 8: Provider sync volume is mixed into internal deal mutation events

Resolution: this plan touches only internal deal writes. HubSpot/Salesforce provider-origin mutation events need a separate coalescing and opt-in workflow-trigger design.

### Loophole 9: Account/contact creation during deal creation is ambiguous

Resolution: this plan records deal mutation events only. Account/contact events created as part of deal creation require a later multi-entity `mutation_group_id` design. Do not accidentally wire grouped account/contact events in this deal slice.

### Loophole 10: Same-value updates churn timestamps and confuse audit

Resolution: this plan guarantees same-value writes do not create mutation rows. Avoiding timestamp churn is a separate write-contract cleanup because changing that behavior can affect existing API expectations.

### Loophole 11: Real caller source is lost

Resolution: public deal write methods accept optional `DealWriteContext`. Legacy callers still default to `source="api"`, but agents, bot forms, imports, and future workflows can pass their true source into mutation events.

### Loophole 12: Participant side effects are mistaken for deal mutation events

Resolution: this plan records only deal snapshot changes. Participant/stakeholder row mutations are deferred to a participant mutation plan and must not be emitted as part of this deal-state slice.

### Loophole 13: Custom-field nulls are interpreted as deletes

Resolution: custom-field nulls are explicit stored null values. Key deletion is out of scope until the custom-field API exposes a clear/delete operation.

### Loophole 14: Relationship triggers fire on deal creation

Resolution: account/contact snapshots on create remain part of `crm.deal.created`; `crm.deal.account_changed` and `crm.deal.primary_contact_changed` are reserved for public relationship update actions.

### Loophole 15: Idempotency support is overclaimed

Resolution: this phase aligns the internal deal event service with scoped idempotency. It does not promise end-user retry safety until API/agent caller contracts expose and pass idempotency keys.

## Success Criteria

- Deal events use the shared CRM mutation recorder.
- Deal idempotency follows the same scoped behavior as contacts/accounts.
- Deal changed fields are JSON-safe.
- Deal create emits only `crm.deal.created` semantic event.
- Deal update semantics are curated and not broad.
- Stage, owner, lifecycle, account, primary-contact, and delete actions each emit at most one event.
- Deal mutation events preserve non-API caller sources when a context is supplied.
- Deal participant/stakeholder side effects do not emit standalone participant mutation events in this slice.
- Same-value writes and lifecycle no-ops do not insert mutation rows.
- Custom-field nulls are treated as stored nulls, not key deletion.
- No workflow dispatch/runtime state is added.
- Provider sync writers are untouched.
- Focused deal mutation and existing deal regression tests pass.

## Recommended Execution Order

1. Add `DealMutationPolicy`.
2. Migrate `DealMutationEventService` to the shared recorder.
3. Replace broad semantic derivation in `DealWriteService`.
4. Expand and verify create/update changed-field coverage.
5. Preserve caller source/context.
6. Tighten lifecycle, relationship, and delete tests.
7. Run focused regression tests.

Do not start workflow dispatcher work until this deal mutation phase is green.
