# CRM Modular Mutation Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement production-grade CRM mutation events for contacts, accounts, and deals through one shared mutation-event foundation and entity-specific write policies.

**Architecture:** `crm_mutation_events` is the shared CRM state-change ledger and future workflow outbox. Contacts, accounts, and deals keep separate write services and separate semantic policies, but all write one event shape through `src/service/crm/mutations/`. Workflow dispatch is not implemented in this plan; mutation events only make dispatch durable and recoverable later.

**Tech Stack:** Python 3.11, FastAPI service layer, SQLModel/SQLAlchemy async sessions, PostgreSQL JSONB/ARRAY indexes, pytest, Ruff/Mypy at PR time.

---

## Context

The original mutation contract was written in:

- `docs/superpowers/plans/2026-06-16-crm-write-mutation-contract.md`
- `docs/superpowers/plans/2026-06-17-crm-mutation-workflow-runtime-flow.md`

Those plans were created before the CRM service split. The repo now has modular write services:

- `src/service/crm/contacts/write_service.py`
- `src/service/crm/accounts/write_service.py`
- `src/service/crm/deals/write_service.py`
- `src/service/crm/deals/mutation_event_service.py`

The old stashed implementation should not be applied directly because it was written against older `src/service/crm/internal/*` boundaries. The correct path is to keep the old plans as design references and implement the mutation layer against the current modular services.

The table already exists through:

- `migrations/org_migrations/versions/3c7f79637737_062_add_crm_mutation_events.py`
- `src/domain/models/organisation_tables/sync/crm/deals.py::CRMDealMutationEvent`

## Product Decision

Use one shared `crm_mutation_events` table for CRM state mutations across contacts, accounts, and deals.

Do not create separate tables like `contact_mutation_events`, `account_mutation_events`, and `deal_mutation_events` right now.

Reason:

- The user-facing product needs one CRM history and workflow-trigger source.
- Contacts, accounts, and deals are all CRM state entities, not unrelated domains like emails or meetings.
- Separate tables would duplicate idempotency, payload hashing, source metadata, and audit behavior.
- One table remains production-safe because `entity_type`, `entity_id`, `deal_id`, `event_type`, and `semantic_event_types` preserve entity boundaries.

The production guardrail is not separate tables. The guardrail is separate policy classes:

- `ContactMutationPolicy`
- `AccountMutationPolicy`
- `DealMutationPolicy`

Each policy decides:

- which fields are audit-safe
- which fields are triggerable
- how summaries are written
- which event type is emitted
- which same-value writes are ignored

## Non-Goals

This plan does not implement workflow dispatch workers, workflow definitions, workflow runs, or workflow step execution.

This plan does not add mutation events for provider sync writes in Phase 1. Provider sync can create high event volume and needs separate coalescing and opt-in workflow rules.

This plan does not refactor notes, tasks, meetings, emails, risks, blockers, or participants into mutation events. Those remain separate activity/domain ledgers unless later product requirements say they should become CRM state mutations.

This plan does not change API response shapes unless a response already returns a write result object internally. Mutation events are backend audit/outbox records first.

## Core Invariants

1. One user/API/agent/workflow business action creates at most one `crm_mutation_events` row per entity changed.
2. One mutation row may contain many changed fields.
3. A same-value write creates no mutation event.
4. A create event uses `old=null` and `new=<initial value>` for auditable initial fields.
5. A contact/account/deal update records all real audit-safe changed fields, even if none are workflow-triggerable.
6. `semantic_event_types` contains only workflow-matchable business semantics.
7. Workflow runtime state is never stored on `crm_mutation_events`.
8. The service writes the mutation row in the same DB transaction as the CRM state change.
9. Callers still own commit/rollback unless the existing service boundary says otherwise.
10. Every query and write remains tenant-scoped by `organisation_id`.
11. A multi-entity action reuses one `mutation_group_id` across all entity mutation rows created by that action.
12. Stored idempotency keys are event-scoped, not raw request-scoped, so one request can safely create contact, account, and deal mutation rows in the same transaction.

## Event Type Contract

Action-level event types:

