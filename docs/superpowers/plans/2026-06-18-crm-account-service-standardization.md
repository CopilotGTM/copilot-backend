# CRM Account Service Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the first vertical CRM entity module for accounts and make account writes flow through one canonical `AccountWriteService`.

**Architecture:** Implement `src/service/crm/accounts/` from the current checkout, not from stashed experimental services. `AccountWriteService` is extracted from the write methods currently inside `AccountsAccessService` plus account materialization helpers currently embedded in `DealWriteService`; `AccountReadService` is extracted from the read/search/detail methods currently inside `AccountsAccessService`. Existing old files stay during migration as compatibility surfaces until production call sites no longer depend on them.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, pytest, mypy, ruff.

---

## Corrected Starting Point

This plan is based on the current checkout after the stash cleanup.

Current facts:

- `src/service/crm/internal/account_write_service.py` does **not** exist in `HEAD`.
- `src/service/crm/internal/account_materialization_service.py` does **not** exist in `HEAD`.
- `tests/service/crm/test_account_write_service.py` does **not** exist in `HEAD`.
- Account read and basic API write behavior currently lives in `src/service/crm/access/accounts_access_service.py`.
- Deal-specific account create/link/copy behavior currently lives directly inside `src/service/crm/internal/deal_write_service.py`.
- Stashes may contain older experimental account service files, but they are reference material only. Do not restore them blindly.
- Unrelated staged CRM mutation-event table/model changes must be preserved and not rewritten by this account slice.

## Current Problem

Account behavior is scattered by caller instead of owned by one account domain module.

Current production risks:

- API account creation uses `AccountsAccessService.create_account()`.
- API account update uses `AccountsAccessService.update_account()`.
- Deal creation/update can create, copy, and link companies through private helpers inside `DealWriteService`.
- Future agents/workflows/WhatsApp actions would have to choose between access services and deal-private helpers.
- `AccountsAccessService` mixes read/query behavior with writes, which makes the service boundary unclear.
- `DealWriteService` owns account construction even though account construction should be an account-domain rule.

## Future Folder Pattern

Use vertical CRM entity modules. This plan implements only accounts.

```text
src/service/crm/
  accounts/
    __init__.py
    contracts.py
    read_service.py
    write_service.py
  access/
    accounts_access_service.py        # temporary compatibility surface during migration
  internal/
    deal_write_service.py             # keeps deal orchestration, delegates account writes
```

Future CRM direction after accounts:

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
  deals/
    contracts.py
    read_service.py
    write_service.py
    mutation_policy.py
  shared/
    entity_normalizers.py
    materialization_errors.py
