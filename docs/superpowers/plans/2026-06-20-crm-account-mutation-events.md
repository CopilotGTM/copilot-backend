# CRM Account Mutation Events Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add production-grade CRM mutation events for account create/update writes after the shared mutation foundation and contact mutation events are already in place.

**Architecture:** `AccountWriteService` remains the canonical writer for accounts. It computes account-specific field diffs after normalization and duplicate checks, then records one immutable row in `crm_mutation_events` through `CRMMutationEventService`. Workflow runtime is still out of scope; this plan only creates durable audit/workflow-trigger source events.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy async sessions, PostgreSQL `crm_mutation_events`, pytest, Ruff/Mypy at PR time.

---

## Current Repo Facts

The shared mutation foundation already exists:

- `src/service/crm/mutations/contracts.py`
- `src/service/crm/mutations/event_service.py`
- `src/service/crm/mutations/json_safe.py`
- `src/service/crm/mutations/policies.py`
- `tests/service/crm/test_crm_mutation_event_service.py`

Contact mutation events already exist and should be used as the implementation pattern:

- `src/service/crm/contacts/write_service.py`
- `tests/service/crm/test_contact_mutation_events.py`

Account write behavior is now centralized here:

- `src/service/crm/accounts/write_service.py`
- `src/service/crm/accounts/contracts.py`
- `tests/service/crm/test_account_write_service.py`

Account API writes already call the canonical writer:

- `src/api/routers/crm/accounts_handler.py`

The broader modular mutation plan is here, but its account phase is still incomplete:

- `docs/superpowers/plans/2026-06-20-crm-modular-mutation-events-implementation.md`

## Product Decision

Account mutation events should be added now, before deal mutation events.

Reason:

- Account writes already have a clean vertical service boundary.
- Account create is an important workflow trigger for agentic CRM use cases such as account research, enrichment, assignment, or account-plan generation.
- Account update has fewer high-value trigger fields than deal update, so it is a good second entity after contacts.
- Deals have the highest blast radius because stage, lifecycle, owner, account link, contact link, custom fields, and participant projections all interact.

## Event Semantics

Use one audit event type per account action:

| Account action | `event_type` | Meaning |
| --- | --- | --- |
| Create account | `crm.account.created` | A new tenant-owned account row was created. |
| Update account | `crm.account.updated` | One existing tenant-owned account row changed. |

Use semantic event types only for workflow-matchable business changes:

| Condition | Semantic event |
| --- | --- |
| Account created | `crm.account.created` |
| `domain` changed from null/value to a different value | `crm.account.domain_changed` |
| `owner_id` changed | `crm.account.owner_changed` |
| `lifecycle_stage` changed | `crm.account.lifecycle_stage_changed` |

Do not make broad `crm.account.updated` a workflow semantic event in V1.

Reason: broad update triggers become noisy. Editing industry, phone, city, description, LinkedIn URL, or enrichment should be audit-visible, but should not start user workflows unless product later exposes specific field-trigger configuration.

## Audit Field Contract

Record account fields that are useful for history/debugging and safe to store in JSONB:

```text
name
domain
website
description
industry
company_type
lifecycle_stage
owner_id
owner_name
owner_email
number_of_employees
annual_revenue
phone
city
state
country
address
postal_code
timezone
linkedin_url
```

Do not record:

```text
properties
metrics
credentials
tokens
provider raw payloads
OAuth/auth fields
```

`properties` and `metrics` are intentionally excluded from account mutation V1 because they can become large provider/enrichment dumping grounds. If account research later needs eventing, add explicit account enrichment columns or a curated `account_research` payload policy instead of storing arbitrary JSON changes in the mutation ledger.

## Create Flow

Direct API/agent account create should flow like this:

```text
API/agent/tool request
  -> AccountWriteService.create_account()
  -> validate at least name/domain/website
  -> normalize domain from domain or website
  -> tenant-scoped duplicate check
  -> get/create internal CRM integration
  -> insert CRMCompany
  -> flush/refresh
  -> AccountMutationPolicy.build_create_changed_fields(company)
  -> CRMMutationEventService.record_event()
  -> caller commits
```