| Entity | Action | `event_type` |
| --- | --- | --- |
| Contact | Create | `crm.contact.created` |
| Contact | Update | `crm.contact.updated` |
| Account | Create | `crm.account.created` |
| Account | Update | `crm.account.updated` |
| Deal | Create | `crm.deal.created` |
| Deal | Multi-field update | `crm.deal.updated` |
| Deal | Stage-only move | `crm.deal.stage_changed` |
| Deal | Owner-only assignment | `crm.deal.owner_changed` |
| Deal | Close won/lost | `crm.deal.closed` |
| Deal | Reopen | `crm.deal.reopened` |
| Deal | Delete | `crm.deal.deleted` |

Semantic event types:

| Entity | Changed field or action | Semantic event |
| --- | --- | --- |
| Contact | Created | `crm.contact.created` |
| Contact | Email added on previously email-less contact | `crm.contact.email_added` |
| Contact | Account link created | `crm.contact.account_linked` |
| Account | Created | `crm.account.created` |
| Account | Owner changed | `crm.account.owner_changed` |
| Account | Lifecycle stage changed | `crm.account.lifecycle_stage_changed` |
| Deal | Created | `crm.deal.created` |
| Deal | `stage_id` changed | `crm.deal.stage_changed` |
| Deal | `owner_id` changed | `crm.deal.owner_changed` |
| Deal | `amount` changed | `crm.deal.amount_changed` |
| Deal | `deal_status` changed by close | `crm.deal.closed` |
| Deal | `deal_status` changed by reopen | `crm.deal.reopened` |
| Deal | Soft deleted | `crm.deal.deleted` |

Non-trigger fields still go into `changed_fields` when they really changed. They do not create semantic events.

## Shared Module Structure

Create:

```text
src/service/crm/mutations/
  __init__.py
  contracts.py
  event_service.py
  json_safe.py
  policies.py
```

Responsibilities:

| File | Responsibility |
| --- | --- |
| `contracts.py` | Shared mutation context, changed-field type, entity literals, and result object. |
| `json_safe.py` | Convert UUID, datetime, Decimal, enum, lists, and dictionaries into JSON-safe values with length/redaction rules. |
| `policies.py` | Shared helpers plus contact/account/deal policy classes. |
| `event_service.py` | Insert `CRMDealMutationEvent` rows with idempotency and payload-hash checks. |

The model class may remain named `CRMDealMutationEvent` in this phase because it already maps the correct table. The service module should hide that naming wart from entity services by exporting a generic `CRMMutationEventService`.

Do not move the model class in the same slice unless tests prove imports are clean. Moving the model is a cleanup task after the mutation behavior is stable.

## Shared Contracts

`src/service/crm/mutations/contracts.py` should define:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

CRMEntityType = Literal["contact", "account", "deal"]
CRMMutationSource = Literal[
    "api",
    "agent",
    "workflow",
    "bot_form",
    "import_assistant",
    "provider_sync",
]


@dataclass(frozen=True)
class CRMFieldChange:
    old: Any
    new: Any


@dataclass(frozen=True)
class CRMMutationContext:
    source: CRMMutationSource
    actor_user_id: UUID | None = None
    source_label: str | None = None
    approval_state_id: UUID | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    mutation_group_id: UUID | None = None


@dataclass(frozen=True)
class CRMMutationRecordCommand:
    entity_type: CRMEntityType
    entity_id: UUID
    event_type: str
    operation: str
    changed_fields: dict[str, dict[str, Any]]
    semantic_event_types: list[str]
    context: CRMMutationContext
    summary: str | None = None
    deal_id: UUID | None = None
    integration_id: UUID | None = None
    provider: str | None = None
    source_object_mapping_id: UUID | None = None
    idempotency_scope: str | None = None


@dataclass(frozen=True)
class CRMMutationRecordResult:
    event_id: UUID | None
    changed: bool
    changed_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic_event_types: list[str] = field(default_factory=list)
