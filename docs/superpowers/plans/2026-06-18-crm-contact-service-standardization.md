# CRM Contact Service Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the vertical CRM contact module after the account module and make contact reads, writes, and deal-linking flow through explicit canonical services.

**Architecture:** Implement `src/service/crm/contacts/` from the current checkout. `ContactReadService` is extracted from read-only behavior currently in `ContactsAccessService`; `ContactWriteService` owns contact create/update and account-link validation; `ContactDealLinkService` owns contact-to-deal participant linking because that is not a pure contact write. `ContactsAccessService` remains temporarily as a compatibility facade until API and agent call sites are migrated.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, pytest, mypy, ruff.

---

## Starting Point

This plan is based on the current checkout after stash cleanup.

Current facts:

- `src/service/crm/access/contacts_access_service.py` exists and mixes read, write, account lookup/create delegation, and add-to-deal behavior.
- `src/api/routers/crm/contacts_handler.py` depends on `ContactsAccessService` for list, detail, create, update, and add-to-deal.
- `src/agents/tools/crm/create_contact.py` depends on `ContactsAccessService` for contact creation, account lookup/create, and deal linking.
- `CRMContact.company_id` exists and is an explicit FK to `crm_companies.company_id`.
- Contact display still falls back to email-domain account matching when `company_id` is absent.
- Provider sync contact writers exist under `src/service/integrations/crm/records/.../contacts/` and are outside this service-standardization slice.
- Account service standardization should be implemented first because contact writes need canonical account lookup/create behavior.

## Current Problem

Contact behavior is scattered by caller and mixes multiple domain responsibilities.

Current production risks:

- API contact creation uses `ContactsAccessService.create_contact()`.
- Agent contact creation also uses `ContactsAccessService.create_contact()`.
- Agent account resolution during contact creation uses contact-service wrapper methods that call account access services.
- Contact update, account linking, and add-to-deal are in the same service even though add-to-deal creates deal participants and can trigger stakeholder sync.
- Contact creation commits inside the service, while add-to-deal commits in the router/tool. That makes transaction ownership inconsistent.
- Future workflows, WhatsApp actions, and agent tools would have to reuse a broad access service instead of a write-specific service.

## Future Folder Pattern

Use the same vertical entity-module pattern as accounts.

```text
src/service/crm/
  contacts/
    __init__.py
    contracts.py
    read_service.py
    write_service.py
    deal_link_service.py
  access/
    contacts_access_service.py        # temporary compatibility surface during migration
```

After accounts and contacts both exist, the target CRM service layout becomes:

```text
src/service/crm/
  accounts/
    contracts.py
    read_service.py
    write_service.py
  contacts/
    contracts.py
    read_service.py
    write_service.py
    deal_link_service.py
  deals/
    contracts.py
    read_service.py
    write_service.py
    mutation_policy.py
  shared/
    entity_normalizers.py
    materialization_errors.py
```

## Target Contact Boundaries

Read callers converge here:

```text
API contact list/detail       -> ContactReadService
agent contact search/detail   -> ContactReadService
dropdown contact search       -> ContactReadService
```

Contact write callers converge here:

```text
API contact create/update      -> ContactWriteService
agent create_contact           -> ContactWriteService
future workflow contact writes -> ContactWriteService
future WhatsApp/chat writes    -> ContactWriteService
```

Contact-to-deal linking converges here:

```text
API add contact to deal        -> ContactDealLinkService
agent create_contact with deal -> ContactDealLinkService
future workflow add-to-deal    -> ContactDealLinkService
```

Temporary compatibility:

```text
ContactsAccessService          -> delegates to ContactReadService + ContactWriteService + ContactDealLinkService
```

## Non-Negotiable Rules