Example mutation event:

```json
{
  "entity_type": "account",
  "entity_id": "account_123",
  "event_type": "crm.account.created",
  "operation": "create_account",
  "semantic_event_types": ["crm.account.created"],
  "changed_fields": {
    "name": {"old": null, "new": "Acme"},
    "domain": {"old": null, "new": "acme.com"},
    "website": {"old": null, "new": "https://www.acme.com"},
    "linkedin_url": {"old": null, "new": "https://www.linkedin.com/company/acme"}
  },
  "source": "api"
}
```

Rules:

- Duplicate-domain conflict creates no event.
- Duplicate-name conflict creates no event.
- Domain is recorded after normalization.
- Unknown ignored input fields create no event.
- The mutation event is in the same transaction as the account insert.
- If the caller rolls back, both account row and mutation event roll back.

## Update Flow

Direct API/agent account update should flow like this:

```text
API/agent/tool request
  -> AccountWriteService.update_account()
  -> tenant-scoped active-account load
  -> normalize candidate domain when domain/website changes
  -> tenant-scoped duplicate-domain check
  -> capture old values for audit-safe fields
  -> apply allowed fields only
  -> derive domain from website when domain was omitted
  -> flush/refresh
  -> AccountMutationPolicy.changed_field(old, final)
  -> AccountMutationPolicy.semantic_for_update(changed_fields)
  -> CRMMutationEventService.record_event()
  -> caller commits
```

Example update where one triggerable field and two audit-only fields changed:

```json
{
  "entity_type": "account",
  "entity_id": "account_123",
  "event_type": "crm.account.updated",
  "operation": "update_account",
  "semantic_event_types": ["crm.account.domain_changed"],
  "changed_fields": {
    "domain": {"old": "old-acme.com", "new": "acme.com"},
    "website": {"old": "https://old-acme.com", "new": "https://www.acme.com"},
    "industry": {"old": "Services", "new": "Software"}
  },
  "source": "agent_tool"
}
```

Rules:

- Same-value update creates no event.
- Unknown ignored fields create no event.
- Duplicate-domain conflict creates no event.
- Explicit null clears are recorded for allowed nullable fields.
- Website-derived domain changes record the final model value.
- `owner_name` or `owner_email` changes alone are audit-only.
- `owner_id` change emits `crm.account.owner_changed`.
- `lifecycle_stage` change emits `crm.account.lifecycle_stage_changed`.
- `linkedin_url` change is audit-only in V1.

## Deal Materialization Rule

`AccountWriteService.upsert_company_from_account()` is used by deal writes to resolve or materialize an account from a deal payload.

Do not wire mutation events into this helper in the first account mutation slice.

Reason:

- This helper is currently part of deal orchestration, not a standalone account UI/tool action.
- Deal creation/update still needs a broader multi-entity mutation-group design.
- If this helper emits now without group propagation, one deal create could produce account/contact/deal events that are technically correct but operationally hard to explain.
- The safer sequence is direct account create/update first, then deal mutation work decides when materialized account/contact events should be emitted and how they share one `mutation_group_id`.

Later deal work may choose either:

1. Emit account/contact/deal events together with one shared `mutation_group_id`.
2. Emit only the deal event for deal creation and treat account/contact materialization as supporting state.

The recommended future choice is option 1 when a genuinely new account/contact row is created, because real entity creation should be auditable. But it should be implemented inside the deal mutation phase, not accidentally inside this account phase.

## Source and Context Mapping

Extend `AccountWriteContext` only if needed. Current fields are enough for this phase:

```text
source
actor_user_id
source_label
created_via
duplicate_scope
```

Map to mutation context:

```text
source -> CRMMutationContext.source
actor_user_id -> CRMMutationContext.actor_user_id
source_label -> CRMMutationContext.source_label
```

Do not add workflow IDs to account mutation events. Workflow-caused account writes can later use `source="workflow"` plus future workflow lineage fields/tables, but workflow dispatch/runtime state does not belong in the parent mutation row.

## Idempotency

The shared mutation service already supports scoped idempotency, but `AccountWriteContext` does not currently expose `idempotency_key`.