```

## JSON-Safe Rules

`changed_fields` must be JSON-safe and audit-safe.

Allowed conversions:

| Input | Stored value |
| --- | --- |
| `UUID` | string |
| `datetime` | ISO 8601 string |
| `date` | ISO 8601 string |
| `Decimal` | string |
| enum | enum value |
| list | recursively normalized list |
| dict | recursively normalized dict with string keys |

Safety rules:

- Store only canonical field names.
- Do not store passwords, tokens, OAuth secrets, API keys, or auth headers.
- Truncate very long strings to a fixed maximum and mark truncation.
- Drop unknown unsupported object instances by converting to string only when the policy explicitly allows it.
- Preserve explicit null clears as `{old: value, new: null}`.
- Omitted fields are not recorded.

## Idempotency and Mutation Grouping Rules

The table has a tenant-scoped unique index on `(organisation_id, idempotency_key)` when `idempotency_key` is not null. That is correct for database enforcement, but the shared service must not store the raw API idempotency key directly when one request can create multiple entity mutation rows.

Rules:

- `CRMMutationContext.idempotency_key` is the caller/request retry key.
- `CRMMutationRecordCommand.idempotency_scope` identifies the event within that request, for example `contact:create:<contact_id>` or `deal:update:<deal_id>`.
- The stored `CRMDealMutationEvent.idempotency_key` is a deterministic scoped value derived from both pieces.
- The derived value must be stable, tenant-local, and at most 128 characters.
- If no caller idempotency key is provided, the stored `idempotency_key` stays null.
- `payload_hash` must include `entity_type`, `entity_id`, `event_type`, `operation`, `changed_fields`, `semantic_event_types`, `source`, and the scoped idempotency value.
- Same scoped idempotency key plus same payload returns the existing row.
- Same scoped idempotency key plus different payload raises a conflict.

Example:

```text
Request idempotency key: req_123
Mutation group id:       grp_abc

Stored event 1:
  entity_type = account
  idempotency_scope = account:create:account_456
  idempotency_key = hash(req_123 + account:create:account_456)
  mutation_group_id = grp_abc

Stored event 2:
  entity_type = contact
  idempotency_scope = contact:create:contact_789
  idempotency_key = hash(req_123 + contact:create:contact_789)
  mutation_group_id = grp_abc