- Implement accounts first; contacts should import canonical account services, not `AccountsAccessService`.
- Do not change provider sync contact writers in this plan.
- Do not delete `ContactsAccessService` in the first implementation step.
- New contact code must live under `src/service/crm/contacts/`.
- New production contact writes must use `src.service.crm.contacts.write_service.ContactWriteService`.
- New production contact reads must use `src.service.crm.contacts.read_service.ContactReadService`.
- New production contact-to-deal writes must use `src.service.crm.contacts.deal_link_service.ContactDealLinkService`.
- `ContactReadService` must not expose `create_contact()`, `update_contact()`, or `add_contact_to_deal()`.
- `ContactWriteService` must not call `DealParticipantService`.
- `ContactDealLinkService` may call `DealParticipantService`.
- Contact write services must not own transaction commit; caller owns commit/rollback.
- Every contact query/write must filter or set `organisation_id`.
- Internal contact duplicate checks must stay tenant-scoped and must not merge by email across provider-owned records.
- Keep email-change behavior conservative: a contact with an existing email cannot change that email in this slice.
- Keep account link behavior conservative: `company_id` is explicit; domain matching is display fallback, not silent write-time linking.
- Do not add contact mutation events in this plan; add them only after contact write boundaries are stable.

## Service Responsibilities

### `src/service/crm/contacts/contracts.py`

Defines contact-domain DTOs and exceptions:

- `ContactWriteContext`
- `ContactWriteResult`
- `ContactDealLinkResult`
- `ContactMatchedBy`
- `ContactWriteServiceError`
- `ContactReadServiceError`
- `ContactDealLinkServiceError`

### `src/service/crm/contacts/read_service.py`

Owns read-only behavior extracted from `ContactsAccessService`:

- `resolve_source_for_contact()`
- `list_contacts()`
- `get_contact_detail()`
- `find_contact_by_email()`
- `_extract_domain()`
- `_map_source()`

It must not construct `CRMContact(...)`.

### `src/service/crm/contacts/write_service.py`

Owns contact create/update behavior extracted from `ContactsAccessService`:

- `create_contact()`
- `update_contact()`
- `find_contact_by_email()` only if needed for idempotent create callers
- `_split_name()`
- `_get_or_create_internal_integration()`
- account-link validation through `AccountWriteService.load_org_company()`

It must be the only CRM service file that constructs `CRMContact(...)` for internal contact writes.

### `src/service/crm/contacts/deal_link_service.py`

Owns contact-to-deal participant linking:

- `add_contact_to_deal()`
- contact existence validation
- contact email precondition
- call into `DealParticipantService`

It does not dispatch stakeholder sync itself. The current router/tool already dispatches stakeholder sync after commit, and that should remain at the orchestration boundary until a workflow dispatcher exists.

### `src/service/crm/access/contacts_access_service.py`

Temporary compatibility facade:

- Delegates reads to `ContactReadService`.
- Delegates writes to `ContactWriteService`.
- Delegates contact-to-deal linking to `ContactDealLinkService`.
- Keeps `ContactsAccessServiceError` compatibility until production imports are migrated.
- Must not contain `CRMContact(...)` construction after migration.

## Task 0: Add Contact Module And Boundary Tests

**Files:**

- Create: `src/service/crm/contacts/__init__.py`
- Create: `src/service/crm/contacts/contracts.py`
- Create: `tests/service/crm/test_contact_service_boundaries.py`

- [ ] **Step 1: Create the boundary test file**

Create `tests/service/crm/test_contact_service_boundaries.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def test_contact_vertical_module_exists() -> None:
    assert Path("src/service/crm/contacts/__init__.py").exists()
    assert Path("src/service/crm/contacts/contracts.py").exists()
    assert Path("src/service/crm/contacts/read_service.py").exists()
    assert Path("src/service/crm/contacts/write_service.py").exists()
    assert Path("src/service/crm/contacts/deal_link_service.py").exists()


def test_contact_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/contacts/read_service.py").read_text()

    assert "async def create_contact" not in source
    assert "async def update_contact" not in source
    assert "async def add_contact_to_deal" not in source
    assert "CRMContact(" not in source


def test_contact_write_service_does_not_manage_deal_participants() -> None:
    source = Path("src/service/crm/contacts/write_service.py").read_text()

    assert "DealParticipantService" not in source
    assert "CRMDealParticipant" not in source
    assert "send_deal_task_async" not in source


def test_contact_construction_removed_from_access_service() -> None:
    result = subprocess.run(
        [
            "rg",
            "-n",
            "CRMContact\\(",
            "src/service/crm/access/contacts_access_service.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run the boundary test and verify it fails**

Run:

```bash
uv run pytest tests/service/crm/test_contact_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/contacts/` does not exist yet.

- [ ] **Step 3: Create contact package**

Create `src/service/crm/contacts/__init__.py`:

```python
"""Contact read, write, and deal-link services for the internal CRM."""
```

- [ ] **Step 4: Create contact contracts**

Create `src/service/crm/contacts/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact

