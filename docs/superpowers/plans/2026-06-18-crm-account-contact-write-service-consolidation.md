# CRM Account Contact Write Service Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate internal CRM account/contact creation and updates into one canonical account writer and one canonical contact writer so frontend, agent, deal write, stakeholder, and discovery flows share the same business rules.

**Architecture:** `AccountWriteService` and `ContactWriteService` become the canonical internal CRM write services for local account/contact rows, introduced behind compatibility adapters first. Access services remain API read/list/detail surfaces and delegate writes after transaction ownership is made explicit. Deal/stakeholder/discovery migration happens in separate phases so one large refactor does not create avoidable regressions.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, pytest, mypy, ruff.

---

## Decision

The production direction is one canonical write service per CRM entity:

- `AccountWriteService` owns internal account create/update/resolve/copy rules, with explicit duplicate scopes.
- `ContactWriteService` owns internal contact create/update/resolve/copy rules, with strict API creation separated from deal/stakeholder provisional contact resolution.
- `AccountsAccessService` and `ContactsAccessService` keep read/list/detail/search API behavior and delegate writes.
- `DealWriteService`, agent tools, stakeholder service, and calendar discovery must not directly create internal account/contact rows.
- HubSpot/Salesforce provider sync writers stay separate because they persist provider snapshots, external mappings, and sync timestamps. They are not internal CRM user/agent write paths.
- Internal CRM integration lookup/creation should not accidentally bootstrap pipelines during account/contact writes. Use an integration-only helper or add `InternalCRMSetupService.ensure_internal_integration(commit=False)` before wiring writers to it.

This fixes the real loophole: today the same logical account/contact can be created through multiple rule sets.

## Tightened Rollout Order

This refactor should not land as one giant patch. The safer production sequence is:

1. **Phase 0: transaction ownership guardrails**
   - Make account/contact API and agent commit ownership explicit in tests first.
   - Add regression tests proving contact-only agent creation persists.
   - Do not move service commits until every caller has an explicit commit.

2. **Phase 1: canonical writers behind adapters**
   - Add `AccountWriteService` and `ContactWriteService`.
   - Keep `AccountMaterializationService` and `ContactMaterializationService` as thin adapters.
   - Migrate their internal logic into the writers without changing `DealWriteService` imports yet.

3. **Phase 2: access services delegate writes**
   - `AccountsAccessService` and `ContactsAccessService` keep public method names.
   - Their create/update methods delegate to canonical writers.
   - Routers own commits.

4. **Phase 3: deal write uses canonical writers**
   - Replace materializer dependencies in `DealWriteService`.
   - Preserve current deal mutation events exactly.

5. **Phase 4: agent tool verification**
   - Keep agent create/reuse behavior.
   - Verify commit and rollback behavior after access services delegate writes.

6. **Phase 5: stakeholder and discovery migration**
   - Migrate direct `CRMContact(...)` creation in stakeholder service.
   - Migrate calendar discovery only after writer contracts prove they support batch inserts without per-row flush.

Each phase must be independently shippable and verified before the next one starts.

## Current Loopholes Found

1. `AccountsAccessService.create_account()` directly creates `CRMCompany`.
2. `AccountMaterializationService` also directly creates `CRMCompany`.
3. `ContactsAccessService.create_contact()` directly creates `CRMContact`.
4. `ContactMaterializationService` also directly creates `CRMContact`.
5. `ContactsAccessService.create_contact()` and `update_contact()` commit internally, while account/deal writers mostly expect caller-owned transactions.
6. `ContactsAccessService.create_account()` delegates through access-service write logic, keeping contact writes coupled to account access.
7. `DealWriteService` depends on materialization services instead of canonical entity writers.
8. `StakeholdersService._ensure_contact_for_stakeholder()` directly creates internal CRM contacts.
9. `CalendarContactAccountDiscoveryService` directly creates discovered internal CRM accounts and contacts.
10. Agent tools rely on access-service write behavior, so changing commit ownership without updating tools would silently lose agent-created contacts/accounts.

## Target Boundaries

### AccountWriteService

Responsible for:

- internal account creation from API, agent, deal write, stakeholder/discovery support
- strict duplicate behavior for explicit account creation
- resolve-or-create behavior for deal/agent enrichment flows
- provider-to-internal account copy when a synced provider account is selected for an internal deal
- account update allowed fields and domain normalization
- internal CRM integration creation/reuse
- org isolation on every lookup
- no commits; only `flush()` and optional `refresh()`