```

This preserves safe retries while still allowing one business action to mutate multiple CRM entities.

## Contact Mutation Plan

Contacts go first because their write surface is smaller than accounts and deals.

### Contact Create

Current entry point:

- `src/service/crm/contacts/write_service.py::ContactWriteService.create_contact`

Expected mutation:

```json
{
  "event_type": "crm.contact.created",
  "entity_type": "contact",
  "operation": "create",
  "semantic_event_types": ["crm.contact.created"],
  "changed_fields": {
    "email": {"old": null, "new": "buyer@example.com"},
    "full_name": {"old": null, "new": "Asha Rao"},
    "company_id": {"old": null, "new": "company_uuid"}
  }
}
```

Rules:

- Duplicate-contact conflict creates no mutation event.
- The mutation row is inserted after the contact has a `contact_id`.
- `company_id` is included only when explicitly linked.
- `company_name` can be audit-only if it changes, but it should not create a workflow trigger.

### Contact Update

Current entry point:

- `src/service/crm/contacts/write_service.py::ContactWriteService.update_contact`

Rules:

- Same-value updates create no mutation event.
- Email update is only allowed when the existing contact has no email; if allowed, semantic event is `crm.contact.email_added`.
- Account link creation from `company_id=null` to a value emits `crm.contact.account_linked`.
- Attempted illegal account relink still raises and creates no event.
- Unknown ignored fields create no event.

## Account Mutation Plan

Accounts go second because duplicate handling and domain normalization need to be proven after contact event utilities exist.

### Account Create

Current entry point:

- `src/service/crm/accounts/write_service.py::AccountWriteService.create_account`

Expected mutation:

```json
{
  "event_type": "crm.account.created",
  "entity_type": "account",
  "operation": "create",
  "semantic_event_types": ["crm.account.created"],
  "changed_fields": {
    "name": {"old": null, "new": "Acme"},
    "domain": {"old": null, "new": "acme.com"},
    "industry": {"old": null, "new": "Software"}
  }
}
```

Rules:

- Duplicate-domain or duplicate-name conflict creates no mutation event.
- Domain is recorded after normalization, not as raw input.
- Unknown ignored fields create no event.

### Account Update

Current entry point:

- `src/service/crm/accounts/write_service.py::AccountWriteService.update_account`

Rules:

- Same-value updates create no mutation event.
- Updating `website` may derive `domain`; record the field that actually changed on the model.
- Duplicate-domain conflict creates no event.
- `owner_id` change emits `crm.account.owner_changed`.
- `lifecycle_stage` change emits `crm.account.lifecycle_stage_changed`.
- Display-only owner snapshot changes such as `owner_name` and `owner_email` are audit-only unless `owner_id` changed.

## Deal Mutation Plan

Deals go last because they combine scalar fields, custom fields, stage history, owner projection, account/contact relationship snapshots, lifecycle actions, and delete behavior.

Current entry points:

- `src/service/crm/deals/write_service.py::DealWriteService.create_deal`
- `src/service/crm/deals/write_service.py::DealWriteService.update_deal`
- `src/service/crm/deals/write_service.py::DealWriteService.move_stage`
- `src/service/crm/deals/write_service.py::DealWriteService.assign_owner`
- `src/service/crm/deals/write_service.py::DealWriteService.close_deal`
- `src/service/crm/deals/write_service.py::DealWriteService.reopen_deal`
- `src/service/crm/deals/write_service.py::DealWriteService.update_account`
- `src/service/crm/deals/write_service.py::DealWriteService.update_primary_contact`
- `src/service/crm/deals/write_service.py::DealWriteService.delete_deal`

Rules:

- One public write method emits at most one event.
- Sub-services return changed fields; the public orchestrator records the event.
- Stage history is still written for real stage moves.
- Creation uses `crm.deal.created`, not `crm.deal.stage_changed` from null.
- Multi-field update uses `crm.deal.updated` with semantic types derived from changed fields.
- Lifecycle close uses `crm.deal.closed`; if stage moved as part of close, include `crm.deal.stage_changed` only when product wants stage workflows to run on close.
- Delete uses `crm.deal.deleted` only. It should not also emit `crm.deal.status_changed`.
- Account/contact snapshot updates are audit-only unless product explicitly adds workflow triggers.

## Implementation Tasks

### Task 1: Shared Mutation Foundation

**Files:**

- Create: `src/service/crm/mutations/__init__.py`
- Create: `src/service/crm/mutations/contracts.py`
- Create: `src/service/crm/mutations/json_safe.py`
- Create: `src/service/crm/mutations/event_service.py`
- Test: `tests/service/crm/test_crm_mutation_event_service.py`

- [x] **Step 1: Add tests for JSON-safe field normalization**

Run:

```bash
uv run pytest tests/service/crm/test_crm_mutation_event_service.py -q
```

Expected before implementation:

```text
FAILED tests/service/crm/test_crm_mutation_event_service.py::test_json_safe_values_are_normalized
```

- [x] **Step 2: Implement `json_safe.py`**

Include support for UUID, datetime, date, Decimal, enum, list, dict, booleans, numbers, strings, and nulls.

- [x] **Step 3: Add tests for event idempotency**

Test cases:

- same `idempotency_key` and same payload returns the existing event
- same scoped `idempotency_key` and different payload raises 409-style service error
- same request `idempotency_key` can create two events when `idempotency_scope` differs
- same `mutation_group_id` is preserved on multiple events from one request
- no changed fields returns `event_id=None`

- [x] **Step 4: Implement `CRMMutationEventService.record_event()`**

The service inserts `CRMDealMutationEvent` while exposing a generic CRM mutation interface.

- [x] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/service/crm/test_crm_mutation_event_service.py -q
```

Expected:

```text
passed
```

### Task 2: Contact Mutation Events

**Files:**

- Modify: `src/service/crm/contacts/contracts.py`
- Modify: `src/service/crm/contacts/write_service.py`
- Create: `src/service/crm/mutations/policies.py`
- Test: `tests/service/crm/test_contact_mutation_events.py`

- [x] **Step 1: Add contact create event tests**

Required assertions:

- create contact adds one `CRMDealMutationEvent`
- `entity_type == "contact"`
- `event_type == "crm.contact.created"`
- `semantic_event_types == ["crm.contact.created"]`
- duplicate email creates no event

- [x] **Step 2: Add contact update event tests**

Required assertions:

- same-value update creates no event
- email added to email-less contact emits `crm.contact.email_added`
- account link emits `crm.contact.account_linked`
- illegal account relink creates no event
- unknown ignored fields create no event

- [x] **Step 3: Implement `ContactMutationPolicy`**

The policy should derive create/update changed fields after service normalization, not from raw request payload.

- [x] **Step 4: Wire `ContactWriteService` to `CRMMutationEventService`**

Use the existing `ContactWriteContext` and map it to `CRMMutationContext`. The write service still does not commit.

