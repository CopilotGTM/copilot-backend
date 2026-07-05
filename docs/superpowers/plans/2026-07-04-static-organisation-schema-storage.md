# Static Organisation Schema Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move organisation-owned runtime data away from one physical schema per organisation and toward one static organisation data schema configured from Helm, while keeping `public` as the control-plane schema and preserving cross-schema foreign keys.

**Architecture:** Keep the existing master/organisation split: `public` owns users, organisations, auth/admin/control-plane tables; a single static organisation schema owns product tables. Reuse the existing master/org migration directories, `org_*` legacy schema naming for already-created tenants during backfill, and existing session helpers so service call sites do not churn.

**Tech Stack:** PostgreSQL, SQLModel/SQLAlchemy, Alembic, Helm values, pytest, existing CopilotGTM database providers.

---

## Boundaries

- Do not rename business tables to `tenant_*` or `shared_*`.
- Do not create new model folders named `tenant` or `shared`.
- Do not introduce user-organisation membership in this change.
- Do not change organisation display/name fields.
- Do not remove `public.users.organisation_id`.
- Do not change hundreds of services; route through existing DB/session helpers.
- Keep `public` for main/control-plane tables.
- Configure the static organisation schema name from Helm values for dev and prod.

## Target Shape

```text
public
  organisations
  users
  super_admins
  external_identity_mappings
  public_ingress_keys
  control-plane/admin/provisioning tables

organisation
  chat_sessions
  chat_messages
  crm_* tables
  integrations/product data
  workflow/product data
  knowledge/product data
```

`organisation` is the proposed static schema name. If we want a different literal, change only the Helm value before implementation.

Cross-schema foreign keys must be explicit:

```python
sa.ForeignKeyConstraint(
    ["organisation_id"],
    ["public.organisations.organisation_id"],
)
```

Every organisation-owned table must have:

```sql
organisation_id uuid not null references public.organisations(organisation_id)
```

and an index on `organisation_id` or the real access-path composite, for example `(organisation_id, user_id)`.

## Files

- Modify: `charts/application/values-dev.yaml`
  - Add `api.secrets.ORGANISATION_SCHEMA_NAME: "organisation"`.
- Modify: `charts/application/values-prod.yaml`
  - Add `api.secrets.ORGANISATION_SCHEMA_NAME: "organisation"`.
- Modify: `src/infra/utils/resource_naming.py`
  - Add a small helper to read the static schema env var.
  - Keep `get_schema_name(organisation_id)` for legacy source schemas and rollback.
- Modify: `src/infra/database/provider_async.py`
  - Make `tenant_session(org_id)` use the static organisation schema when configured.
  - Keep legacy per-org schema routing when the env var is absent or rollout flag is disabled.
- Modify: `src/infra/database/provider_sync.py`
  - Same behavior as async provider.
- Modify: `scripts/run_org_migrations.py`
  - Add a mode to run org migrations once against the static schema.
  - Keep existing per-org migration behavior for legacy schemas and rollback.
- Modify: `migrations/org_migrations/env.py`
  - Ensure the target schema can be static and does not require an organisation-derived name.
- Modify: `migrations/org_migrations/versions/*.py`
  - Add missing cross-schema FKs and indexes only where the table already has `organisation_id`.
  - Add missing `organisation_id` only after table inventory proves the table is organisation-owned.
- Create: `tests/infra/database/test_static_organisation_schema_routing.py`
  - Provider/session routing tests.
- Modify: `tests/scripts/test_run_org_migrations.py`
  - Static schema migration runner tests.
- Create: `docs/Technical Documentation/static-organisation-schema-rollout.md`
  - Operator rollout, backfill, verification, rollback.

## Task 0: Revert Earlier Over-Broad Tenant Storage Code

**Files:**
- Inspect: `git status --short`
- Revert only prior tenant-storage implementation files from the earlier attempt.
- Keep planning and documentation files unless they contradict this plan.

- [ ] **Step 1: Confirm current checkout**

Run:

```powershell
git -C D:\CopilotGTM\copilot-backend status --short --branch
```