Do not add idempotency to account context in this slice unless an existing account API/tool request already carries an idempotency key.

Reason:

- Adding idempotency at the service context without API/tool support creates unused plumbing.
- The mutation service can already accept idempotency once a caller-level idempotency key exists.
- We should not invent public idempotency behavior inside an internal event slice.

Use event scopes when idempotency is later wired:

```text
account:create:<company_id>
account:update:<company_id>
```

## Implementation Tasks

### Task 1: Account Mutation Policy

**Files:**

- Modify: `src/service/crm/mutations/policies.py`
- Test: `tests/service/crm/test_account_mutation_events.py`

- [ ] Add failing tests for account create changed fields.
- [ ] Add failing tests for audit-only update fields.
- [ ] Add failing tests for semantic update fields.
- [ ] Implement `AccountMutationPolicy`.
- [ ] Verify same-value fields return no change.

Expected policy shape:

```python
from typing import Any

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.mutations.json_safe import changed_field


class AccountMutationPolicy:
    CREATE_AUDIT_FIELDS = (
        "name",
        "domain",
        "website",
        "description",
        "industry",
        "company_type",
        "lifecycle_stage",
        "owner_id",
        "owner_name",
        "owner_email",
        "number_of_employees",
        "annual_revenue",
        "phone",
        "city",
        "state",
        "country",
        "address",
        "postal_code",
        "timezone",
        "linkedin_url",
    )
    UPDATE_AUDIT_FIELDS = CREATE_AUDIT_FIELDS

    @classmethod
    def build_create_changed_fields(cls, account: CRMCompany) -> dict[str, dict[str, Any]]:
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name in cls.CREATE_AUDIT_FIELDS:
            value = getattr(account, field_name, None)
            if value is not None:
                changed_fields[field_name] = {"old": None, "new": value}
        return changed_fields

    @staticmethod
    def semantic_for_create() -> list[str]:
        return ["crm.account.created"]

    @classmethod
    def changed_field(cls, field_name: str, old: Any, new: Any) -> dict[str, Any] | None:
        if field_name not in cls.UPDATE_AUDIT_FIELDS:
            return None
        return changed_field(old, new)

    @staticmethod
    def semantic_for_update(changed_fields: dict[str, dict[str, Any]]) -> list[str]:
        semantic_event_types: list[str] = []
        if "domain" in changed_fields:
            semantic_event_types.append("crm.account.domain_changed")
        if "owner_id" in changed_fields:
            semantic_event_types.append("crm.account.owner_changed")
        if "lifecycle_stage" in changed_fields:
            semantic_event_types.append("crm.account.lifecycle_stage_changed")
        return semantic_event_types
```

### Task 2: Account Create Event

**Files:**

- Modify: `src/service/crm/accounts/write_service.py`
- Test: `tests/service/crm/test_account_mutation_events.py`

- [ ] Add failing test: create account records one `crm.account.created` event.
- [ ] Add failing test: normalized domain is recorded, not raw domain.
- [ ] Add failing test: duplicate domain creates no mutation event.
- [ ] Add failing test: duplicate name creates no mutation event.
- [ ] Wire `AccountWriteService` to `CRMMutationEventService`.
- [ ] Add `_record_account_create_event()`.
- [ ] Convert `CRMMutationEventServiceError` into `AccountWriteServiceError`.

### Task 3: Account Update Event

**Files:**

- Modify: `src/service/crm/accounts/write_service.py`
- Test: `tests/service/crm/test_account_mutation_events.py`

- [ ] Add failing test: profile update records `crm.account.updated` with no semantic events.
- [ ] Add failing test: domain change emits `crm.account.domain_changed`.
- [ ] Add failing test: website-derived domain change records both real changes when both values change.
- [ ] Add failing test: owner ID change emits `crm.account.owner_changed`.
- [ ] Add failing test: owner name/email only is audit-only.
- [ ] Add failing test: lifecycle stage change emits `crm.account.lifecycle_stage_changed`.
- [ ] Add failing test: same-value update records no event.
- [ ] Add failing test: duplicate-domain conflict records no event.
- [ ] Capture old audit-safe field values before applying updates.
- [ ] Build changed fields from final normalized model values after update.
- [ ] Add `_record_account_update_event()`.