- [x] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/service/crm/test_contact_mutation_events.py -q
```

Expected:

```text
passed
```

### Task 3: Account Mutation Events

**Files:**

- Modify: `src/service/crm/accounts/contracts.py`
- Modify: `src/service/crm/accounts/write_service.py`
- Modify: `src/service/crm/mutations/policies.py`
- Test: `tests/service/crm/test_account_mutation_events.py`

- [ ] **Step 1: Add account create event tests**

Required assertions:

- create account adds one mutation event
- `entity_type == "account"`
- `event_type == "crm.account.created"`
- normalized domain is recorded
- duplicate domain creates no event

- [ ] **Step 2: Add account update event tests**

Required assertions:

- same-value update creates no event
- website-derived domain changes record the final model value
- owner ID change emits `crm.account.owner_changed`
- lifecycle stage change emits `crm.account.lifecycle_stage_changed`
- owner display snapshot only is audit-only
- duplicate domain conflict creates no event

- [ ] **Step 3: Implement `AccountMutationPolicy`**

The policy should compare persisted old values against final normalized model values.

- [ ] **Step 4: Wire `AccountWriteService` to `CRMMutationEventService`**

Keep commit ownership unchanged. Do not emit events from read service or access facade.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/service/crm/test_account_mutation_events.py -q
```

Expected:

```text
passed
```

### Task 4: Deal Mutation Events On Current Modular Services

**Files:**

- Modify: `src/service/crm/deals/mutation_event_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Modify: `src/service/crm/deals/stage_service.py`
- Modify: `src/service/crm/deals/owner_service.py`
- Modify: `src/service/crm/deals/lifecycle_service.py`
- Modify: `src/service/crm/mutations/policies.py`
- Test: `tests/service/crm/test_deal_mutation_events.py`

- [ ] **Step 1: Add deal create event tests**

Required assertions:

- deal create emits `crm.deal.created`
- initial stage is recorded in `changed_fields`
- initial stage does not emit `crm.deal.stage_changed`
- duplicate or failed create creates no event

- [ ] **Step 2: Add deal multi-field update tests**

Required assertions:

- one API-style update with five changed fields emits one event
- `event_type == "crm.deal.updated"`
- `changed_fields` includes all real changes
- `semantic_event_types` includes only triggerable fields
- omitted fields are not recorded
- explicit null clears are recorded

- [ ] **Step 3: Add stage, owner, lifecycle, delete tests**

Required assertions:

- same-stage move creates no event and no stage history
- real stage move creates one event and one stage history row
- same-owner assignment creates no event
- owner change creates one `crm.deal.owner_changed` event
- close creates one `crm.deal.closed` event
- reopen creates one `crm.deal.reopened` event
- delete creates one `crm.deal.deleted` event and does not include status-change semantics

- [ ] **Step 4: Replace deal-only mutation service with shared event service**

Keep `DealMutationEventService` as a thin compatibility wrapper only if current callers still depend on it.

- [ ] **Step 5: Implement `DealMutationPolicy`**

Centralize semantic derivation for deal fields so API, agent, bot form, and future workflow writes behave the same.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_events.py -q
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
```

Expected:

```text
passed
```

### Task 5: Integration Surface Regression Tests

**Files:**

- Modify or create focused tests under `tests/api/routers/crm/`
- Modify or create focused tests under `tests/agents/tools/crm/`

- [ ] **Step 1: Add API caller regression tests**

Cover:

- contact API create/update path
- account API create/update path
- deal API create/update/stage path

- [ ] **Step 2: Add agent caller regression tests**

Cover:

- create contact tool
- update deal fields tool
- lifecycle or stage tool if current tests already cover it

- [ ] **Step 3: Run focused API/tool tests**

Run:

```bash
uv run pytest tests/api/routers/crm tests/agents/tools/crm -q
```

Expected:

```text
passed for the focused CRM write tests
```

## Loophole Review Pass

### Loophole 1: The shared table becomes a generic dumping ground

Resolution: Keep the shared table, but require entity-specific policies. Do not let callers pass arbitrary `changed_fields` directly to the event service.

### Loophole 2: Create events accidentally trigger update workflows

Resolution: Creation uses creation semantic types only. Initial stage, amount, owner, and account values are recorded as initial values but do not imply update semantics unless product explicitly adds a creation-with-condition workflow.