Expected: shows unrelated user changes separately from tenant-storage files.

- [ ] **Step 2: Revert only abandoned implementation changes**

If the abandoned worktree still contains earlier changes such as membership tables, data-route tables, `TenantChatSession`, `SharedChatSession`, or domain routing helpers, revert those code/test/migration files before implementing this plan.

Do not revert:

```text
docs/Technical Documentation/two-schema-tenant-storage-plan.md
docs/superpowers/plans/2026-07-04-static-organisation-schema-storage.md
```

- [ ] **Step 3: Verify forbidden names are absent**

Run:

```powershell
rg "TenantChatSession|SharedChatSession|tenant_chat_sessions|shared_chat_sessions|user_organisation_memberships|organisation_data_routes" src migrations tests scripts
```

Expected: no matches, unless a match is only in an explanatory doc saying not to add it.

## Task 1: Add Static Schema Configuration

**Files:**
- Modify: `charts/application/values-dev.yaml`
- Modify: `charts/application/values-prod.yaml`
- Modify: `src/infra/utils/resource_naming.py`
- Test: `tests/infra/test_static_organisation_schema_config.py`

- [ ] **Step 1: Write config helper tests**

Create `tests/infra/test_static_organisation_schema_config.py`:

```python
from uuid import UUID

from src.infra.utils import resource_naming


def test_static_organisation_schema_name_uses_env(monkeypatch):
    monkeypatch.setenv("ORGANISATION_SCHEMA_NAME", "organisation")

    assert resource_naming.get_static_organisation_schema_name() == "organisation"


def test_static_organisation_schema_name_empty_env_is_disabled(monkeypatch):
    monkeypatch.setenv("ORGANISATION_SCHEMA_NAME", "")

    assert resource_naming.get_static_organisation_schema_name() is None


def test_legacy_schema_name_stays_available(monkeypatch):
    monkeypatch.delenv("ORGANISATION_SCHEMA_NAME", raising=False)
    org_id = UUID("11111111-2222-3333-4444-555555555555")

    assert (
        resource_naming.get_schema_name(org_id)
        == "org_11111111_2222_3333_4444_555555555555"
    )
```

- [ ] **Step 2: Run failing test**

Run:

```powershell
uv run pytest tests/infra/test_static_organisation_schema_config.py -q
```

Expected: fails because `get_static_organisation_schema_name` does not exist.

- [ ] **Step 3: Add minimal helper**

Modify `src/infra/utils/resource_naming.py`:

```python
def get_static_organisation_schema_name() -> str | None:
    schema_name = os.getenv("ORGANISATION_SCHEMA_NAME", "").strip()
    return schema_name or None
```

Keep `get_schema_name(organisation_id)` unchanged.

- [ ] **Step 4: Add Helm values**

Add to `charts/application/values-dev.yaml` under `api.secrets`:

```yaml
    ORGANISATION_SCHEMA_NAME: "organisation"
```

Add the same value to `charts/application/values-prod.yaml` under `api.secrets`:

```yaml
    ORGANISATION_SCHEMA_NAME: "organisation"
```

- [ ] **Step 5: Verify**

Run:

```powershell
uv run pytest tests/infra/test_static_organisation_schema_config.py -q
```

Expected: passes.

## Task 2: Route Existing Org Sessions To Static Schema

**Files:**
- Modify: `src/infra/database/provider_async.py`
- Modify: `src/infra/database/provider_sync.py`
- Test: `tests/infra/database/test_static_organisation_schema_routing.py`

- [ ] **Step 1: Write routing tests**

Create `tests/infra/database/test_static_organisation_schema_routing.py`:

```python
from uuid import UUID

from src.infra.utils.resource_naming import get_schema_name


def test_static_schema_env_wins_for_runtime_routing(monkeypatch):
    monkeypatch.setenv("ORGANISATION_SCHEMA_NAME", "organisation")

    from src.infra.utils.resource_naming import get_static_organisation_schema_name

    assert get_static_organisation_schema_name() == "organisation"


def test_legacy_schema_name_remains_for_backfill_and_rollback(monkeypatch):
    monkeypatch.setenv("ORGANISATION_SCHEMA_NAME", "organisation")
    org_id = UUID("11111111-2222-3333-4444-555555555555")

    assert get_schema_name(org_id) == "org_11111111_2222_3333_4444_555555555555"
```