```

Do not move contacts or deals in this plan.

## Target Account Boundaries

Write callers should converge here:

```text
API account create/update         -> AccountWriteService
DealWriteService account helpers  -> AccountWriteService
future agent account actions      -> AccountWriteService
future workflow account actions   -> AccountWriteService
future WhatsApp/chat account write-> AccountWriteService
```

Read callers should converge here:

```text
API account list/detail/search    -> AccountReadService
agent account search              -> AccountReadService
dropdown suggestions              -> AccountReadService
```

Temporary compatibility:

```text
AccountsAccessService             -> delegates to AccountReadService + AccountWriteService
```

The compatibility surface exists only to keep migration smooth. New code must not import it.

## File Responsibilities

### `src/service/crm/accounts/contracts.py`

Defines account-domain DTOs and exceptions:

- `AccountWriteContext`
- `AccountWriteResult`
- `AccountCreateMode`
- `AccountDuplicateScope`
- `AccountMatchedBy`
- `AccountWriteServiceError`
- `AccountReadServiceError`

### `src/service/crm/accounts/read_service.py`

Owns read-only account behavior extracted from current `AccountsAccessService`:

- `suggest_account_by_domain()`
- `find_accounts_by_domain()`
- `find_accounts_by_name()`
- `list_accounts()`
- `get_account_detail()`
- `list_account_contacts()`
- `get_account_contacts()`
- `get_account_deals()`

It must not construct `CRMCompany(...)`.

### `src/service/crm/accounts/write_service.py`

Owns account writes extracted from current `AccountsAccessService` and current `DealWriteService`:

- `create_account()`
- `update_account()`
- `upsert_company_from_account()`
- `ensure_internal_company_from_source()`
- `load_org_company()`
- `get_account_audit_snapshot()`
- `_get_or_create_internal_integration()`
- domain/name normalization helpers needed by account writes

It must be the only CRM service file that constructs `CRMCompany(...)` for internal account writes.

### `src/service/crm/access/accounts_access_service.py`

Temporary compatibility facade:

- May remain during this migration.
- Should delegate reads to `AccountReadService`.
- Should delegate writes to `AccountWriteService`.
- Should not contain `CRMCompany(...)` construction after the migration.
- Should be marked deprecated in a docstring/comment once delegation is complete.

### `src/service/crm/internal/deal_write_service.py`

Keeps deal orchestration:

- deal creation/update/lifecycle behavior
- participant replacement/linking
- deal snapshot updates

Delegates account construction/copy/upsert to `AccountWriteService`.

## Non-Negotiable Rules

- Do not restore stashed account services as the implementation source.
- Do not delete `AccountsAccessService` in the first implementation step.
- New account code must live under `src/service/crm/accounts/`.
- New production writes must use `src.service.crm.accounts.write_service.AccountWriteService`.
- New production reads must use `src.service.crm.accounts.read_service.AccountReadService`.
- `AccountReadService` must not expose `create_account()` or `update_account()`.
- `AccountWriteService` must not own transaction commit; caller owns commit/rollback.
- Every account query/write must filter or set `organisation_id`.
- Account duplicate checks must stay tenant-scoped.
- Do not change provider sync writers in this plan.
- Do not add account mutation events in this plan; leave that as a later phase.

## Migration Strategy

Use an additive migration first, then route callers, then deprecate old paths.

1. Create `src/service/crm/accounts/` with contracts, read service, and write service.
2. Extract read behavior from `AccountsAccessService` into `AccountReadService`.
3. Extract account write behavior from `AccountsAccessService` into `AccountWriteService`.
4. Extract deal account materialization helpers from `DealWriteService` into `AccountWriteService`.
5. Update `DealWriteService` to delegate account writes to `AccountWriteService`.
6. Update account API handlers to depend on `AccountReadService` for reads and `AccountWriteService` for writes.
7. Convert `AccountsAccessService` into a compatibility facade or leave it only for unmigrated read callers.
8. Add boundary tests to prevent new account writes from appearing outside `AccountWriteService`.
9. In a later cleanup PR, move/delete the compatibility facade only after `rg` proves there are no production imports.

## Task 0: Add Account Module And Contracts

**Files:**

- Create: `src/service/crm/accounts/__init__.py`
- Create: `src/service/crm/accounts/contracts.py`
- Create: `tests/service/crm/test_account_service_boundaries.py`

- [ ] **Step 1: Create the boundary test file**

Create `tests/service/crm/test_account_service_boundaries.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def test_account_vertical_module_exists() -> None:
    assert Path("src/service/crm/accounts/__init__.py").exists()
    assert Path("src/service/crm/accounts/contracts.py").exists()
    assert Path("src/service/crm/accounts/read_service.py").exists()
    assert Path("src/service/crm/accounts/write_service.py").exists()


def test_account_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/accounts/read_service.py").read_text()

    assert "async def create_account" not in source
    assert "async def update_account" not in source
    assert "CRMCompany(" not in source