Duplicate scopes:

- `strict_org`: explicit API/account-app creation; duplicate active domain or name anywhere in the org returns 409.
- `internal_only`: internal CRM writes; duplicate check is scoped to the internal CRM integration.
- `resolve_or_create`: deal/agent enrichment; reuse safe matches instead of raising duplicates.

Not responsible for:

- account list/detail/search presentation
- deal participant links
- provider sync persistence
- workflow dispatch

### ContactWriteService

Responsible for:

- internal contact creation from API, agent, deal write, stakeholder, and discovery flows
- strict duplicate behavior for explicit contact creation
- resolve-or-create behavior for deal primary contact and stakeholder flows
- provider-to-internal contact copy when a synced provider contact is selected for an internal deal
- contact update allowed fields, set-once email/account rules, and contact properties writes
- explicit account link validation through `AccountWriteService`
- no commits; only `flush()` and optional `refresh()`

Creation modes:

- `strict`: API/contact-app creation requires email and name, rejects duplicate email.
- `resolve_or_create`: agent/stakeholder/deal enrichment reuses safe matches.
- `primary_contact_resolution`: deal primary-contact path can handle name-only provisional contacts and ambiguous matches without pretending it is normal API contact creation.

Not responsible for:

- contact list/detail/search presentation
- adding a contact to a deal as a participant
- stakeholder behavior fields
- provider sync persistence
- workflow dispatch

## File Responsibilities

- Create `src/service/crm/internal/account_write_service.py`
  - Canonical internal account write API.
  - Moves direct `CRMCompany(...)` creation out of access/materialization paths.

- Create `src/service/crm/internal/contact_write_service.py`
  - Canonical internal contact write API.
  - Moves direct `CRMContact(...)` creation out of access/materialization paths.

- Modify `src/service/crm/internal/account_materialization_service.py`
  - Convert to a compatibility adapter or remove after call sites migrate.
  - It must contain no direct `CRMCompany(...)` construction.

- Modify `src/service/crm/internal/contact_materialization_service.py`
  - Convert to a compatibility adapter or remove after call sites migrate.
  - It must contain no direct `CRMContact(...)` construction.

- Modify `src/service/crm/access/accounts_access_service.py`
  - Keep read/list/detail/search methods.
  - Delegate `create_account()` and `update_account()` to `AccountWriteService`.
  - Remove duplicate domain normalizer or delegate it to shared normalizers.

- Modify `src/service/crm/access/contacts_access_service.py`
  - Keep read/list/detail/search and `add_contact_to_deal()`.
  - Delegate `create_contact()` and `update_contact()` to `ContactWriteService`.
  - Delegate account creation to `AccountWriteService`, not `AccountsAccessService`.
  - Remove internal commits from contact create/update service methods.

- Modify `src/api/routers/crm/accounts_handler.py`
  - Commit after both create and update account writes.

- Modify `src/api/routers/crm/contacts_handler.py`
  - Commit after create and update contact writes because the write service will not commit.

- Modify `src/service/crm/internal/deal_write_service.py`
  - Replace materializer dependencies with canonical writers.
  - Preserve deal mutation events and participant behavior.

- Modify `src/agents/tools/crm/create_account.py`
  - Keep tool response behavior, but rely on canonical write service through access wrapper or direct writer.
  - Ensure agent commits after account creation.

- Modify `src/agents/tools/crm/create_contact.py`
  - Ensure contact-only creation commits after write.
  - Keep deal-link creation in the same transaction before commit.

- Modify `src/service/crm/insights/stakeholders_service.py`
  - Replace direct `CRMContact(...)` creation in `_ensure_contact_for_stakeholder()` with `ContactWriteService`.

- Modify `src/service/crm/discovery/calendar_contact_account_discovery.py`
  - Replace direct `CRMCompany(...)` and `CRMContact(...)` creation with batch-safe writer methods.
  - Keep discovery-specific candidate filtering in discovery service.

- Modify tests under `tests/service/crm/`, `tests/api/routers/crm/`, and `tests/agents/tools/crm/`.

## Core Contracts

### Account Write Types

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

AccountCreateMode = Literal["strict", "resolve_or_create"]
AccountDuplicateScope = Literal["strict_org", "internal_only", "resolve_or_create"]