ContactMatchedBy = Literal["email", "explicit_id", "created"]


@dataclass(frozen=True)
class ContactWriteContext:
    """Caller metadata for future contact audit events and workflow triggers."""

    source: str
    actor_user_id: UUID | None = None
    source_label: str | None = None
    created_via: str | None = None


@dataclass(frozen=True)
class ContactWriteResult:
    """Canonical contact write result returned to API, agent, workflow, and deal callers."""

    contact: CRMContact
    created: bool
    matched_by: ContactMatchedBy


@dataclass(frozen=True)
class ContactDealLinkResult:
    """Result of linking an existing contact to a deal as a participant."""

    created: list[dict]
    skipped: list[dict]
    contact_participant_changed: bool


class ContactWriteServiceError(Exception):
    """HTTP-shaped contact write error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class ContactReadServiceError(Exception):
    """HTTP-shaped contact read error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class ContactDealLinkServiceError(Exception):
    """HTTP-shaped contact-to-deal linking error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
```

## Task 1: Extract ContactReadService

**Files:**

- Create: `src/service/crm/contacts/read_service.py`
- Modify in Task 6: `src/service/crm/access/contacts_access_service.py`
- Test: `tests/service/crm/test_contact_service_boundaries.py`

- [ ] **Step 1: Copy read-only behavior into ContactReadService**

Create `src/service/crm/contacts/read_service.py` from the read-only parts of current `src/service/crm/access/contacts_access_service.py`.

The class name must be:

```python
class ContactReadService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Move these methods without changing behavior:

```python
close
_extract_domain
_map_source
resolve_source_for_contact
list_contacts
get_contact_detail
find_contact_by_email
```

Replace `ContactsAccessServiceError` with:

```python
from src.service.crm.contacts.contracts import ContactReadServiceError
```

- [ ] **Step 2: Keep read service account behavior read-only**

`ContactReadService` may read `CRMCompany` for account display resolution, but it must not create or update accounts.

The file must not import:

```python
uuid4
IntegrationScope
```

- [ ] **Step 3: Verify read service boundary**

Run:

```bash
uv run pytest tests/service/crm/test_contact_service_boundaries.py::test_contact_read_service_does_not_expose_writes -q
```

Expected: PASS after `read_service.py` exists and contains no write methods.

## Task 2: Create ContactWriteService

**Files:**

- Create: `src/service/crm/contacts/write_service.py`
- Test: `tests/service/crm/test_contact_write_service.py`

- [ ] **Step 1: Create write-service tests for current contact behavior**

Create `tests/service/crm/test_contact_write_service.py` with tests covering:

```python
async def test_create_contact_rejects_duplicate_email_in_org(...)
async def test_create_contact_normalizes_email_and_splits_name(...)
async def test_create_contact_links_explicit_existing_account(...)
async def test_create_contact_rejects_cross_org_or_deleted_account(...)
async def test_update_contact_rejects_existing_email_change(...)
async def test_update_contact_can_set_email_and_account_together_when_email_missing(...)
async def test_update_contact_rejects_account_unlink(...)
async def test_update_contact_stores_linkedin_url_in_properties(...)
```

Expected behavior must match current `ContactsAccessService.create_contact()` and `ContactsAccessService.update_contact()`, except transaction ownership changes: the service flushes and refreshes but does not commit.

- [ ] **Step 2: Implement ContactWriteService**

Create `src/service/crm/contacts/write_service.py`:

```python
class ContactWriteService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Extract these methods from current `ContactsAccessService`:

```python
_split_name
create_contact
update_contact
_get_or_create_internal_integration
```

Use canonical account service behavior for account validation:

```python
from src.service.crm.accounts.write_service import AccountWriteService
```

When validating a `company_id`, call:

```python
company = await self._account_writer.load_org_company(company_id)
```

If `load_org_company()` returns `None`, raise:

```python
ContactWriteServiceError("Account not found", status_code=404)
```

- [ ] **Step 3: Keep transaction ownership outside the write service**

The final lines of `create_contact()` and `update_contact()` should be:

```python
self.org_session.add(contact)
await self.org_session.flush()
await self.org_session.refresh(contact)
return ContactWriteResult(contact=contact, created=True, matched_by="created")
```

For update:

```python
self.org_session.add(contact)
await self.org_session.flush()
await self.org_session.refresh(contact)
return ContactWriteResult(contact=contact, created=False, matched_by="explicit_id")
```

The service must not call:

```python
await self.org_session.commit()
```

- [ ] **Step 4: Run write-service tests**

Run:

```bash
uv run pytest tests/service/crm/test_contact_write_service.py -q
```

Expected: PASS.

## Task 3: Create ContactDealLinkService

**Files:**

- Create: `src/service/crm/contacts/deal_link_service.py`
- Test: `tests/service/crm/test_contact_deal_link_service.py`

- [ ] **Step 1: Create deal-link tests**

Create `tests/service/crm/test_contact_deal_link_service.py` with tests covering:

```python
async def test_add_contact_to_deal_rejects_missing_contact(...)
async def test_add_contact_to_deal_rejects_contact_without_email(...)
async def test_add_contact_to_deal_delegates_to_deal_participant_service(...)
async def test_add_contact_to_deal_does_not_dispatch_stakeholder_sync(...)
```

The last test protects the boundary: dispatch remains in router/tool orchestration until the workflow dispatcher exists.

- [ ] **Step 2: Implement ContactDealLinkService**

Create `src/service/crm/contacts/deal_link_service.py`:

```python
class ContactDealLinkService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Move `add_contact_to_deal()` from `ContactsAccessService` into this service.

Replace `ContactsAccessServiceError` with:

```python
from src.service.crm.contacts.contracts import ContactDealLinkServiceError
```

Keep the existing call into:

```python
from src.service.crm.internal.deal_participant_service import DealParticipantService
```

- [ ] **Step 3: Return a typed result**

Return `ContactDealLinkResult` from `contracts.py`, not a raw dict:

```python
return ContactDealLinkResult(
    created=created,
    skipped=[
        {
            "participant_type": s.participant_type,
            "related_id": s.related_id,
            "relationship_role": s.relationship_role,
            "reason": s.reason,
        }
        for s in skipped
    ],
    contact_participant_changed=contact_changed,
)
```

- [ ] **Step 4: Run deal-link tests**

Run:

```bash
uv run pytest tests/service/crm/test_contact_deal_link_service.py -q
```

Expected: PASS.

## Task 4: Route Contact API Through New Services

**Files:**

- Modify: `src/api/routers/crm/contacts_handler.py`
- Modify/Add: contact API tests under `tests/api/routers/crm/`

- [ ] **Step 1: Split FastAPI dependencies by responsibility**

Replace the single `get_contacts_service()` dependency with:

```python
async def get_contact_read_service(...):
    service = ContactReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        await service.close()


async def get_contact_write_service(...):
    service = ContactWriteService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        await service.close()


async def get_contact_deal_link_service(...):
    service = ContactDealLinkService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        await service.close()
```

- [ ] **Step 2: Route read endpoints**

Use `ContactReadService` for:

```python
list_contacts
get_contact_detail
```

- [ ] **Step 3: Route write endpoints**

Use `ContactWriteService` for:

```python
create_contact
update_contact
```

Create and update calls should pass:

```python
ContactWriteContext(
    source="api",
    actor_user_id=user_ids.user_id,
    created_via="contacts_app",
)
```

After successful create/update, the router must commit:

```python
await service.org_session.commit()
```

On `ContactWriteServiceError`, rollback before returning the error:

```python
await service.org_session.rollback()
```

- [ ] **Step 4: Route add-to-deal endpoint**

Use `ContactDealLinkService` for:

```python
add_contact_to_deal
```

After successful link:

```python
await service.org_session.commit()
```

Keep the existing stakeholder sync dispatch in the router after commit.

- [ ] **Step 5: Run contact router tests**

Run:

```bash
uv run pytest tests/api/routers/crm/test_contacts_handler.py -q
```

Expected: PASS. If the checkout does not have this exact test file, add focused API tests for list/detail/create/update/add-to-deal response behavior before routing.

## Task 5: Route Agent CreateContact Tool Through New Services

**Files:**

- Modify: `src/agents/tools/crm/create_contact.py`
- Modify/Add: contact agent tests under `tests/agents/` or `tests/service/agents/`

- [ ] **Step 1: Replace ContactsAccessService imports**

Replace:

```python
from src.service.crm.access.contacts_access_service import (
    ContactsAccessService,
    ContactsAccessServiceError,
)
```

with:

```python
from src.service.crm.accounts.read_service import AccountReadService
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.contacts.contracts import (
    ContactDealLinkServiceError,
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.deal_link_service import ContactDealLinkService
from src.service.crm.contacts.read_service import ContactReadService
from src.service.crm.contacts.write_service import ContactWriteService
```

- [ ] **Step 2: Use contact reader for existing-contact lookup**

Inside `create_contact_direct()`, replace:

```python
service = ContactsAccessService(org_session, organisation_id)
contact = await service.find_contact_by_email(normalized_email)
```

with:

```python
contact_reader = ContactReadService(org_session, organisation_id)
contact_writer = ContactWriteService(org_session, organisation_id)
account_reader = AccountReadService(org_session, organisation_id)
account_writer = AccountWriteService(org_session, organisation_id)
deal_linker = ContactDealLinkService(org_session, organisation_id)

contact = await contact_reader.find_contact_by_email(normalized_email)
```

- [ ] **Step 3: Use account reader/writer for account resolution**

Replace account lookup/create calls with:

```python
matches = await account_reader.find_accounts_by_domain(lookup_domain)
```

and:

```python
account_result = await account_writer.create_account(
    name=name_value,
    context=AccountWriteContext(
        source="agent",
        actor_user_id=context.user_id,
        created_via="create_contact_tool",
        duplicate_scope="internal_only",
    ),
)
account = account_result.account
```

Use the exact `AccountWriteContext` signature from the completed account plan.

- [ ] **Step 4: Use contact writer for creation**

Replace:

```python
contact = await service.create_contact(
    email=normalized_email,
    name=cleaned_name,
    **kwargs,
)
```

with:

```python
contact_result = await contact_writer.create_contact(
    email=normalized_email,
    name=cleaned_name,
    context=ContactWriteContext(
        source="agent",
        actor_user_id=context.user_id,
        created_via="create_contact_tool",
    ),
    **kwargs,
)
contact = contact_result.contact
await org_session.commit()
```

- [ ] **Step 5: Use deal linker for optional deal association**

Replace:

```python
link = await service.add_contact_to_deal(...)
```

with:

```python
link_result = await deal_linker.add_contact_to_deal(
    contact_id=contact.contact_id,
    deal_id=parsed_deal_id,
    relationship_role=_clean_optional(relationship_role),
    actor_user_id=context.user_id,
)
await org_session.commit()
link = {
    "created": link_result.created,
    "skipped": link_result.skipped,
    "contact_participant_changed": link_result.contact_participant_changed,
}
```

Keep `_dispatch_stakeholder_sync()` after commit.

- [ ] **Step 6: Run agent contact tool tests**

Run:

```bash
uv run pytest tests/service/agents/test_channel_tool_registry.py -q
```

Expected: PASS. Add focused `create_contact_direct()` tests if existing coverage does not exercise create, account selection, and deal-link paths.

## Task 6: Convert ContactsAccessService Into Compatibility Facade

**Files:**

- Modify: `src/service/crm/access/contacts_access_service.py`
- Test: `tests/service/crm/test_contact_service_boundaries.py`

- [ ] **Step 1: Replace implementation with delegation**

Keep the public class for compatibility:

```python
class ContactsAccessService:
    """Deprecated compatibility facade; use contact read, write, or deal-link services."""
```

Delegate reads to:

```python
self._reader = ContactReadService(org_session, organisation_id)
```

Delegate writes to:

```python
self._writer = ContactWriteService(org_session, organisation_id)
```

Delegate add-to-deal to:

```python
self._deal_linker = ContactDealLinkService(org_session, organisation_id)
```

- [ ] **Step 2: Preserve old exception compatibility**

Keep:

```python
ContactsAccessServiceError = ContactReadServiceError
```

When delegating write and link methods, translate:

```python
ContactWriteServiceError
ContactDealLinkServiceError
```

into:

```python
ContactsAccessServiceError(exc.message, status_code=exc.status_code, data=exc.data)
```

- [ ] **Step 3: Verify no contact construction remains in access service**

Run:

```bash
rg -n "CRMContact\\(" src/service/crm/access/contacts_access_service.py
```

Expected: no output.

## Task 7: Focused Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Verify service boundaries**

Run:

```bash
uv run pytest tests/service/crm/test_contact_service_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify contact writer**

Run:

```bash
uv run pytest tests/service/crm/test_contact_write_service.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify contact deal linker**

Run:

```bash
uv run pytest tests/service/crm/test_contact_deal_link_service.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify contact API**

Run:

```bash
uv run pytest tests/api/routers/crm/test_contacts_handler.py -q
```

Expected: PASS, or run the focused contact router tests added in Task 4 if this file does not exist.

- [ ] **Step 5: Verify agent contact tool**

Run:

```bash
uv run pytest tests/service/agents/test_channel_tool_registry.py -q
```

Expected: PASS. If focused `create_contact_direct()` tests were added, run them too.

- [ ] **Step 6: Run focused lint and type checks before PR**

Run:

```bash
uv run ruff check src/service/crm/contacts src/service/crm/access/contacts_access_service.py src/api/routers/crm/contacts_handler.py src/agents/tools/crm/create_contact.py tests/service/crm/test_contact_service_boundaries.py tests/service/crm/test_contact_write_service.py tests/service/crm/test_contact_deal_link_service.py
```

Expected: PASS.

Run:

```bash
uv run mypy --no-incremental src/service/crm/contacts src/service/crm/access/contacts_access_service.py src/api/routers/crm/contacts_handler.py src/agents/tools/crm/create_contact.py
```

Expected: PASS.

## Rollout Notes

- This is contact-only and should follow the account service-standardization slice.
- This does not migrate deals into `src/service/crm/deals/`.
- This does not change HubSpot or Salesforce contact sync writers.
- This does not add contact mutation events.
- This does not add workflow dispatcher tables or workers.
- This keeps contact-to-deal linking separate from pure contact writes.
- This preserves explicit `company_id` linking and domain fallback display behavior.
- This keeps stakeholder sync dispatch at router/tool orchestration until workflow dispatch exists.

## Loopholes This Plan Closes

- Contact create/update no longer commits inside the service while router/tool code also manages commits.
- Agent and API contact creation no longer use different write rules.
- Contact write service no longer silently owns deal participant creation.
- Contact-to-deal linking has a clear service boundary because it creates deal participants, not just contact data.
- Account lookup/create during contact creation uses the canonical account services after the account plan is implemented.
- Provider sync contact writers remain untouched so external CRM ingestion is not accidentally changed.
- Duplicate checks remain tenant-scoped and do not merge unrelated provider contacts by email.

## Success Criteria

- `src/service/crm/contacts/contracts.py` exists.
- `src/service/crm/contacts/read_service.py` exists and has no write methods.
- `src/service/crm/contacts/write_service.py` exists and owns internal contact create/update.
- `src/service/crm/contacts/deal_link_service.py` exists and owns contact-to-deal participant linking.
- `ContactsAccessService` no longer contains contact construction logic.
- Contact API reads call `ContactReadService`.
- Contact API writes call `ContactWriteService`.
- Contact API add-to-deal calls `ContactDealLinkService`.
- Agent `create_contact` uses contact/account read/write services instead of `ContactsAccessService`.
- Focused contact service, API, and agent tests pass.
- Focused ruff and mypy checks pass before PR.