If provider internals expose a schema-selection helper after implementation, extend this test to assert that runtime routing chooses `organisation`.

- [ ] **Step 2: Run failing or incomplete test**

Run:

```powershell
uv run pytest tests/infra/database/test_static_organisation_schema_routing.py -q
```

Expected: either fails before helper implementation or passes as a config-only guard. Add provider-level assertions after the helper exists.

- [ ] **Step 3: Implement provider schema selection**

In both providers, keep the current search-path setup but choose schema like this:

```python
from src.infra.utils.resource_naming import (
    get_schema_name,
    get_static_organisation_schema_name,
)


def _resolve_runtime_org_schema(org_id: UUID) -> str:
    return get_static_organisation_schema_name() or get_schema_name(org_id)
```

Use `_resolve_runtime_org_schema` inside `tenant_session`.

- [ ] **Step 4: Verify existing session call sites do not change**

Run:

```powershell
rg "acquire_org_async_session|org_async_session_scope|acquire_org_session|org_session_scope" src scripts tests
```

Expected: existing call sites still pass `organisation_id`; no broad service rewrite is needed.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
uv run pytest tests/infra/database/test_static_organisation_schema_routing.py tests/infra/test_tenant_search_path.py -q
```

Expected: passes.

## Task 3: Run Organisation Migrations Once For Static Schema

**Files:**
- Modify: `scripts/run_org_migrations.py`
- Modify: `migrations/org_migrations/env.py`
- Modify: `tests/scripts/test_run_org_migrations.py`

- [ ] **Step 1: Add tests for static migration target**

Add tests to `tests/scripts/test_run_org_migrations.py` proving:

```python
def test_static_schema_mode_uses_configured_schema(monkeypatch):
    monkeypatch.setenv("ORGANISATION_SCHEMA_NAME", "organisation")
    assert resolve_static_schema_name() == "organisation"
```

and:

```python
def test_static_schema_mode_does_not_require_org_id(monkeypatch):
    monkeypatch.setenv("ORGANISATION_SCHEMA_NAME", "organisation")
    targets = resolve_migration_targets(static_schema=True, org_ids=[])
    assert targets == ["organisation"]
```

Use the actual helper names created in `scripts/run_org_migrations.py`; keep them small and importable.

- [ ] **Step 2: Run failing tests**

Run:

```powershell
uv run pytest tests/scripts/test_run_org_migrations.py -q
```

Expected: fails because static schema mode does not exist.

- [ ] **Step 3: Add CLI mode**

Add an explicit option:

```text
--static-organisation-schema
```

Behavior:

```text
if --static-organisation-schema:
    target schema = ORGANISATION_SCHEMA_NAME
    run org migrations exactly once
else:
    keep existing per-org behavior