@dataclass(frozen=True)
class AccountWriteContext:
    source: str
    actor_user_id: UUID | None = None
    source_label: str | None = None
    created_via: str | None = None
    duplicate_scope: AccountDuplicateScope = "internal_only"


@dataclass(frozen=True)
class AccountWriteResult:
    account: CRMCompany
    created: bool
    matched_by: Literal["domain", "name", "explicit_id", "copied_source", "created"]
```

### Contact Write Types

```python
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

ContactCreateMode = Literal["strict", "resolve_or_create", "primary_contact_resolution"]


@dataclass(frozen=True)
class ContactWriteContext:
    source: str
    actor_user_id: UUID | None = None
    source_label: str | None = None
    created_via: str | None = None


@dataclass(frozen=True)
class ContactWriteResult:
    contact: CRMContact
    created: bool
    matched_by: Literal["email", "name", "explicit_id", "copied_source", "created"]
    changed_fields: dict[str, Any] | None = None
```

### Commit Rule

Canonical write services never call `commit()`.

```python
# Correct transaction ownership:
result = await account_writer.create_account(
    name="Acme",
    fields={"domain": "acme.com"},
    mode="strict",
    context=AccountWriteContext(source="api", created_via="accounts_app"),
)
company = result.account
await org_session.commit()

# Correct inside deal writes:
company, ambiguous_name = await account_writer.resolve_or_create_for_deal_payload(
    {"name": "Acme", "website": "https://acme.com"},
    integration_id=internal_integration_id,
    now=now,
    context=AccountWriteContext(source="deal_write", created_via="deal_create"),
)
# DealWriteService continues writing deal fields, participants, and mutation event.
# The caller commits the whole deal write atomically.
```

## Task 0: Add Transaction Ownership Guardrails

**Files:**

- Modify: `src/api/routers/crm/accounts_handler.py`
- Modify: `src/api/routers/crm/contacts_handler.py`
- Modify: `src/agents/tools/crm/create_contact.py`
- Modify: `tests/api/routers/crm/test_accounts_handler.py`
- Add or modify: `tests/api/routers/crm/test_contacts_handler.py`
- Modify: `tests/agents/tools/crm/test_create_contact_account_tools.py`

- [x] **Step 1: Add account update handler commit test**

Write a test proving `accounts_handler.update_account()` commits once after `service.update_account()` succeeds.

```python
async def test_update_account_commits_session_after_service_write() -> None:
    service = FakeAccountsAccessService()
    response = await update_account(
        company_id=uuid4(),
        response=FastAPIResponse(),
        body=UpdateAccountRequest(name="Acme Updated"),
        service=service,
    )

    assert response.success is True
    assert service.org_session.commit_count == 1
```

- [x] **Step 2: Fix account update handler commit**

Add the missing commit after successful update:

```python
company = await service.update_account(company_id, updates)
await service.org_session.commit()
```

- [x] **Step 3: Add contact handler commit tests before moving service commits**

Tests should prove:

- create contact commits once after service write
- update contact commits once after service write
- service errors do not commit

Use fake services that do not commit internally, so these tests verify only router-owned commits. They must pass before service-level contact commits are removed.

- [x] **Step 4: Add agent contact-only commit regression**

Write a test for `create_contact_direct()` with no `deal_id`. It must prove a newly created contact is committed by the agent tool after the service write.

```python
result = await create_contact_direct(
    context=context,
    email="alex@acme.com",
    name="Alex Buyer",
)