### Loophole 3: Same-value writes pollute history

Resolution: Every policy compares old persisted values to normalized final values. Same-value writes return no event.

### Loophole 4: Contact/account services record raw payload instead of final model state

Resolution: Policies must run after normalization and duplicate checks. Domain normalization, email normalization, account link resolution, and ignored-field filtering happen before changed fields are built.

### Loophole 5: One idempotency key hides a different request

Resolution: Store and compare `payload_hash`. Same key with different hash raises a conflict.

### Loophole 6: Workflow runtime sneaks into mutation events

Resolution: `crm_mutation_events` stores only mutation facts and semantic types. Workflow run IDs, dispatch claim status, retry count, and per-workflow errors belong in future workflow dispatch tables.

### Loophole 7: Provider sync floods the mutation ledger

Resolution: Provider sync remains out of Phase 1. Later provider-origin events need coalescing, source mapping, and opt-in workflow rules.

### Loophole 8: Deal sub-services emit duplicate events

Resolution: Stage, owner, lifecycle, relationship, and custom-field services return changed-field results. The public deal write orchestrator emits the event once.

### Loophole 9: Contact/account mutations inside deal creation create confusing extra history

Resolution: If deal creation materializes an account or contact as part of the same user action, the account/contact write service may emit its own entity mutation event because a real entity was created. The deal create event still records only the deal snapshot and relationship. These are separate entities changed in one transaction.

### Loophole 10: Audit values leak sensitive data

Resolution: `json_safe.py` and policies include denylisted fields. Entity policies decide allowed audit fields; unsupported fields are ignored for mutation events even if stored elsewhere.

### Loophole 11: Current model class name is deal-specific

Resolution: Use a shared service API now and keep the ORM class location stable in the behavior slice. Move `CRMDealMutationEvent` to a generic model file only after event behavior is green.

### Loophole 12: One request idempotency key blocks multi-entity writes

Resolution: Treat the caller-provided idempotency key as request-level input, then derive a stored event-scoped idempotency key using `idempotency_scope`. This avoids unique-index collisions when one API request creates account, contact, and deal mutation events in one transaction.

### Loophole 13: Multi-entity actions cannot be traced as one user action

Resolution: Add optional `CRMMutationContext.mutation_group_id`. The service uses it when supplied and generates one only when missing. Deal creation that materializes account/contact rows can pass the same group id to all entity mutation events.

## Success Criteria

- Contact create/update writes mutation events for real changes.
- Account create/update writes mutation events for real changes.
- Deal create/update/stage/owner/lifecycle/delete writes mutation events for real changes.
- Same-value writes do not create events.
- Duplicate/conflict writes do not create events.
- Multi-field updates create one event per entity, not one event per field.
- `semantic_event_types` contains only triggerable business semantics.
- Workflow runtime state remains out of `crm_mutation_events`.
- Focused CRM mutation tests pass.

## Recommended Execution Order

1. Shared mutation foundation.
2. Contact mutation events.
3. Account mutation events.
4. Deal mutation events.
5. API and agent caller regression tests.

This order is intentional. Contacts prove the simple policy. Accounts prove normalization and duplicate behavior. Deals come last because they have the highest blast radius.

## Self-Review Loop

### Loop 1: Scope Check

The plan covers shared mutation infrastructure, contacts, accounts, deals, and caller regression tests. It excludes workflow dispatch, provider sync, notes/tasks/meetings, and model-file cleanup because adding them now would widen the slice without improving the immediate CRM write contract.

### Loop 2: Production Edge-Case Check

The plan covers duplicate writes, same-value writes, null clears, ignored fields, normalization, idempotency conflicts, multi-field saves, workflow fanout separation, provider-sync volume, and sensitive field leakage.

### Loop 3: Modularity Check

The plan does not add a monolithic CRM write service. It adds a shared event writer and entity-specific policies, while keeping contacts, accounts, and deals in their current vertical service modules.

### Loop 4: User Behavior Check

The frontend or agent can send one save containing many fields. The backend records the real user action as one mutation per entity and derives only the workflow-relevant semantic events. Users are not forced to split updates field by field.

### Loop 5: Rollout Check

Each phase has focused tests and can be implemented independently. If deal mutation work is delayed, contact/account mutation events can still ship without changing the deal write behavior.