### Task 4: Caller Regression Tests

**Files:**

- Modify: `tests/api/routers/crm/test_accounts_handler.py` if needed.
- Modify: `tests/agents/tools/crm/test_create_contact_account_tools.py` if needed.

- [ ] Verify API create path still passes `AccountWriteContext(source="api")`.
- [ ] Verify API update path still passes `AccountWriteContext(source="api")`.
- [ ] Verify create-account agent tool passes `source="agent_tool"`.
- [ ] Do not add assertions that require API response shape changes.

### Task 5: Focused Verification

Run focused tests:

```bash
uv run pytest tests/service/crm/test_account_mutation_events.py -q
uv run pytest tests/service/crm/test_account_write_service.py -q
uv run pytest tests/api/routers/crm/test_accounts_handler.py -q
uv run pytest tests/agents/tools/crm/test_create_contact_account_tools.py -q
```

Before commit/PR, run focused Ruff and Mypy:

```bash
uv run ruff check src/service/crm/accounts src/service/crm/mutations tests/service/crm/test_account_mutation_events.py tests/service/crm/test_account_write_service.py
uv run mypy --no-incremental src/service/crm/accounts src/service/crm/mutations
```

## Loophole Review

### Loophole 1: Broad account update becomes a noisy workflow trigger

Resolution: `event_type="crm.account.updated"` is audit envelope only. Workflow matching uses curated `semantic_event_types`, not the broad event type.

### Loophole 2: Raw domain input is recorded instead of normalized state

Resolution: record changed fields only after `AccountWriteService` normalizes domain and applies final model values.

### Loophole 3: Website update silently changes domain but audit misses it

Resolution: when `website` derives a new domain, compare both old and final `website` and old and final `domain`.

### Loophole 4: Same-value writes pollute account history

Resolution: `AccountMutationPolicy.changed_field()` returns `None` for equal normalized old/new values. If no changed fields remain, `CRMMutationEventService.record_event()` returns no event.

### Loophole 5: Duplicate conflicts create false mutation events

Resolution: event recording happens only after duplicate checks pass and after the company row is flushed/refreshed.

### Loophole 6: Deal creation accidentally emits ungrouped account events

Resolution: do not wire `upsert_company_from_account()` in this slice. Deal mutation work later decides grouped multi-entity emission.

### Loophole 7: Account research/enrichment JSON floods the ledger

Resolution: exclude `properties` and `metrics` from account mutation V1. Add curated enrichment fields later if product wants account-research workflow triggers.

### Loophole 8: Owner display changes trigger owner workflows incorrectly

Resolution: only `owner_id` emits `crm.account.owner_changed`. `owner_name` and `owner_email` are audit-only unless paired with `owner_id`.

### Loophole 9: Workflow runtime state leaks into account mutation rows

Resolution: do not add workflow run IDs, dispatch status, retry counters, or worker claim fields to account mutation events.

### Loophole 10: Provider sync volume overwhelms CRM mutation events

Resolution: provider sync account writes remain out of this phase. Provider-origin mutation events require a separate coalescing and opt-in workflow-trigger design.

## Success Criteria

- Direct account create emits one `crm.account.created` mutation event.
- Direct account update emits one `crm.account.updated` mutation event when real audit-safe fields changed.
- Same-value account update emits no event.
- Duplicate-domain and duplicate-name conflicts emit no event.
- Domain normalization is reflected in mutation payloads.
- `semantic_event_types` contains only curated account workflow triggers.
- Deal account materialization remains unchanged in this phase.
- Focused account mutation, account writer, API account, and create-account agent tests pass.

## Recommended Next Step

Implement this account phase with TDD:

1. Write `tests/service/crm/test_account_mutation_events.py`.
2. Add `AccountMutationPolicy`.
3. Wire direct `create_account()` event recording.
4. Wire direct `update_account()` event recording.
5. Run focused tests.

Do not touch deal mutation code until this phase is green.