```

If `ORGANISATION_SCHEMA_NAME` is empty, fail with a clear error.

- [ ] **Step 4: Ensure Alembic receives the target schema**

Keep the existing Alembic `-x schema=<schema>` path. The static mode should pass:

```text
schema=organisation
```

- [ ] **Step 5: Verify**

Run:

```powershell
uv run pytest tests/scripts/test_run_org_migrations.py -q
```

Expected: passes.

## Task 4: Add Cross-Schema FK/Index Migration Rules

**Files:**
- Inspect: `migrations/org_migrations/versions/*.py`
- Modify: only migration files needed for missing `organisation_id` FK/indexes.
- Test: create a migration contract test if missing.

- [ ] **Step 1: Inventory organisation-owned tables**

Run:

```powershell
rg "sa.Column\\(\"organisation_id\"|ForeignKeyConstraint\\(.*organisation_id|ix_.*organisation_id" migrations/org_migrations/versions -n
```

Expected: list tables with `organisation_id`, indexes, and missing FK constraints.

- [ ] **Step 2: Write migration contract test**

Create or extend a test that scans org migrations and asserts every table with `organisation_id` has:

```text
public.organisations.organisation_id
```

or is explicitly allowlisted with a reason.

- [ ] **Step 3: Run failing contract test**

Run:

```powershell
uv run pytest tests/migrations -q
```

Expected: fails for tables missing cross-schema FK/index coverage.

- [ ] **Step 4: Add minimal FK/index changes**

For each organisation-owned table, add:

```python
sa.ForeignKeyConstraint(
    ["organisation_id"],
    ["public.organisations.organisation_id"],
    ondelete="CASCADE",
)
```

Use `ondelete="CASCADE"` only when deleting an organisation should delete that product data. Use `RESTRICT` or omit `ondelete` where deletion must be blocked or handled manually.

Add indexes only for actual access paths:

```python
op.create_index(
    op.f("ix_<table>_organisation_id"),
    "<table>",
    ["organisation_id"],
    unique=False,
)
```

- [ ] **Step 5: Verify**

Run:

```powershell
uv run pytest tests/migrations -q
```

Expected: passes.

## Task 5: Backfill Plan, Not Backfill Code Yet

**Files:**
- Create: `docs/Technical Documentation/static-organisation-schema-rollout.md`

- [ ] **Step 1: Document source and target**

Add:

```text
Source: existing per-org schemas from get_schema_name(organisation_id)
Target: static schema from ORGANISATION_SCHEMA_NAME
Key: organisation_id on every copied row
```

- [ ] **Step 2: Document copy order**

Order must follow FK dependencies:

```text
parent/reference product tables first
child tables second
event/log tables last
```

- [ ] **Step 3: Document rollback**

Rollback is:

```text
unset ORGANISATION_SCHEMA_NAME or disable static routing flag
route sessions back to get_schema_name(organisation_id)
keep old per-org schemas read-only until cutover is proven
```

- [ ] **Step 4: Document verification SQL**

Include:

```sql
SELECT organisation_id, count(*) FROM organisation.chat_sessions GROUP BY organisation_id;
SELECT conname FROM pg_constraint WHERE confrelid = 'public.organisations'::regclass;
```

## Task 6: Final Verification

**Files:**
- All changed files.

- [ ] **Step 1: Static scan for forbidden implementation**

Run:

```powershell
rg "TenantChatSession|SharedChatSession|tenant_chat_sessions|shared_chat_sessions|user_organisation_memberships|organisation_data_routes" src migrations tests scripts
```

Expected: no runtime/code matches.

- [ ] **Step 2: Focused tests**

Run:

```powershell
uv run pytest tests/infra/test_static_organisation_schema_config.py tests/infra/database/test_static_organisation_schema_routing.py tests/scripts/test_run_org_migrations.py tests/migrations -q
```

Expected: passes.

- [ ] **Step 3: Lint touched Python files**

Run:

```powershell
uv run ruff check src/infra/utils/resource_naming.py src/infra/database/provider_async.py src/infra/database/provider_sync.py scripts/run_org_migrations.py tests/infra/test_static_organisation_schema_config.py tests/infra/database/test_static_organisation_schema_routing.py tests/scripts/test_run_org_migrations.py
```

Expected: passes.

- [ ] **Step 4: Helm render check**

Run:

```powershell
helm template copilot-backend charts/application -f charts/application/values.yaml -f charts/application/values-dev.yaml
```

Expected: rendered API secret contains `ORGANISATION_SCHEMA_NAME`.

Run:

```powershell
helm template copilot-backend charts/application -f charts/application/values.yaml -f charts/application/values-prod.yaml
```

Expected: rendered API secret contains `ORGANISATION_SCHEMA_NAME`.

## Self-Review

- This plan reuses existing master/org migrations and session helpers.
- It keeps `public.users.organisation_id` and defers membership.
- It uses a static schema name from Helm values instead of organisation-derived schema names for runtime.
- It keeps legacy per-org schema naming available for backfill and rollback.
- It does not require service-wide renames or table renames.
- It preserves cross-schema FK support as the core condition for the two-schema design.