assert result["status"] == "created"
assert context.session.commit_count == 1
```

- [x] **Step 5: Do not remove ContactsAccessService commits yet**

This task only adds guardrails and the missing account update commit. Service-level contact commits are removed later when access services delegate to `ContactWriteService`.

## Task 1: Add AccountWriteService Tests

**Files:**

- Create: `tests/service/crm/test_account_write_service.py`

- [ ] **Step 1: Add tests for strict account creation**

Create tests that prove:

- domain is normalized once
- duplicate domain raises `AccountWriteServiceError(status_code=409)`
- duplicate name raises `AccountWriteServiceError(status_code=409)` when domain is absent
- created account uses internal CRM integration
- service flushes but does not commit
- `strict_org` rejects duplicates across the org
- `internal_only` checks duplicates only inside the internal CRM integration

```python
async def test_create_account_strict_rejects_duplicate_domain(session, org_id, internal_integration):
    existing = CRMCompany(
        company_id=uuid4(),
        organisation_id=org_id,
        integration_id=internal_integration.integration_id,
        name="Acme",
        domain="acme.com",
        is_deleted=False,
    )
    session.add(existing)
    await session.flush()

    service = AccountWriteService(session, org_id)

    with pytest.raises(AccountWriteServiceError) as exc:
        await service.create_account(
            name="Acme Inc",
            fields={"domain": "https://www.acme.com/about"},
            mode="strict",
            context=AccountWriteContext(
                source="api",
                created_via="accounts_app",
                duplicate_scope="strict_org",
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.data == {"company_id": str(existing.company_id)}
```

- [ ] **Step 2: Add tests for resolve-or-create account creation**

Create tests that prove:

- existing domain account is returned instead of raising
- name-only matching returns a single exact normalized match
- ambiguous name-only matches return `(None, ambiguous_name)` for deal flows
- missing name/domain returns `(None, None)`
- provider-backed rows are not reused directly by internal CRM writes unless copied first

```python
async def test_resolve_or_create_for_deal_returns_existing_domain_account(
    session, org_id, internal_integration
):
    existing = CRMCompany(
        company_id=uuid4(),
        organisation_id=org_id,
        integration_id=internal_integration.integration_id,
        name="Acme",
        domain="acme.com",
        is_deleted=False,
    )
    session.add(existing)
    await session.flush()

    service = AccountWriteService(session, org_id)
    company, ambiguous_name = await service.resolve_or_create_for_deal_payload(
        {"name": "Acme Updated", "website": "https://acme.com"},
        integration_id=internal_integration.integration_id,
        now=datetime.now(UTC),
        context=AccountWriteContext(
            source="deal_write",
            created_via="deal_create",
            duplicate_scope="resolve_or_create",
        ),
    )

    assert company.company_id == existing.company_id
    assert ambiguous_name is None
```

- [ ] **Step 3: Add tests for provider-to-internal account copy**

Create tests that prove a provider-backed account selected by `company_id` is copied into the internal integration and existing internal copies are reused by domain/name.

## Task 2: Implement AccountWriteService

**Files:**

- Create: `src/service/crm/internal/account_write_service.py`
- Modify: `src/service/crm/internal/account_materialization_service.py`

- [x] **Step 1: Create service error and dataclasses**

Use `AccountWriteServiceError` as the canonical account-write exception. Keep it HTTP-shaped so access and deal services can translate it without losing status codes.

```python
class AccountWriteServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
```

- [x] **Step 2: Move account creation rules**

Implement `create_account()` with this behavior:

- normalize `domain` using `normalize_domain`
- reject duplicate domain/name in `mode="strict"`
- return existing account in `mode="resolve_or_create"`
- allow only the existing account field allowlist
- write `properties.source`, `properties.created_via`, and optional actor/source metadata
- get or create only the internal CRM integration without creating default pipelines/stages
- call `flush()` and `refresh()`
- do not call `commit()`

```text
AccountWriteService.create_account(
    *,
    name: str,
    fields: dict[str, Any] | None = None,
    mode: AccountCreateMode = "strict",
    context: AccountWriteContext,
) -> AccountWriteResult
```

The implementation must use one of these safe integration paths:

```python
integration = await internal_crm_setup.ensure_internal_integration(commit=False)
```

or an equivalent extracted helper that only creates `KnowledgeIntegration`. It must not call a setup method that also creates pipeline/stage rows from an account/contact write.

- [x] **Step 3: Move deal materialization account rules**

Move the current `AccountMaterializationService` methods into `AccountWriteService` with equivalent behavior:

- `resolve_or_create_for_deal_payload()`
- `load_org_company()`
- `ensure_internal_company_from_source()`
- `get_current_company()`
- `get_account_audit_snapshot()`
- `company_to_account_audit_snapshot()`

- [x] **Step 4: Convert AccountMaterializationService to adapter**

Keep the old class temporarily as a thin adapter so imports do not break during the refactor.

```python
class AccountMaterializationService(AccountWriteService):
    """Compatibility adapter for older deal-write imports.

    New code should use AccountWriteService directly.
    """
```

The adapter must not define any direct `CRMCompany(...)` creation logic.

## Task 3: Add ContactWriteService Tests

**Files:**

- Create: `tests/service/crm/test_contact_write_service.py`

- [ ] **Step 1: Add tests for strict contact creation**

Create tests that prove:

- email is normalized
- duplicate email raises `ContactWriteServiceError(status_code=409)`
- explicit `company_id` must belong to the org and be active
- created contact uses internal CRM integration
- service flushes but does not commit
- strict creation rejects missing email/name

```python
async def test_create_contact_strict_rejects_duplicate_email(
    session, org_id, internal_integration
):
    existing = CRMContact(
        contact_id=uuid4(),
        organisation_id=org_id,
        integration_id=internal_integration.integration_id,
        email="alex@acme.com",
        full_name="Alex Existing",
        is_deleted=False,
    )
    session.add(existing)
    await session.flush()

    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError) as exc:
        await service.create_contact(
            email="ALEX@ACME.COM",
            name="Alex New",
            fields={},
            mode="strict",
            context=ContactWriteContext(source="api", created_via="contacts_app"),
        )

    assert exc.value.status_code == 409
```

- [ ] **Step 2: Add tests for deal primary contact resolution**

Create tests that prove:

- existing email contact is reused
- name-only contact creation remains provisional
- ambiguous name-only match returns fallback name/email for deal audit behavior
- strict API creation does not allow name-only provisional contacts

- [ ] **Step 3: Add tests for provider-to-internal contact copy**

Create tests that prove provider-backed selected contacts are copied into internal CRM, linked to the internal account if provided, and existing internal copies are reused by email.

## Task 4: Implement ContactWriteService

**Files:**

- Create: `src/service/crm/internal/contact_write_service.py`
- Modify: `src/service/crm/internal/contact_materialization_service.py`

- [ ] **Step 1: Create service error and dataclasses**

```python
class ContactWriteServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
```

- [ ] **Step 2: Move contact creation rules**

Implement `create_contact()` with this behavior:

- normalize email with `normalize_email`
- reject duplicate email in `mode="strict"`
- return existing contact in `mode="resolve_or_create"`
- split display name into first/last when needed
- validate explicit company_id through `AccountWriteService.load_org_company()`
- store `linkedin_url` in `properties`
- get or create only the internal CRM integration without creating default pipelines/stages
- call `flush()` and `refresh()`
- do not call `commit()`

```text
ContactWriteService.create_contact(
    *,
    email: str,
    name: str,
    fields: dict[str, Any] | None = None,
    mode: ContactCreateMode = "strict",
    context: ContactWriteContext,
) -> ContactWriteResult
```

`create_contact()` is for strict and normal resolve-or-create paths. Deal-specific name-only behavior stays in:

```text
ContactWriteService.resolve_or_create_primary_contact(
    *,
    primary_contact_name: str | None,
    primary_contact_email: str | None,
    company_name: str | None,
    integration_id: UUID,
    now: datetime,
    context: ContactWriteContext,
) -> tuple[CRMContact | None, str | None, str | None]
```

- [ ] **Step 3: Move materialization contact rules**

Move the current `ContactMaterializationService` methods into `ContactWriteService` with equivalent behavior:

- `resolve_or_create_primary_contact()`
- `load_org_contact()`
- `ensure_internal_contact_from_source()`

- [ ] **Step 4: Move contact update rules**

Move `ContactsAccessService.update_contact()` rules into `ContactWriteService.update_contact()`:

- email is set-once
- company_id is set-once and cannot be unlinked
- contact must have email before account link
- `linkedin_url` lives in `properties`
- allowed scalar fields only
- call `flush()` and `refresh()`
- do not call `commit()`

- [ ] **Step 5: Convert ContactMaterializationService to adapter**

```python
class ContactMaterializationService(ContactWriteService):
    """Compatibility adapter for older deal-write imports.

    New code should use ContactWriteService directly.
    """
```

The adapter must not define any direct `CRMContact(...)` creation logic.

## Task 5: Delegate API Access Writes

**Files:**

- Modify: `src/service/crm/access/accounts_access_service.py`
- Modify: `src/service/crm/access/contacts_access_service.py`
- Modify: `tests/service/crm/test_accounts_access_service.py`
- Modify: `tests/service/crm/test_contacts_access_service.py`

- [x] **Step 1: AccountsAccessService delegates writes**

Replace direct account creation/update with:

```python
writer = AccountWriteService(self.org_session, self.organisation_id)
result = await writer.create_account(
    name=name,
    fields=kwargs,
    mode="strict",
    context=AccountWriteContext(
        source="api",
        created_via="accounts_app",
        duplicate_scope="strict_org",
    ),
)
return result.account
```

`update_account()` delegates to:

```python
return await writer.update_account(
    company_id=company_id,
    updates=updates,
    context=AccountWriteContext(
        source="api",
        created_via="accounts_app",
        duplicate_scope="strict_org",
    ),
)
```

- [ ] **Step 2: ContactsAccessService delegates writes**

Replace direct contact creation/update with:

```python
writer = ContactWriteService(self.org_session, self.organisation_id)
result = await writer.create_contact(
    email=email,
    name=name,
    fields=kwargs,
    mode="strict",
    context=ContactWriteContext(source="api", created_via="contacts_app"),
)
return result.contact
```

`update_contact()` delegates to:

```python
return await writer.update_contact(
    contact_id=contact_id,
    updates=updates,
    context=ContactWriteContext(source="api", created_via="contacts_app"),
)
```

- [ ] **Step 3: ContactsAccessService account creation wrapper delegates to AccountWriteService**

Keep this wrapper only for agent-tool compatibility during migration:

```python
async def create_account(self, name: str, **kwargs: Any) -> CRMCompany:
    writer = AccountWriteService(self.org_session, self.organisation_id)
    result = await writer.create_account(
        name=name,
        fields=kwargs,
        mode="strict",
        context=AccountWriteContext(source="api", created_via="contact_create"),
    )
    return result.account
```

## Task 6: Re-check API Transaction Ownership During Delegation

**Files:**

- Modify: `src/api/routers/crm/accounts_handler.py`
- Modify: `src/api/routers/crm/contacts_handler.py`
- Modify: `tests/api/routers/crm/test_accounts_handler.py`
- Add or modify: `tests/api/routers/crm/test_contacts_handler.py`

- [ ] **Step 1: Confirm account update handler still commits**

`accounts_handler.update_account()` must commit after service write. This is currently missing and should be fixed before access-service write delegation.

```python
company = await service.update_account(company_id, updates)
await service.org_session.commit()
```

- [ ] **Step 2: Confirm contact create/update handlers commit after service write**

`contacts_handler.create_contact()` and `contacts_handler.update_contact()` must commit after service write.

```python
contact = await service.create_contact(email=body.email, name=body.name, **extra)
await service.org_session.commit()
```

```python
contact = await service.update_contact(contact_id, updates)
await service.org_session.commit()
```

- [ ] **Step 3: Keep handler regression tests green**

Tests must prove account update and contact create/update commit exactly once after successful service writes and do not commit after service errors.

- [ ] **Step 4: Keep agent transaction regression test green before moving commits**

Test `create_contact_direct()` with no `deal_id` and assert `org_session.commit()` is called after a new contact is created. This must be green before removing commits from `ContactsAccessService`.

## Task 7: Migrate DealWriteService

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Modify: `tests/service/crm/test_deal_write_service_account_copy.py`
- Modify: `tests/service/crm/test_deal_write_service_account_updates.py`
- Modify: `tests/service/crm/test_deal_write_service_mutation_events.py`

- [ ] **Step 1: Replace constructor dependencies**

Change:

```python
self._account_materializer = AccountMaterializationService(
    self.session,
    self.organisation_id,
)
self._contact_materializer = ContactMaterializationService(
    self.session,
    self.organisation_id,
)
```

To:

```python
self._account_writer = AccountWriteService(self.session, self.organisation_id)
self._contact_writer = ContactWriteService(self.session, self.organisation_id)
```

- [ ] **Step 2: Update call sites**

Map old calls to new calls:

- `upsert_company_from_account()` -> `resolve_or_create_for_deal_payload()`
- `ensure_internal_company_from_source()` -> same method on `AccountWriteService`
- `get_account_audit_snapshot()` -> same method on `AccountWriteService`
- `upsert_contact_from_primary()` -> `resolve_or_create_primary_contact()`
- `ensure_internal_contact_from_source()` -> same method on `ContactWriteService`

- [ ] **Step 3: Preserve mutation behavior**

Focused tests must still pass for:

- deal create event
- multi-field deal update event
- update account mutation event
- update primary contact mutation event
- no-op account/contact updates do not create mutation events

## Task 8: Migrate Agent Tool Commit Behavior

**Files:**

- Modify: `src/agents/tools/crm/create_account.py`
- Modify: `src/agents/tools/crm/create_contact.py`
- Modify: `tests/agents/tools/crm/test_create_contact_account_tools.py`

- [ ] **Step 1: Account tool remains create-or-reuse**

Keep existing agent behavior:

- pre-check by domain/name
- create if absent
- commit after create
- rollback on access/write error
- no double commit when `ContactsAccessService.create_contact()` stops committing

- [ ] **Step 2: Contact tool commits contact-only writes**

After canonical contact writer stops committing internally, `create_contact_direct()` must commit even when no deal is linked.

```python
contact = await service.create_contact(
    email=normalized_email,
    name=cleaned_name,
    **kwargs,
)
if parsed_deal_id:
    link = await service.add_contact_to_deal(
        contact_id=contact.contact_id,
        deal_id=parsed_deal_id,
        relationship_role=_clean_optional(relationship_role),
        actor_user_id=context.user_id,
    )
await org_session.commit()
```

- [ ] **Step 3: Preserve account selection behavior**

Tests must prove:

- multiple account domain matches still return `needs_account_selection`
- single domain account still links automatically
- account_name can create account and contact in one transaction
- deal link dispatch still happens after commit

## Task 9: Migrate Stakeholder Contact Creation

**Files:**

- Modify: `src/service/crm/insights/stakeholders_service.py`
- Modify: `tests/service/crm/insights/test_stakeholders_service_participant_writes.py`
- Modify: `tests/service/crm/test_stakeholder_contact_linking.py`

- [ ] **Step 1: Replace direct CRMContact creation**

`_ensure_contact_for_stakeholder()` keeps stakeholder-specific skip rules, then delegates contact creation:

```python
writer = ContactWriteService(self.session, self.organisation_id)
result = await writer.create_contact(
    email=email_normalized,
    name=name or email_normalized,
    fields={
        "job_title": job_title,
        "phone": phone_number,
        "properties": {
            key: value
            for key, value in {
                "department": department,
                "external_links": external_links,
            }.items()
            if value
        },
    },
    mode="resolve_or_create",
    context=ContactWriteContext(
        source="api",
        actor_user_id=self.user_id,
        created_via="stakeholder_create",
    ),
)
return result.contact.contact_id
```

- [ ] **Step 2: Preserve existing contact enrichment**

If an existing contact is reused, preserve current behavior that fills missing or newer stakeholder profile values.

## Task 10: Migrate Calendar Discovery Batch Writes

**Files:**

- Modify: `src/service/crm/discovery/calendar_contact_account_discovery.py`
- Modify: `tests/service/crm/discovery/test_calendar_contact_account_discovery.py`

- [ ] **Step 1: Prove generic writers can support discovery before adding special methods**

Try to express discovery account/contact creation through generic writer methods with `flush=False` and discovery context. Add special discovery methods only if the generic contract becomes unclear or inefficient.

- [ ] **Step 2: Add batch-safe writer methods only if needed**

Add writer methods that add rows without flushing each row:

```text
AccountWriteService.create_discovered_account(
    *,
    domain: str,
    name: str,
    properties: dict[str, Any],
    integration_id: UUID,
    now: datetime,
    flush: bool = False,
) -> CRMCompany
```

```text
ContactWriteService.create_discovered_contact(
    *,
    email: str,
    first_name: str | None,
    last_name: str | None,
    full_name: str,
    company: CRMCompany,
    properties: dict[str, Any],
    integration_id: UUID,
    now: datetime,
    flush: bool = False,
) -> CRMContact
```

- [ ] **Step 3: Replace direct discovery creation**

Discovery service keeps candidate filtering and preloaded existing maps, then delegates creation to writers and calls one final `flush()`.

- [ ] **Step 4: Preserve discovery counters**

Existing discovery tests must still prove:

- account is created before contact
- existing contacts are skipped
- existing account domains are skipped
- counters match current behavior

## Task 11: Remove Competing Creation Logic

**Files:**

- Modify: `src/service/crm/internal/account_materialization_service.py`
- Modify: `src/service/crm/internal/contact_materialization_service.py`
- Search: `rg -n "CRMCompany\\(|CRMContact\\(" src/service/crm src/agents/tools/crm`

- [ ] **Step 1: Verify remaining direct constructors**

After migration, direct constructors under internal CRM write surfaces should be limited to:

- `account_write_service.py`
- `contact_write_service.py`
- provider sync writers under `src/service/integrations/`
- tests

- [ ] **Step 2: Keep or delete compatibility adapters**

If no production imports remain, delete:

- `src/service/crm/internal/account_materialization_service.py`
- `src/service/crm/internal/contact_materialization_service.py`

If imports remain in tests or staged migration code, keep thin adapter classes only.

## Task 12: Verification

**Files:**

- No new files unless tests expose a missing helper.

- [ ] **Step 1: Run focused CRM write tests**

```bash
uv run pytest tests/service/crm/test_account_write_service.py tests/service/crm/test_contact_write_service.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run API handler tests**

```bash
uv run pytest tests/api/routers/crm/test_accounts_handler.py tests/api/routers/crm/test_contacts_handler.py -v
```

Expected: account/contact write handlers commit successful writes and avoid commits on service errors.

- [ ] **Step 3: Run deal write regression tests**

```bash
uv run pytest tests/service/crm/test_deal_write_service_account_copy.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_deal_write_service_mutation_events.py -v
```

Expected: account/contact deal update behavior and mutation events remain unchanged.

- [ ] **Step 4: Run agent tool tests**

```bash
uv run pytest tests/agents/tools/crm/test_create_contact_account_tools.py -v
```

Expected: agent account/contact creation, reuse, account selection, and deal linking remain unchanged.

- [ ] **Step 5: Run type checks for changed files**

```bash
uv run mypy src/service/crm/internal/account_write_service.py src/service/crm/internal/contact_write_service.py src/service/crm/access/accounts_access_service.py src/service/crm/access/contacts_access_service.py src/service/crm/internal/deal_write_service.py src/agents/tools/crm/create_account.py src/agents/tools/crm/create_contact.py
```

Expected: no mypy errors in changed CRM write files.

## Rollout Notes

- This is a backend refactor with no API response-shape change.
- First implementation should keep compatibility adapters to reduce blast radius.
- Do not add account/contact mutation event tables in this refactor. The writer result shape should make future account/contact mutation events easy, but this plan is about write-path consolidation.
- Do not route HubSpot/Salesforce sync writers through these services. Provider sync has separate source mapping and snapshot semantics.
- The highest-risk behavior change is transaction ownership for contacts. API handlers and agent tools must explicitly commit after successful writes.
- Do not call a setup method that creates default pipeline/stage rows from account/contact writes. Extract or add an integration-only helper if needed.
- Do not migrate stakeholder and discovery direct constructors in the same patch as access-service transaction changes.

## Loophole Review

- **Duplicate race condition:** service-level duplicate checks still have a race window without database uniqueness constraints. This plan preserves current behavior; a later migration should add partial unique indexes for internal account domains and contact emails if existing data permits it.
- **Contact update compatibility:** moving commits out of `ContactsAccessService` will break callers unless every create/update caller is updated in the same implementation batch.
- **Agent contact-only persistence:** the current agent tool depends on service-level contact commit. This plan explicitly adds a tool-level commit after contact create.
- **Discovery performance:** per-row flushing would be inefficient. The plan requires batch-safe writer methods and one final flush.
- **Provider sync separation:** forcing provider sync through internal write services would be wrong because provider rows need provider timestamps, external IDs, source mappings, and deletion semantics.
- **Mutation events:** account/contact mutation history is not solved here. Deal-level account/contact changes continue to be recorded by `DealWriteService`; pure account/contact entity mutation events should be a separate phase.
- **Access-service naming:** access services can keep `create_*` methods for router/tool compatibility, but those methods must be delegation wrappers only.
- **Accidental pipeline bootstrap:** account/contact writes must not create default deal pipelines/stages as a side effect.
- **Duplicate scope drift:** explicit account create and deal enrichment do not have the same duplicate semantics; the writer must encode the mode instead of relying on caller convention.
- **Over-specialized discovery methods:** discovery-specific writer methods should be added only if generic writer methods cannot express batch creation cleanly.

## Success Criteria

- `rg -n "CRMCompany\\(|CRMContact\\(" src/service/crm src/agents/tools/crm` shows internal account/contact creation only in canonical write services, with provider sync excluded.
- `DealWriteService` imports `AccountWriteService` and `ContactWriteService`, not materialization services.
- `AccountsAccessService.create_account/update_account` and `ContactsAccessService.create_contact/update_contact` delegate to canonical writers.
- Contact create/update services no longer call `commit()`.
- Account/contact API handlers commit after successful writes.
- Account update handler commits after successful write.
- Agent direct contact creation persists when no deal link is requested.
- Account/contact writes do not create default CRM pipeline/stage rows.
- Focused CRM, API, deal-write, and agent tests pass.