def test_account_write_service_is_only_crm_service_company_constructor() -> None:
    result = subprocess.run(
        [
            "rg",
            "-n",
            "CRMCompany\\(",
            "src/service/crm/access",
            "src/service/crm/internal/deal_write_service.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    unexpected = [
        line
        for line in result.stdout.splitlines()
        if "src/service/crm/accounts/write_service.py" not in line.replace("\\", "/")
    ]
    assert unexpected == []
```

- [ ] **Step 2: Run the boundary test and verify it fails**

Run:

```bash
uv run pytest tests/service/crm/test_account_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/accounts/` does not exist yet.

- [ ] **Step 3: Create account package**

Create `src/service/crm/accounts/__init__.py`:

```python
"""Account read and write services for the internal CRM."""
```

- [ ] **Step 4: Create account contracts**

Create `src/service/crm/accounts/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany

AccountCreateMode = Literal["strict", "resolve_or_create"]
AccountDuplicateScope = Literal["strict_org", "internal_only", "resolve_or_create"]
AccountMatchedBy = Literal["domain", "name", "explicit_id", "copied_source", "created"]


@dataclass(frozen=True)
class AccountWriteContext:
    """Caller metadata for duplicate policy and future account audit events."""

    source: str
    actor_user_id: UUID | None = None
    source_label: str | None = None
    created_via: str | None = None
    duplicate_scope: AccountDuplicateScope = "internal_only"


@dataclass(frozen=True)
class AccountWriteResult:
    """Canonical account write result returned to API, agent, workflow, and deal callers."""

    account: CRMCompany
    created: bool
    matched_by: AccountMatchedBy


class AccountWriteServiceError(Exception):
    """HTTP-shaped account write error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class AccountReadServiceError(Exception):
    """HTTP-shaped account read error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
```

## Task 1: Extract AccountReadService From Current AccountsAccessService

**Files:**

- Create: `src/service/crm/accounts/read_service.py`
- Modify later: `src/service/crm/access/accounts_access_service.py`
- Test: `tests/service/crm/test_account_service_boundaries.py`

- [ ] **Step 1: Copy read-only behavior into AccountReadService**

Create `src/service/crm/accounts/read_service.py` from the read-only parts of current `src/service/crm/access/accounts_access_service.py`.

The class name must be:

```python
class AccountReadService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Move these methods without changing behavior:

```python
close
_normalize_domain
suggest_account_by_domain
find_accounts_by_domain
find_accounts_by_name
list_accounts
get_account_detail
list_account_contacts
get_account_contacts
get_account_deals
```

Replace `AccountsAccessServiceError` with `AccountReadServiceError` from:

```python
from src.service.crm.accounts.contracts import AccountReadServiceError
```

- [ ] **Step 2: Remove write-only imports from AccountReadService**

`src/service/crm/accounts/read_service.py` must not import:

```python
datetime
uuid4
IntegrationScope
IntegrationType
KnowledgeIntegration
```

unless one is still required by read-only code.

- [ ] **Step 3: Verify read service boundary**

Run:

```bash
uv run pytest tests/service/crm/test_account_service_boundaries.py::test_account_read_service_does_not_expose_writes -q
```

Expected: PASS after `read_service.py` exists and contains no write methods.

## Task 2: Create AccountWriteService From Current Write Sources

**Files:**

- Create: `src/service/crm/accounts/write_service.py`
- Modify later: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_account_write_service.py`

- [ ] **Step 1: Create write-service tests for current API account writes**

Create `tests/service/crm/test_account_write_service.py` with tests covering:

```python
async def test_create_account_rejects_duplicate_domain(...)
async def test_create_account_rejects_duplicate_name_when_domain_missing(...)
async def test_create_account_normalizes_domain(...)
async def test_update_account_rejects_cross_org_or_deleted_account(...)
async def test_update_account_ignores_unknown_fields(...)
```

Use the same model fixtures already used by existing account/deal tests. The expected behavior should match current `AccountsAccessService.create_account()` and `AccountsAccessService.update_account()`.

- [ ] **Step 2: Implement AccountWriteService create/update behavior**

Create `src/service/crm/accounts/write_service.py` with:

```python
class AccountWriteService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
```

Extract these behaviors from current `AccountsAccessService`:

```python
_normalize_domain
find_accounts_by_domain
find_accounts_by_name
create_account
update_account
_get_or_create_internal_integration
```

`create_account()` should return `AccountWriteResult`, not raw `CRMCompany`.

`update_account()` may return raw `CRMCompany` for now because callers update one existing account. If account mutation events are added later, wrap this in a richer result then.

- [ ] **Step 3: Run write-service tests**

Run:

```bash
uv run pytest tests/service/crm/test_account_write_service.py -q
```

Expected: PASS.

## Task 3: Move Deal Account Materialization Into AccountWriteService

**Files:**

- Modify: `src/service/crm/accounts/write_service.py`
- Modify: `src/service/crm/internal/deal_write_service.py`
- Modify/Add: deal account tests under `tests/service/crm/`

- [ ] **Step 1: Add tests for deal account behavior before extraction**

Use existing deal account tests if present; otherwise add focused tests for:

```python
async def test_deal_create_with_account_creates_company_through_account_writer(...)
async def test_deal_update_account_reuses_existing_company_by_domain(...)
async def test_deal_update_account_copies_external_company_to_internal_company(...)
```

The assertion should prove `DealWriteService` no longer constructs `CRMCompany(...)` directly after extraction.

- [ ] **Step 2: Extract deal-private account helpers**

Move account-domain helper behavior from `src/service/crm/internal/deal_write_service.py` into `AccountWriteService`:

```python
upsert_company_from_account
load_org_company
ensure_internal_company_from_source
normalize_company_name
```

Keep deal-specific participant behavior in `DealWriteService`:

```python
_add_company_participant
_replace_company_participant
_has_active_company_participant
_sync_deal_company_snapshot
_apply_fallback_company_name
```

- [ ] **Step 3: Inject AccountWriteService into DealWriteService**

In `DealWriteService.__init__`, create:

```python
self._account_writer = AccountWriteService(self.session, self.organisation_id)
```

Replace internal calls:

```python
self._upsert_company_from_account(...)
self._load_org_company(...)
self._ensure_internal_company_from_source(...)
```

with:

```python
self._account_writer.upsert_company_from_account(...)
self._account_writer.load_org_company(...)
self._account_writer.ensure_internal_company_from_source(...)
```

- [ ] **Step 4: Verify constructor boundary**

Run:

```bash
uv run pytest tests/service/crm/test_account_service_boundaries.py::test_account_write_service_is_only_crm_service_company_constructor -q
```

Expected: PASS. This boundary intentionally scans account access code and
`DealWriteService` only. Provider sync and discovery writers are outside this
account-service standardization slice and must not be changed by this plan.

## Task 4: Route Account API Through New Services

**Files:**

- Modify: `src/api/routers/crm/accounts_handler.py`
- Modify: `tests/api/routers/crm/test_accounts_handler.py`

- [ ] **Step 1: Update read endpoints**

Account list/detail/search dependencies should use:

```python
from src.service.crm.accounts.read_service import AccountReadService
```

- [ ] **Step 2: Update create/update endpoints**

Account create/update dependencies should use:

```python
from src.service.crm.accounts.contracts import AccountWriteContext, AccountWriteServiceError
from src.service.crm.accounts.write_service import AccountWriteService
```

Create calls should pass:

```python
AccountWriteContext(
    source="api",
    actor_user_id=user_ids.user_id,
    created_via="accounts_app",
    duplicate_scope="strict_org",
)
```

Update calls should pass the same context.

- [ ] **Step 3: Keep transaction ownership in router**

After successful create and update writes:

```python
await service.session.commit()
```

The current account create endpoint already commits after the service write.
Account update must also commit after a successful service write so the migrated
write path does not preserve the existing no-commit bug. On error, preserve
existing rollback/error response behavior.

- [ ] **Step 4: Run account router tests**

Run:

```bash
uv run pytest tests/api/routers/crm/test_accounts_handler.py -q
```

Expected: PASS.

## Task 5: Convert AccountsAccessService Into Compatibility Facade

**Files:**

- Modify: `src/service/crm/access/accounts_access_service.py`
- Test: `tests/service/crm/test_account_service_boundaries.py`

- [ ] **Step 1: Replace implementation with delegation**

Keep the public class for compatibility:

```python
class AccountsAccessService:
    """Deprecated compatibility facade; use AccountReadService or AccountWriteService."""
```

Delegate reads to:

```python
self._reader = AccountReadService(org_session, organisation_id)
```

Delegate writes to:

```python
self._writer = AccountWriteService(org_session, organisation_id)
```

Do not keep account construction logic in this file.

- [ ] **Step 2: Preserve old exception import compatibility**

For now, alias:

```python
AccountsAccessServiceError = AccountReadServiceError
```

If write callers still catch `AccountsAccessServiceError`, translate `AccountWriteServiceError` into `AccountsAccessServiceError` inside the facade.

- [ ] **Step 3: Verify no account construction remains in access service**

Run:

```bash
rg -n "CRMCompany\\(" src/service/crm/access/accounts_access_service.py
```

Expected: no output.

## Task 6: Focused Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Verify service boundaries**

Run:

```bash
uv run pytest tests/service/crm/test_account_service_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify account writer**

Run:

```bash
uv run pytest tests/service/crm/test_account_write_service.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify account API**

Run:

```bash
uv run pytest tests/api/routers/crm/test_accounts_handler.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify deal account flows**

Run the focused deal account tests that exist in this checkout:

```bash
uv run pytest tests/service/crm/test_deal_write_service_account_copy.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected: PASS. If either file does not exist in the current checkout, first add the missing focused tests around the extracted behavior.

- [ ] **Step 5: Run focused lint and type checks before PR**

Run:

```bash
uv run ruff check src/service/crm/accounts src/service/crm/access/accounts_access_service.py src/service/crm/internal/deal_write_service.py src/api/routers/crm/accounts_handler.py tests/service/crm/test_account_service_boundaries.py tests/service/crm/test_account_write_service.py tests/api/routers/crm/test_accounts_handler.py
```

Expected: PASS.

Run:

```bash
uv run mypy --no-incremental src/service/crm/accounts src/service/crm/access/accounts_access_service.py src/service/crm/internal/deal_write_service.py src/api/routers/crm/accounts_handler.py
```

Expected: PASS.

## Later Cleanup Phase

Only after this account slice is merged and call sites are stable:

1. Run:

   ```bash
   rg -n "AccountsAccessService|accounts_access_service" src tests
   ```

2. If no production imports remain, move the compatibility facade to a deprecated path or delete it.
3. Repeat the same vertical-module pattern for contacts.
4. Repeat the same vertical-module pattern for deals.
5. Add account mutation events only after the account write service boundary is stable.

## Rollout Notes

- This is account-only.
- This does not migrate contacts into `src/service/crm/contacts/`.
- This does not migrate deals into `src/service/crm/deals/`.
- This does not change provider sync writers.
- This does not add account mutation events.
- This does not touch the staged CRM mutation-event schema/model slice except where tests require import compatibility.
- Stashed account service files may be inspected as reference, but current `HEAD` is the implementation source of truth.

## Success Criteria

- `src/service/crm/accounts/contracts.py` exists.
- `src/service/crm/accounts/read_service.py` exists and has no write methods.
- `src/service/crm/accounts/write_service.py` exists and owns account writes.
- `DealWriteService` no longer constructs `CRMCompany(...)` directly.
- `AccountsAccessService` no longer contains account construction logic.
- Account API writes call `AccountWriteService`.
- Account API reads call `AccountReadService`.
- Focused account, API, and deal account tests pass.
- Focused ruff and mypy checks pass before PR.
