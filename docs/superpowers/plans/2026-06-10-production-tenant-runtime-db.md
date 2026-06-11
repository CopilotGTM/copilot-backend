# Production Tenant Runtime Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current per-org tenant DB runtime path with a production-grade dedicated tenant runtime login loaded from Key Vault, while keeping per-org schemas and per-org roles available for rollback.

**Architecture:** Use two clean database identities: master/admin credentials for master and provisioning work, and one dedicated tenant runtime login for org-schema application queries. Tenant sessions must set transaction-scoped tenant context before any tenant query, using `SET LOCAL search_path` inside the provider-owned transaction boundary. The tenant runtime `search_path` contains only the tenant schema plus a locked trusted `extensions` schema; `public` is never in the tenant runtime path. The tenant runtime password is an environment-level secret stored in the master/shared Key Vault, and existing per-org roles/passwords remain untouched in the first release.

**Tech Stack:** Python 3.11, SQLAlchemy async/sync sessions, PostgreSQL schemas and roles, PgBouncer transaction pooling, Azure Key Vault, pytest, Ruff.

---

## Why This Plan Exists

The previous implementation moved in the correct direction but was still a patch over the current provider. If this were written from scratch, the shape should be simpler:

```text
master provider
  -> DATABASE_USER / DATABASE_PASSWORD
  -> public schema and admin/provisioning operations

tenant runtime provider
  -> TENANT_DATABASE_USER / master Key Vault password
  -> one shared tenant engine per process
  -> transaction-scoped tenant schema selection

provisioning/backfill
  -> creates or verifies dedicated tenant runtime role
  -> grants it least-privilege access to each org schema
  -> does not delete per-org roles, passwords, or Key Vault secrets
```

This is a baby-step production implementation. It does not attempt the later single-schema `organisation_id` model yet.

## External Constraints

These are design constraints, not opinions:

- PgBouncer transaction pooling releases the server connection after each transaction. Pooling also scales better when all clients use the same database user instead of many per-org users. Source: https://www.pgbouncer.org/config.html
- PostgreSQL `SET LOCAL` lasts only until the end of the current transaction. That is the correct primitive for PgBouncer transaction pooling. Source: https://www.postgresql.org/docs/current/sql-set.html
- PostgreSQL `SET ROLE` can change effective permissions, but it is still role state and still requires membership. It does not remove the need to select the tenant schema. Source: https://www.postgresql.org/docs/current/sql-set-role.html
- PostgreSQL `search_path` resolves unqualified object names and trusts schemas on the path. Writable shared schemas in `search_path` are unsafe. Source: https://www.postgresql.org/docs/current/ddl-schemas.html
- PostgreSQL extensions such as pgvector expose types/operators through a schema. Because this repo uses pgvector casts such as `::vector`, the tenant runtime path must include a trusted non-writable extension schema instead of `public`.
- PostgreSQL Row Level Security is the right future guardrail for shared-schema tenant rows; when enabled with no policy, it defaults to deny. Source: https://www.postgresql.org/docs/current/ddl-rowsecurity.html

## Explicit Decisions

1. Do not use the master DB user/password for tenant runtime.
   Verification: provider tests must fail if `TENANT_DATABASE_USER == DATABASE_USER`.

2. Use one dedicated shared tenant runtime login.
   Verification: two org IDs return the same tenant runtime engine in the same process.

3. Load the tenant runtime password through one credential contract.
   Verification: when `TENANT_DATABASE_PASSWORD` is empty and `TENANT_DATABASE_PASSWORD_SECRET_NAME` is set, the async provider calls `master_secrets_manager` exactly once during engine creation, and the sync provider uses `src.infra.secrets.master_key_vault_sync` with the shared Azure credential strategy and without `asyncio.run()` in the provider. The provisioning script writes to `AZURE_MASTER_KEY_VAULT_URL`, not the org-specific vault. When `TENANT_DATABASE_PASSWORD` is supplied directly, provisioning must not require a Key Vault client.

4. Keep per-org roles and passwords for rollback.
   Verification: no maintenance script deletes Key Vault secrets, drops per-org roles, or sets per-org roles to `NOLOGIN`.

5. Use `SET LOCAL search_path` inside provider-controlled transaction begin.
   Verification: a query after `commit()` in the same acquired tenant session starts a new transaction and reapplies tenant context. The runtime path is `SET LOCAL search_path TO "<org_schema>", "extensions"`, without `public`.

6. Do not use `SET ROLE` in v1 runtime.
   Verification: provider code has no runtime `SET ROLE`; a future hardening plan can convert per-org roles to `NOLOGIN PASSWORD NULL`.

7. Runtime must fail closed without provider tenant context.
   Verification: the provisioning script revokes unnecessary `CREATE` on `public`, sets the runtime role default `search_path` to `pg_catalog` for the target database only, and the lower-env rollout checklist proves unqualified tenant table access fails unless a tenant session sets provider-owned `SET LOCAL search_path`.

8. Runtime role password rotation must be explicit.
   Verification: re-running provisioning against an existing role does not change the PostgreSQL password or Key Vault secret unless `--rotate-password` is passed. When creating or rotating a password, the script must write the PostgreSQL password SQL inside an open transaction and store the Key Vault secret before that transaction exits, so a Key Vault failure rolls back the DB password change. PostgreSQL commit failure after Key Vault success remains the one non-atomic edge and must be called out in the rollback checklist.

9. Keep trusted extension objects out of `public`.
   Verification: provisioning creates or verifies an `extensions` schema, revokes `CREATE` on it from `PUBLIC`, grants `USAGE` to the tenant runtime role, grants `USAGE` to per-org owner roles only for rollback extension lookup, and attempts to ensure the `vector` extension lives in `extensions` where PostgreSQL supports moving it. Shared tenant runtime sessions include `extensions` only so pgvector type casts/operators work without trusting `public`. Org Alembic migrations also include `extensions` in `search_path` so unqualified pgvector `VECTOR` types keep resolving.

10. Shared-tenant misconfiguration must fail at process startup.
    Verification: FastAPI lifespan and the main Celery worker startup path validate both config and actual tenant runtime password availability when `USE_SHARED_TENANT_DATABASE=true`; missing tenant runtime user/password secret config, master credential reuse, or missing Key Vault secret fails before serving requests or running jobs. Celery prefork child init remains config-only to avoid blocking child startup on Key Vault/auth latency.

## Files

- Modify: `src/infra/config/configs.py`
  - Add explicit tenant runtime credential config.
- Create: `src/infra/database/tenant_runtime_config.py`
  - Validate tenant runtime config, prevent master credential reuse, and expose startup validation helpers.
- Create: `src/infra/database/tenant_runtime_credentials.py`
  - Resolve tenant runtime password from env or master/shared Key Vault for async and sync callers.
- Create: `src/infra/secrets/azure_credentials.py`
  - Share the sync Azure credential selection used by Key Vault callers.
- Create: `src/infra/secrets/master_key_vault_sync.py`
  - Build sync master/shared Key Vault clients and fetch master vault secrets without direct provider-local credential construction.
- Modify: `src/infra/database/provider_async.py`
  - Use exactly one tenant runtime async engine, kept in `_shared_tenant_engine`.
  - Remove runtime per-org Key Vault password fetch from shared mode.
- Modify: `src/infra/database/provider_sync.py`
  - Use exactly one tenant runtime sync engine, kept in `_shared_tenant_engine`.
  - Keep sync code from calling async APIs unsafely inside running loops.
- Create: `src/infra/database/tenant_search_path.py`
  - Own all transaction-scoped `SET LOCAL search_path` behavior, including the trusted `extensions` schema.
- Delete: `src/infra/database/tenant_context.py`
  - Remove the patch-shaped predecessor after callers move to `tenant_search_path.py`.
- Create: `src/infra/database/tenant_runtime_grants.py`
  - Own least-privilege runtime grants for org schemas.
- Verify/modify: `src/infra/utils/async_runner.py`
  - Ensure fork/loop reset clears `_shared_tenant_engine`; do not rename this field without updating cleanup tests.
- Create: `scripts/maintenance/provision_tenant_runtime_role.py`
  - Create/update dedicated tenant runtime role, store password in the master/shared Key Vault, and provision locked extension schema access.
- Create: `scripts/maintenance/grant_tenant_runtime_schema_access.py`
  - Dry-run-first grant backfill across existing org schemas.
- Delete: `scripts/maintenance/grant_shared_tenant_role.py`
  - Replace the patch-shaped combined script with focused provision and grant scripts.
- Modify: `src/service/orgnisation/organization_management.py`
  - Grant tenant runtime role access for newly provisioned org schemas after migrations.
- Modify: `src/api/fastapi_app.py`
  - Fail API startup when shared tenant runtime mode is enabled with invalid config or unavailable password secret.
- Modify: `src/workers/base/celery_base.py`
  - Fail shared worker startup when shared tenant runtime mode is enabled with invalid config or unavailable password secret.
- Create: `tests/infra/test_tenant_runtime_config.py`
- Create: `tests/infra/test_tenant_runtime_credentials.py`
- Create: `tests/infra/secrets/test_master_key_vault_sync.py`
- Modify: `tests/infra/test_async_db_provider.py`
- Create: `tests/infra/test_sync_db_provider.py`
- Create: `tests/infra/test_tenant_search_path.py`
- Create: `tests/infra/test_tenant_runtime_grants.py`
- Create: `tests/api/test_fastapi_app_tenant_runtime_startup.py`
- Create: `tests/workers/test_celery_tenant_runtime_startup.py`
- Create: `tests/scripts/maintenance/test_provision_tenant_runtime_role.py`
- Create: `tests/scripts/maintenance/test_grant_tenant_runtime_schema_access.py`
- Create: `tests/scripts/maintenance/test_check_tenant_runtime_bypasses.py`
- Modify: `docs/Technical Documentation/search-path-tenant-pooling.md`

## Task 0: Remove Patch-Shaped Tenant Runtime Artifacts

**Files:**
- Delete: `src/infra/database/tenant_context.py`
- Delete: `scripts/maintenance/grant_shared_tenant_role.py`
- Modify later tasks to import from `src.infra.database.tenant_search_path`

- [ ] **Step 1: Inspect current patch-shaped files**

Run:

```powershell
Get-Content src\infra\database\tenant_context.py
Get-Content scripts\maintenance\grant_shared_tenant_role.py
```

Expected: both files exist only as the earlier partial implementation and overlap with this plan's `tenant_search_path.py`, `provision_tenant_runtime_role.py`, and `grant_tenant_runtime_schema_access.py`.

- [ ] **Step 2: Move useful behavior into planned files only**

Do not keep compatibility imports from `tenant_context.py`. When Task 3 and Tasks 4-5 are implemented, provider imports must point directly at:

```python
from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    install_tenant_transaction_context_listener,
)
```

Expected: the old module is no longer needed by runtime code.

- [ ] **Step 3: Delete stale patch files after replacements are wired**

Run only after Task 3, Task 4, Task 5, Task 6, and Task 7 are implemented:

```powershell
Remove-Item src\infra\database\tenant_context.py
Remove-Item scripts\maintenance\grant_shared_tenant_role.py
```

Expected: files are removed after all provider imports and maintenance-script references have moved to the new modules. Do not use `git restore` or `git reset`; these were generated patch artifacts and are superseded by this plan.

- [ ] **Step 4: Verify no stale imports or script references remain**

Run:

```powershell
rg "tenant_context|grant_shared_tenant_role" src tests scripts docs
```

Expected: no matches.

## Task 1: Tenant Runtime Config Contract

**Files:**
- Modify: `src/infra/config/configs.py`
- Create: `src/infra/database/tenant_runtime_config.py`
- Test: `tests/infra/test_tenant_runtime_config.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

import pytest

from src.infra.database.tenant_runtime_config import (
    TenantRuntimeConfigError,
    validate_shared_tenant_runtime_startup,
    validate_tenant_runtime_config,
)


def test_rejects_master_database_user_for_tenant_runtime() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="must not equal DATABASE_USER"):
        validate_tenant_runtime_config(
            database_user="postgres",
            tenant_database_user="postgres",
            tenant_password="tenant-password",
            tenant_password_secret_name="shared-tenant-db-password",
        )


def test_requires_password_or_secret_name() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="password or Key Vault secret"):
        validate_tenant_runtime_config(
            database_user="postgres",
            tenant_database_user="app_tenant_runtime",
            tenant_password="",
            tenant_password_secret_name="",
        )


def test_accepts_key_vault_secret_without_inline_password() -> None:
    result = validate_tenant_runtime_config(
        database_user="postgres",
        tenant_database_user="app_tenant_runtime",
        tenant_password="",
        tenant_password_secret_name="shared-tenant-db-password",
    )

    assert result.user == "app_tenant_runtime"
    assert result.password == ""
    assert result.password_secret_name == "shared-tenant-db-password"


def test_startup_validation_skips_when_shared_mode_disabled() -> None:
    result = validate_shared_tenant_runtime_startup(
        use_shared_tenant_database=False,
        database_user="postgres",
        tenant_database_user="",
        tenant_password="",
        tenant_password_secret_name="",
    )

    assert result is None


def test_startup_validation_fails_when_shared_mode_reuses_master_user() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="must not equal DATABASE_USER"):
        validate_shared_tenant_runtime_startup(
            use_shared_tenant_database=True,
            database_user="postgres",
            tenant_database_user="postgres",
            tenant_password="",
            tenant_password_secret_name="shared-tenant-db-password",
        )
```

- [ ] **Step 2: Run tests to verify red**

Run:

```powershell
uv run pytest tests\infra\test_tenant_runtime_config.py -v
```

Expected: fail because `src.infra.database.tenant_runtime_config` does not exist.

- [ ] **Step 3: Implement config object**

Verify these existing fields remain in `src/infra/config/configs.py` inside `DatabaseConfig`:

```python
TENANT_DATABASE_USER: str = os.getenv("TENANT_DATABASE_USER", "")
TENANT_DATABASE_PASSWORD: str = os.getenv("TENANT_DATABASE_PASSWORD", "")
TENANT_DATABASE_PASSWORD_SECRET_NAME: str = os.getenv(
    "TENANT_DATABASE_PASSWORD_SECRET_NAME",
    "shared-tenant-db-password",
)
```

Do not add duplicate config fields. These may already exist from the earlier patch-shaped
implementation; preserve them and move validation into the new runtime config module.

Create `src/infra/database/tenant_runtime_config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


class TenantRuntimeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantRuntimeConfig:
    user: str
    password: str
    password_secret_name: str


def validate_tenant_runtime_config(
    *,
    database_user: str,
    tenant_database_user: str,
    tenant_password: str,
    tenant_password_secret_name: str,
) -> TenantRuntimeConfig:
    if not tenant_database_user:
        raise TenantRuntimeConfigError("TENANT_DATABASE_USER is required")
    if tenant_database_user == database_user:
        raise TenantRuntimeConfigError("TENANT_DATABASE_USER must not equal DATABASE_USER")
    if not tenant_password and not tenant_password_secret_name:
        raise TenantRuntimeConfigError(
            "TENANT_DATABASE_PASSWORD or Key Vault secret name is required"
        )
    return TenantRuntimeConfig(
        user=tenant_database_user,
        password=tenant_password,
        password_secret_name=tenant_password_secret_name,
    )


def validate_shared_tenant_runtime_startup(
    *,
    use_shared_tenant_database: bool,
    database_user: str,
    tenant_database_user: str,
    tenant_password: str,
    tenant_password_secret_name: str,
) -> TenantRuntimeConfig | None:
    if not use_shared_tenant_database:
        return None
    return validate_tenant_runtime_config(
        database_user=database_user,
        tenant_database_user=tenant_database_user,
        tenant_password=tenant_password,
        tenant_password_secret_name=tenant_password_secret_name,
    )
```

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\infra\test_tenant_runtime_config.py -v
```

Expected: 5 passed.

## Task 2: Tenant Runtime Credential Loading

**Files:**
- Create: `src/infra/database/tenant_runtime_credentials.py`
- Test: `tests/infra/test_tenant_runtime_credentials.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from src.infra.database.tenant_runtime_config import TenantRuntimeConfig
from src.infra.database.tenant_runtime_credentials import (
    TenantRuntimeCredentialError,
    load_tenant_runtime_password_sync,
    load_tenant_runtime_password,
)


@pytest.mark.asyncio
async def test_uses_inline_password_without_key_vault_call() -> None:
    secrets_manager = AsyncMock()
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="inline-password",
        password_secret_name="shared-tenant-db-password",
    )

    password = await load_tenant_runtime_password(config, secrets_manager=secrets_manager)

    assert password == "inline-password"
    secrets_manager.get.assert_not_called()


@pytest.mark.asyncio
async def test_loads_password_from_key_vault_secret() -> None:
    secrets_manager = AsyncMock()
    secrets_manager.get.return_value = "key-vault-password"
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )

    password = await load_tenant_runtime_password(config, secrets_manager=secrets_manager)

    assert password == "key-vault-password"
    secrets_manager.get.assert_awaited_once_with("shared-tenant-db-password")


@pytest.mark.asyncio
async def test_raises_when_key_vault_secret_missing() -> None:
    secrets_manager = AsyncMock()
    secrets_manager.get.return_value = None
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )

    with pytest.raises(TenantRuntimeCredentialError, match="unavailable"):
        await load_tenant_runtime_password(config, secrets_manager=secrets_manager)


def test_sync_uses_inline_password_without_key_vault_call() -> None:
    get_secret = Mock()
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="inline-password",
        password_secret_name="shared-tenant-db-password",
    )

    password = load_tenant_runtime_password_sync(config, get_secret=get_secret)

    assert password == "inline-password"
    get_secret.assert_not_called()


def test_sync_loads_password_from_same_secret_name() -> None:
    get_secret = Mock(return_value="key-vault-password")
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )

    password = load_tenant_runtime_password_sync(config, get_secret=get_secret)

    assert password == "key-vault-password"
    get_secret.assert_called_once_with("shared-tenant-db-password")
```

- [ ] **Step 2: Run tests to verify red**

Run:

```powershell
uv run pytest tests\infra\test_tenant_runtime_credentials.py -v
```

Expected: fail because `tenant_runtime_credentials.py` does not exist.

- [ ] **Step 3: Implement credential loader**

Create `src/infra/database/tenant_runtime_credentials.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from src.infra.database.tenant_runtime_config import TenantRuntimeConfig


class _SecretsManager(Protocol):
    async def get(self, name: str) -> str | None: ...


class TenantRuntimeCredentialError(RuntimeError):
    pass


async def load_tenant_runtime_password(
    config: TenantRuntimeConfig,
    *,
    secrets_manager: _SecretsManager,
) -> str:
    if config.password:
        return config.password

    password = await secrets_manager.get(config.password_secret_name)
    if not password:
        raise TenantRuntimeCredentialError(
            f"Tenant runtime password secret '{config.password_secret_name}' unavailable"
        )
    return password


def load_tenant_runtime_password_sync(
    config: TenantRuntimeConfig,
    *,
    get_secret: Callable[[str], str | None],
) -> str:
    if config.password:
        return config.password

    password = get_secret(config.password_secret_name)
    if not password:
        raise TenantRuntimeCredentialError(
            f"Tenant runtime password secret '{config.password_secret_name}' unavailable"
        )
    return password
```

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\infra\test_tenant_runtime_credentials.py -v
```

Expected: 5 passed.

## Task 2A: Startup Fail-Fast Wiring

**Files:**
- Modify: `src/api/fastapi_app.py`
- Modify: `src/workers/base/celery_base.py`
- Test: `tests/api/test_fastapi_app_tenant_runtime_startup.py`
- Test: `tests/workers/test_celery_tenant_runtime_startup.py`

- [ ] **Step 1: Write failing API startup test**

Create `tests/api/test_fastapi_app_tenant_runtime_startup.py`:

```python
from __future__ import annotations

import pytest

from src.api import fastapi_app


@pytest.mark.asyncio
async def test_api_lifespan_validates_shared_tenant_runtime_before_serving(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(fastapi_app, "initialize_redis_pool", lambda: None)

    async def async_noop(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(fastapi_app, "initialize_async_redis_pool", async_noop)
    monkeypatch.setattr(fastapi_app.security_secrets_config, "preload_required", async_noop)
    monkeypatch.setattr("src.infra.auth.supabase_admin_client.supabase_admin_client.initialize", async_noop)
    monkeypatch.setattr("src.infra.auth.supabase_user_client.supabase_user_client.initialize", async_noop)
    monkeypatch.setattr("src.infra.security.token_revocation.token_revocation_service.initialize", async_noop)
    monkeypatch.setattr(fastapi_app, "init_sentry", lambda: None)
    monkeypatch.setattr(fastapi_app, "async_init_otel", lambda service_name_suffix: async_noop())

    def validate() -> None:
        calls.append("validated")
        raise RuntimeError("TENANT_DATABASE_USER is required")

    monkeypatch.setattr(fastapi_app, "validate_current_shared_tenant_runtime_startup", validate)

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_USER"):
        async with fastapi_app.lifespan(object()):
            pass

    assert calls == ["validated"]
```

- [ ] **Step 2: Write failing worker startup test**

Create `tests/workers/test_celery_tenant_runtime_startup.py`:

```python
from __future__ import annotations

import pytest

from src.workers.base import celery_base


def test_worker_runtime_validation_raises_shared_tenant_config_error(monkeypatch) -> None:
    def validate() -> None:
        raise RuntimeError("TENANT_DATABASE_USER is required")

    monkeypatch.setattr(celery_base, "validate_current_shared_tenant_runtime_startup", validate)

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_USER"):
        celery_base._validate_tenant_runtime_startup()
```

- [ ] **Step 3: Run tests to verify red**

Run:

```powershell
uv run pytest tests\api\test_fastapi_app_tenant_runtime_startup.py tests\workers\test_celery_tenant_runtime_startup.py -v
```

Expected: fail because startup validation is not wired.

- [ ] **Step 4: Implement startup helper and API/worker calls**

In `src/infra/database/tenant_runtime_config.py`, add:

```python
from src.infra.config.configs import database_config


def validate_current_shared_tenant_runtime_startup() -> TenantRuntimeConfig | None:
    return validate_shared_tenant_runtime_startup(
        use_shared_tenant_database=database_config.USE_SHARED_TENANT_DATABASE,
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )
```

In `src/api/fastapi_app.py`, import and call config plus password validation immediately after the startup log:

```python
from src.infra.database.tenant_runtime_config import (
    validate_current_shared_tenant_runtime_startup,
)
from src.infra.database.tenant_runtime_credentials import (
    validate_current_shared_tenant_runtime_password,
)
from src.infra.secrets.manager import master_secrets_manager
```

```python
logger.info("Starting application services...")
validate_current_shared_tenant_runtime_startup()
await validate_current_shared_tenant_runtime_password(
    secrets_manager=master_secrets_manager,
)
```

In `src/workers/base/celery_base.py`, import and add a small wrapper:

```python
from src.infra.database.tenant_runtime_config import (
    validate_current_shared_tenant_runtime_startup,
)
from src.infra.database.tenant_runtime_credentials import (
    validate_current_shared_tenant_runtime_password_sync,
)
from src.infra.secrets.master_key_vault_sync import get_master_key_vault_secret_sync
```

```python
def _validate_tenant_runtime_startup() -> None:
    validate_current_shared_tenant_runtime_startup()
    validate_current_shared_tenant_runtime_password_sync(
        get_secret=get_master_key_vault_secret_sync,
    )
```

Then call it in the existing `_register_observability_signals()` worker hooks before OTel init:

```python
@worker_process_init.connect(weak=False)
def _validate_tenant_runtime_after_fork(sender=None, **kwargs):  # noqa: ARG001
    _validate_tenant_runtime_startup()


@worker_ready.connect(weak=False)
def _validate_tenant_runtime_in_main_process(sender=None, **kwargs):  # noqa: ARG001
    _validate_tenant_runtime_startup()
```

- [ ] **Step 5: Run tests to verify green**

Run:

```powershell
uv run pytest tests\api\test_fastapi_app_tenant_runtime_startup.py tests\workers\test_celery_tenant_runtime_startup.py -v
```

Expected: startup validation tests pass.

## Task 3: Tenant Search Path Transaction Boundary

**Files:**
- Create: `src/infra/database/tenant_search_path.py`
- Test: `tests/infra/test_tenant_search_path.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from sqlalchemy import event, text
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
    apply_tenant_transaction_context,
    install_tenant_transaction_context_listener,
)


@pytest.fixture
def sqlite_sessionmaker():
    engine = create_engine("sqlite:///:memory:")
    yield sessionmaker(bind=engine, class_=Session)
    engine.dispose()


def test_applies_set_local_search_path_at_transaction_begin() -> None:
    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    assert connection.exec_driver_sql.call_args_list[0] == call(
        'SET LOCAL search_path TO "org_123", "extensions"'
    )
    assert TRUSTED_EXTENSION_SCHEMA == "extensions"


def test_does_nothing_for_master_session_without_tenant_schema() -> None:
    session = Session()
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    connection.exec_driver_sql.assert_not_called()


def test_reapplies_for_new_transaction_after_commit_boundary() -> None:
    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)
    apply_tenant_transaction_context(session, None, connection)

    assert connection.exec_driver_sql.call_args_list.count(
        call('SET LOCAL search_path TO "org_123", "extensions"')
    ) == 2


def test_real_session_event_reapplies_after_commit(sqlite_sessionmaker) -> None:
    session = sqlite_sessionmaker()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    calls: list[str] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("SET LOCAL"):
            calls.append(statement)
            return "SELECT 1", ()
        return statement, parameters

    event.listen(session.bind, "before_cursor_execute", capture_sql, retval=True)

    try:
        session.execute(text("SELECT 1"))
        session.commit()
        session.execute(text("SELECT 1"))
    finally:
        event.remove(session.bind, "before_cursor_execute", capture_sql)
        session.close()

    assert calls.count('SET LOCAL search_path TO "org_123", "extensions"') == 2


def test_listener_is_registered_once_for_sqlalchemy_sessions() -> None:
    install_tenant_transaction_context_listener()
    install_tenant_transaction_context_listener()

    assert event.contains(Session, "after_begin", apply_tenant_transaction_context)
```

- [ ] **Step 2: Run tests to verify red**

Run:

```powershell
uv run pytest tests\infra\test_tenant_search_path.py -v
```

Expected: fail because `tenant_search_path.py` does not exist.

- [ ] **Step 3: Implement transaction hook module**

Create `src/infra/database/tenant_search_path.py`:

```python
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session
from src.infra.config.configs import database_config

TENANT_SCHEMA_SESSION_KEY = "tenant_schema"
TRUSTED_EXTENSION_SCHEMA = "extensions"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def apply_tenant_transaction_context(session: Session, _transaction, connection) -> None:
    schema = session.info.get(TENANT_SCHEMA_SESSION_KEY)
    if not schema:
        return

    search_path = ", ".join(
        [
            _quote_identifier(str(schema)),
            _quote_identifier(TRUSTED_EXTENSION_SCHEMA),
        ]
    )
    connection.exec_driver_sql(f"SET LOCAL search_path TO {search_path}")
    timeout_ms = int(database_config.DATABASE_STATEMENT_TIMEOUT_MS)
    if timeout_ms > 0:
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")


def install_tenant_transaction_context_listener() -> None:
    if not event.contains(Session, "after_begin", apply_tenant_transaction_context):
        event.listen(Session, "after_begin", apply_tenant_transaction_context)


install_tenant_transaction_context_listener()
```

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\infra\test_tenant_search_path.py -v
```

Expected: 5 passed.

## Task 4: Clean Async Tenant Runtime Provider

**Files:**
- Modify: `src/infra/database/provider_async.py`
- Test: `tests/infra/test_async_db_provider.py`

- [ ] **Step 1: Write failing tests**

Add tests to `tests/infra/test_async_db_provider.py`:

```python
from src.infra.database.tenant_search_path import TENANT_SCHEMA_SESSION_KEY
from src.infra.utils.resource_naming import get_schema_name


@pytest.mark.asyncio
async def test_tenant_runtime_uses_one_engine_for_multiple_orgs(monkeypatch) -> None:
    created_urls: list[str] = []

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(provider_async.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    first = await provider._get_tenant_engine(uuid4())
    second = await provider._get_tenant_engine(uuid4())

    assert first is second
    assert len(created_urls) == 1
    assert created_urls[0].startswith(
        "postgresql+asyncpg://app_tenant_runtime:runtime-password@"
    )


@pytest.mark.asyncio
async def test_tenant_runtime_rejects_master_user(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(provider_async.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        await provider._get_tenant_engine(uuid4())


@pytest.mark.asyncio
async def test_tenant_runtime_password_loads_from_master_key_vault_once(monkeypatch) -> None:
    created_urls: list[str] = []
    secret_calls: list[str] = []

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    async def fake_get(secret_name: str) -> str:
        secret_calls.append(secret_name)
        return "vault-password"

    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(provider_async.master_secrets_manager, "get", fake_get)
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(provider_async.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(provider_async.database_config, "TENANT_DATABASE_PASSWORD", "", raising=False)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    first = await provider._get_tenant_engine(uuid4())
    second = await provider._get_tenant_engine(uuid4())

    assert first is second
    assert secret_calls == ["shared-tenant-db-password"]
    assert created_urls[0].startswith("postgresql+asyncpg://app_tenant_runtime:vault-password@")


@pytest.mark.asyncio
async def test_async_tenant_session_sets_schema_context(monkeypatch) -> None:
    org_id = uuid4()

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = type("SyncSession", (), {"info": {}})()
            self.closed = False

        async def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        async def close(self) -> None:
            self.closed = True

    fake_session = FakeAsyncSession()

    async def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    def fake_sessionmaker(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: fake_session

    monkeypatch.setattr(
        provider_async.AsyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(provider_async, "async_sessionmaker", fake_sessionmaker)

    provider = provider_async.AsyncSqlAlchemyProvider()

    async with provider.tenant_session(org_id) as session:
        assert session is fake_session
        assert session.sync_session.info[TENANT_SCHEMA_SESSION_KEY] == get_schema_name(org_id)

    assert fake_session.closed
```

- [ ] **Step 2: Run tests to verify red**

Run:

```powershell
uv run pytest tests\infra\test_async_db_provider.py -v -k tenant_runtime
```

Expected: fail because provider still uses old per-org engine behavior or missing config validator.

- [ ] **Step 3: Implement clean async provider shape**

In `src/infra/database/provider_async.py`:

- Keep `_master_engine`.
- Keep the shared tenant runtime cache as `_shared_tenant_engine`; do not rename it to `_tenant_engine` because `src/infra/utils/async_runner.py` already clears `_shared_tenant_engine` on fork/loop reset.
- Keep `_tenant_engines` only for legacy per-org runtime when `USE_SHARED_TENANT_DATABASE=false`.
- Build tenant URL from validated tenant runtime config.
- Replace the runtime shared-tenant secret import with `from src.infra.secrets.manager import master_secrets_manager`; keep `secrets_manager` only for legacy per-org mode if that code path still needs it.
- Load tenant password through `load_tenant_runtime_password(config, secrets_manager=master_secrets_manager)`.
- In `tenant_session(org_id)`, set `session.sync_session.info[TENANT_SCHEMA_SESSION_KEY] = get_schema_name(org_id)`.
- Remove runtime calls to `get_key_vault_secret_name(org_id)` from shared tenant mode.
- Do not call `SET search_path` directly in `tenant_session`.
- Do not call `SET ROLE`.
- Verify `tests/infra/test_async_runner.py` still covers clearing `_shared_tenant_engine`; update that test only if the cleanup behavior changes.

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\infra\test_async_db_provider.py tests\infra\test_tenant_runtime_config.py tests\infra\test_tenant_runtime_credentials.py tests\infra\test_tenant_search_path.py -v
```

Expected: all selected tests pass.

## Task 5: Clean Sync Tenant Runtime Provider

**Files:**
- Modify: `src/infra/database/provider_sync.py`
- Test: create `tests/infra/test_sync_db_provider.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from uuid import uuid4

import pytest

import src.infra.database.provider_sync as provider_sync
from src.infra.database.tenant_search_path import TENANT_SCHEMA_SESSION_KEY
from src.infra.utils.resource_naming import get_schema_name


def test_sync_tenant_runtime_uses_one_engine_for_multiple_orgs(monkeypatch) -> None:
    created_urls: list[str] = []

    def fake_create_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    monkeypatch.setattr(provider_sync, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_sync.SyncSqlAlchemyProvider()
    first = provider._get_tenant_engine(uuid4())
    second = provider._get_tenant_engine(uuid4())

    assert first is second
    assert len(created_urls) == 1
    assert created_urls[0].startswith("postgresql://app_tenant_runtime:runtime-password@")


def test_sync_tenant_runtime_rejects_master_user(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_sync.SyncSqlAlchemyProvider()

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        provider._get_tenant_engine(uuid4())


def test_sync_tenant_runtime_loads_password_through_sync_credential_helper(monkeypatch) -> None:
    calls: list[str] = []

    def fake_create_engine(url, **kwargs):  # noqa: ANN001, ARG001
        return {"url": url}

    def fake_get_secret(name: str) -> str:
        calls.append(name)
        return "key-vault-password"

    monkeypatch.setattr(provider_sync, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        provider_sync,
        "_get_master_key_vault_secret_sync",
        fake_get_secret,
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(provider_sync.database_config, "TENANT_DATABASE_PASSWORD", "", raising=False)
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    provider = provider_sync.SyncSqlAlchemyProvider()

    engine = provider._get_tenant_engine(uuid4())

    assert engine == {
        "url": "postgresql://app_tenant_runtime:key-vault-password@localhost:5432/postgres"
    }
    assert calls == ["shared-tenant-db-password"]


def test_sync_tenant_session_sets_schema_context(monkeypatch) -> None:
    org_id = uuid4()

    class FakeSession:
        def __init__(self) -> None:
            self.info: dict[str, str] = {}
            self.closed = False
            self.committed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        def close(self) -> None:
            self.closed = True

    fake_session = FakeSession()

    def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    def fake_sessionmaker(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: fake_session

    monkeypatch.setattr(
        provider_sync.SyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(provider_sync, "sessionmaker", fake_sessionmaker)

    provider = provider_sync.SyncSqlAlchemyProvider()

    with provider.tenant_session(org_id) as session:
        assert session is fake_session
        assert session.info[TENANT_SCHEMA_SESSION_KEY] == get_schema_name(org_id)

    assert fake_session.committed
    assert fake_session.closed
```

- [ ] **Step 2: Run test to verify red**

Run:

```powershell
uv run pytest tests\infra\test_sync_db_provider.py -v
```

Expected: fail until sync provider uses one tenant runtime engine.

- [ ] **Step 3: Implement sync provider**

In `src/infra/database/provider_sync.py`:

- Use the same config validator.
- Use inline `TENANT_DATABASE_PASSWORD` for sync runtime.
- If inline password is empty, load from the master/shared Key Vault through `load_tenant_runtime_password_sync(...)`.
- Use `src.infra.secrets.master_key_vault_sync.get_master_key_vault_secret_sync` for Key Vault reads. The sync provider must not build `DefaultAzureCredential` directly and must not call `asyncio.run()` for tenant runtime credentials.
- Keep the shared tenant runtime cache as `_shared_tenant_engine`; do not rename it to `_tenant_engine` because `src/infra/utils/async_runner.py` already clears `_shared_tenant_engine` on fork reset.
- Keep `_tenant_engines` only for legacy per-org runtime when `USE_SHARED_TENANT_DATABASE=false`.
- Set `session.info[TENANT_SCHEMA_SESSION_KEY] = get_schema_name(org_id)` in `tenant_session`.
- Do not call direct `SET search_path`.
- Do not call `SET ROLE`.
- Verify `tests/infra/test_async_runner.py` still covers clearing `_shared_tenant_engine`; update that test only if the cleanup behavior changes.

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\infra\test_sync_db_provider.py -v
```

Expected: sync provider tests pass.

## Task 6: Dedicated Runtime Role Provisioning Script

**Files:**
- Create: `scripts/maintenance/provision_tenant_runtime_role.py`
- Test: `tests/scripts/maintenance/test_provision_tenant_runtime_role.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from contextlib import contextmanager

import pytest
from azure.core.exceptions import ResourceNotFoundError

from scripts.maintenance import provision_tenant_runtime_role as provision_module


class RecordingSession:
    def __init__(self, *, role_exists: bool = False) -> None:
        self.role_exists = role_exists
        self.statements: list[str] = []
        self.params: list[dict[str, str] | None] = []
        self.events: list[str] = []

    def begin(self):
        return self

    def __enter__(self):
        self.events.append("begin")
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        self.events.append("rollback" if exc_type else "commit")
        return False

    def execute(self, statement, params=None):  # noqa: ANN001
        self.statements.append(str(statement))
        self.params.append(params)
        value = 1 if self.role_exists else None
        return type("Result", (), {"scalar_one_or_none": lambda self: value})()


def test_creates_dedicated_runtime_role_and_stores_password(monkeypatch) -> None:
    session = RecordingSession(role_exists=False)
    stored: list[tuple[str, str]] = []

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            raise AssertionError(f"unexpected existing secret lookup for {name}")

        def set_secret(self, name: str, value: str) -> None:
            stored.append((name, value))

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda: FakeSecretClient("https://master-vault.example", object()),
    )
    monkeypatch.setattr(provision_module.secrets, "token_urlsafe", lambda length: "generated")
    monkeypatch.setattr(provision_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(provision_module.database_config, "DATABASE_NAME", "appdb", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )
    provision_module.provision_runtime_role(execute=True, rotate_password=False)

    rendered_sql = "\n".join(session.statements)
    assert 'CREATE ROLE "app_tenant_runtime" LOGIN PASSWORD :pwd' in rendered_sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS" in rendered_sql
    assert (
        'ALTER ROLE "app_tenant_runtime" IN DATABASE "appdb" SET search_path = pg_catalog'
        in rendered_sql
    )
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in rendered_sql
    assert 'CREATE SCHEMA IF NOT EXISTS "extensions" AUTHORIZATION "postgres"' in rendered_sql
    assert 'REVOKE CREATE ON SCHEMA "extensions" FROM PUBLIC' in rendered_sql
    assert 'GRANT USAGE ON SCHEMA "extensions" TO "app_tenant_runtime"' in rendered_sql
    assert 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA "extensions"' in rendered_sql
    assert 'ALTER EXTENSION vector SET SCHEMA "extensions"' in rendered_sql
    assert stored == [("shared-tenant-db-password", "generated")]
    assert session.events == ["begin", "commit"]


def test_existing_role_does_not_rotate_password_without_flag(monkeypatch) -> None:
    session = RecordingSession(role_exists=True)
    stored: list[tuple[str, str]] = []

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            assert name == "shared-tenant-db-password"
            return type("Secret", (), {"value": "existing-password"})()

        def set_secret(self, name: str, value: str) -> None:
            stored.append((name, value))

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda: FakeSecretClient("https://master-vault.example", object()),
    )
    monkeypatch.setattr(provision_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(provision_module.database_config, "DATABASE_NAME", "appdb", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )
    provision_module.provision_runtime_role(execute=True, rotate_password=False)

    rendered_sql = "\n".join(session.statements)
    assert "PASSWORD :pwd" not in rendered_sql
    assert 'ALTER ROLE "app_tenant_runtime" WITH LOGIN' in rendered_sql
    assert stored == []


def test_existing_role_fails_when_runtime_secret_missing(monkeypatch) -> None:
    session = RecordingSession(role_exists=True)

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            assert name == "shared-tenant-db-password"
            return None

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda: FakeSecretClient("https://master-vault.example", object()),
    )
    monkeypatch.setattr(provision_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(provision_module.database_config, "DATABASE_NAME", "appdb", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )
    with pytest.raises(RuntimeError, match="missing from Key Vault"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)

    assert "ALTER ROLE" not in "\n".join(session.statements)


def test_existing_role_treats_key_vault_not_found_as_missing(monkeypatch) -> None:
    session = RecordingSession(role_exists=True)

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            assert name == "shared-tenant-db-password"
            raise ResourceNotFoundError(message="not found")

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda: FakeSecretClient("https://master-vault.example", object()),
    )
    monkeypatch.setattr(provision_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(provision_module.database_config, "DATABASE_NAME", "appdb", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )
    with pytest.raises(RuntimeError, match="missing from Key Vault"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)

    assert "ALTER ROLE" not in "\n".join(session.statements)


def test_key_vault_write_failure_rolls_back_password_sql(monkeypatch) -> None:
    session = RecordingSession(role_exists=False)

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def set_secret(self, name: str, value: str) -> None:
            assert name == "shared-tenant-db-password"
            assert value == "generated"
            raise RuntimeError("Key Vault write failed")

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda: FakeSecretClient("https://master-vault.example", object()),
    )
    monkeypatch.setattr(provision_module.secrets, "token_urlsafe", lambda length: "generated")
    monkeypatch.setattr(provision_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(provision_module.database_config, "DATABASE_NAME", "appdb", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )
    with pytest.raises(RuntimeError, match="Key Vault write failed"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)

    assert 'CREATE ROLE "app_tenant_runtime" LOGIN PASSWORD :pwd' in "\n".join(
        session.statements
    )
    assert session.events == ["begin", "rollback"]


def test_rejects_master_user_reuse(monkeypatch) -> None:
    monkeypatch.setattr(provision_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)
```

- [ ] **Step 2: Run tests to verify red**

Run:

```powershell
uv run pytest tests\scripts\maintenance\test_provision_tenant_runtime_role.py -v
```

Expected: fail because script does not exist.

- [ ] **Step 3: Implement script**

Create `scripts/maintenance/provision_tenant_runtime_role.py`:

```python
from __future__ import annotations

import argparse
import secrets

from azure.core.exceptions import ResourceNotFoundError
from azure.keyvault.secrets import SecretClient
from sqlalchemy import text
from src.infra.config.configs import database_config
from src.infra.database.sessions import master_session_scope
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.secrets.master_key_vault_sync import build_master_secret_client_sync


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def build_secret_client() -> SecretClient:
    return build_master_secret_client_sync()


def ensure_existing_secret(client: SecretClient, secret_name: str) -> None:
    try:
        secret = client.get_secret(secret_name)
    except ResourceNotFoundError as exc:
        raise RuntimeError(
            f"Tenant runtime password secret '{secret_name}' missing from Key Vault"
        ) from exc
    if secret is None or not getattr(secret, "value", None):
        raise RuntimeError(f"Tenant runtime password secret '{secret_name}' missing from Key Vault")


def ensure_trusted_extension_schema(session, *, runtime_role: str) -> None:  # noqa: ANN001
    extension_schema_sql = quote_identifier("extensions")
    runtime_role_sql = quote_identifier(runtime_role)
    owner_sql = quote_identifier(database_config.DATABASE_USER)
    session.execute(
        text(f"CREATE SCHEMA IF NOT EXISTS {extension_schema_sql} AUTHORIZATION {owner_sql}")
    )
    session.execute(text(f"REVOKE CREATE ON SCHEMA {extension_schema_sql} FROM PUBLIC"))
    session.execute(text(f"GRANT USAGE ON SCHEMA {extension_schema_sql} TO {runtime_role_sql}"))
    session.execute(text(f"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {extension_schema_sql}"))
    session.execute(text(f"ALTER EXTENSION vector SET SCHEMA {extension_schema_sql}"))


def provision_runtime_role(*, execute: bool, rotate_password: bool = False) -> None:
    config = validate_tenant_runtime_config(
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )
    password = database_config.TENANT_DATABASE_PASSWORD or secrets.token_urlsafe(32)
    role_sql = quote_identifier(config.user)
    database_sql = quote_identifier(database_config.DATABASE_NAME)

    if not execute:
        print(f"dry-run provision tenant runtime role {config.user}")
        return

    client = build_secret_client()
    with master_session_scope() as session:
        role_exists = session.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
            {"role_name": config.user},
        ).scalar_one_or_none()

    should_store_secret = (not role_exists) or rotate_password
    if not should_store_secret and not database_config.TENANT_DATABASE_PASSWORD:
        ensure_existing_secret(client, database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME)

    with master_session_scope() as session, session.begin():
        if role_exists:
            if rotate_password:
                session.execute(
                    text(
                        f"ALTER ROLE {role_sql} WITH LOGIN PASSWORD :pwd "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ),
                    {"pwd": password},
                )
            else:
                session.execute(
                    text(
                        f"ALTER ROLE {role_sql} WITH LOGIN "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    )
                )
        else:
            session.execute(
                text(
                    f"CREATE ROLE {role_sql} LOGIN PASSWORD :pwd "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                ),
                {"pwd": password},
            )
        session.execute(
            text(f"ALTER ROLE {role_sql} IN DATABASE {database_sql} SET search_path = pg_catalog")
        )
        session.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        ensure_trusted_extension_schema(session, runtime_role=config.user)
        if should_store_secret:
            client.set_secret(database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME, password)

    if not should_store_secret:
        print(f"verified tenant runtime role {config.user}; password unchanged")
        return
    print(f"stored tenant runtime password in {database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--rotate-password", action="store_true")
    args = parser.parse_args()
    provision_runtime_role(execute=args.execute, rotate_password=args.rotate_password)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\scripts\maintenance\test_provision_tenant_runtime_role.py -v
```

Expected: all tenant runtime role provisioning tests pass.

## Task 7: Schema Grant Backfill Script

**Files:**
- Create: `src/infra/database/tenant_runtime_grants.py`
- Create: `scripts/maintenance/grant_tenant_runtime_schema_access.py`
- Test: `tests/infra/test_tenant_runtime_grants.py`
- Test: `tests/scripts/maintenance/test_grant_tenant_runtime_schema_access.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from scripts.maintenance import grant_tenant_runtime_schema_access as grant_module
from src.infra.database.tenant_runtime_grants import TenantRuntimeGrantService


class ScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class ExecuteResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return ScalarResult(self.values)


class RecordingSession:
    def __init__(self, values):
        self.values = values
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))
        return ExecuteResult(self.values)


def test_all_org_query_has_no_status_filter() -> None:
    org_ids = [uuid4(), uuid4()]
    session = RecordingSession(org_ids)

    resolved = grant_module.resolve_target_org_ids(session, all_orgs=True, org_id=None)

    assert resolved == org_ids
    rendered_sql = "\n".join(session.statements).lower()
    assert "organisations" in rendered_sql
    assert "status" not in rendered_sql


def test_dry_run_does_not_apply_grants(monkeypatch) -> None:
    org_id = uuid4()

    @contextmanager
    def fake_master_session_scope():
        yield RecordingSession([org_id])

    class FakeService:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def grant_tenant_runtime_role_access(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("dry-run must not mutate")

    monkeypatch.setattr(grant_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(grant_module, "TenantRuntimeGrantService", FakeService)
    monkeypatch.setattr(grant_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        grant_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        grant_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        grant_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    count = grant_module.grant_schema_access(all_orgs=True, org_id=None, execute=False)

    assert count == 1


def test_grant_service_emits_least_privilege_schema_sql() -> None:
    session = RecordingSession([])
    service = TenantRuntimeGrantService(session)

    service.grant_tenant_runtime_role_access(
        schema_name="org_123",
        schema_owner_role="org_123_role",
        migration_owner_role="postgres",
        runtime_role="app_tenant_runtime",
    )

    rendered_sql = "\n".join(session.statements)
    assert 'GRANT USAGE ON SCHEMA "org_123" TO "app_tenant_runtime"' in rendered_sql
    assert (
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "org_123" '
        'TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "org_123" '
        'TO "app_tenant_runtime"'
    ) in rendered_sql
    assert 'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "org_123" TO "app_tenant_runtime"' in rendered_sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "org_123" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "org_123_role" IN SCHEMA "org_123" '
        'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "app_tenant_runtime"'
    ) in rendered_sql
    assert "OWNER TO" not in rendered_sql
    assert "ON SCHEMA public" not in rendered_sql
```

- [ ] **Step 2: Run tests to verify red**

Run:

```powershell
uv run pytest tests\scripts\maintenance\test_grant_tenant_runtime_schema_access.py -v
```

Expected: fail because script does not exist.

- [ ] **Step 3: Implement grant service**

Create `src/infra/database/tenant_runtime_grants.py`:

```python
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class TenantRuntimeGrantService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def grant_tenant_runtime_role_access(
        self,
        *,
        schema_name: str,
        schema_owner_role: str,
        migration_owner_role: str,
        runtime_role: str,
    ) -> None:
        schema_sql = quote_identifier(schema_name)
        runtime_sql = quote_identifier(runtime_role)
        owner_roles = [migration_owner_role]
        if schema_owner_role != migration_owner_role:
            owner_roles.append(schema_owner_role)

        statements = [
            f"GRANT USAGE ON SCHEMA {schema_sql} TO {runtime_sql}",
            (
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema_sql} "
                f"TO {runtime_sql}"
            ),
            (
                f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {schema_sql} "
                f"TO {runtime_sql}"
            ),
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema_sql} TO {runtime_sql}",
        ]

        for owner_role in owner_roles:
            owner_sql = quote_identifier(owner_role)
            statements.extend(
                [
                    (
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} IN SCHEMA {schema_sql} "
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {runtime_sql}"
                    ),
                    (
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} IN SCHEMA {schema_sql} "
                        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {runtime_sql}"
                    ),
                    (
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} IN SCHEMA {schema_sql} "
                        f"GRANT EXECUTE ON FUNCTIONS TO {runtime_sql}"
                    ),
                ]
            )

        for statement in statements:
            self.session.execute(text(statement))
```

- [ ] **Step 4: Implement backfill script**

Create `scripts/maintenance/grant_tenant_runtime_schema_access.py`:

```python
from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from src.domain.models.master_tables.organisation import Organisation
from src.infra.config.configs import database_config
from src.infra.database.sessions import master_session_scope
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.database.tenant_runtime_grants import TenantRuntimeGrantService
from src.infra.utils.resource_naming import get_role_name, get_schema_name


def resolve_target_org_ids(
    session: Session,
    *,
    all_orgs: bool,
    org_id: UUID | None,
) -> list[UUID]:
    if org_id is not None:
        return [org_id]
    if not all_orgs:
        raise RuntimeError("Pass --all-orgs or --org-id")
    result = session.execute(select(Organisation.id))
    return list(result.scalars().all())


def grant_schema_access(*, all_orgs: bool, org_id: UUID | None, execute: bool) -> int:
    config = validate_tenant_runtime_config(
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )

    with master_session_scope() as session:
        org_ids = resolve_target_org_ids(session, all_orgs=all_orgs, org_id=org_id)
        if not execute:
            for target_org_id in org_ids:
                print(f"dry-run grant tenant runtime access for {target_org_id}")
            return len(org_ids)

        service = TenantRuntimeGrantService(session)
        for target_org_id in org_ids:
            service.grant_tenant_runtime_role_access(
                schema_name=get_schema_name(target_org_id),
                schema_owner_role=get_role_name(target_org_id),
                migration_owner_role=database_config.DATABASE_USER,
                runtime_role=config.user,
            )
        session.commit()
        return len(org_ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-orgs", action="store_true")
    parser.add_argument("--org-id", type=UUID)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    count = grant_schema_access(all_orgs=args.all_orgs, org_id=args.org_id, execute=args.execute)
    print(f"processed {count} organisation schemas")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify green**

Run:

```powershell
uv run pytest tests\infra\test_tenant_runtime_grants.py tests\scripts\maintenance\test_grant_tenant_runtime_schema_access.py -v
```

Expected: all grant service and grant script tests pass.

## Task 8: New-Org Provisioning Hook

**Files:**
- Modify: `src/service/orgnisation/organization_management.py`
- Test: `tests/service/test_organization_management_shared_tenant.py`

- [ ] **Step 1: Write failing test**

```python
def test_new_org_provisioning_grants_tenant_runtime_access_after_migrations(monkeypatch) -> None:
    calls: list[str] = []
    service = OrganizationManagementService()

    monkeypatch.setattr(service, "_create_database_role_and_schema", lambda *args: calls.append("schema"))
    monkeypatch.setattr(service, "_run_organization_migrations", lambda *args: calls.append("migrations"))
    monkeypatch.setattr(service, "_grant_tenant_runtime_role_access", lambda *args: calls.append("grants"))

    service._provision_tenant_database("org_123", "org_123_role", "password")

    assert calls == ["schema", "migrations", "grants"]


def test_new_org_runtime_grant_skips_when_runtime_role_not_configured(monkeypatch) -> None:
    service = OrganizationManagementService()
    calls: list[str] = []

    monkeypatch.setattr(
        "src.service.orgnisation.organization_management.database_config.TENANT_DATABASE_USER",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        "src.service.orgnisation.organization_management.TenantRuntimeGrantService",
        lambda *args: calls.append("service"),
    )

    service._grant_tenant_runtime_role_access("org_123", "org_123_role")

    assert calls == []


def test_new_org_runtime_grant_rejects_master_database_user(monkeypatch) -> None:
    service = OrganizationManagementService()

    monkeypatch.setattr(
        "src.service.orgnisation.organization_management.database_config.DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        "src.service.orgnisation.organization_management.database_config.TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        "src.service.orgnisation.organization_management.database_config.TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        "src.service.orgnisation.organization_management.database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        service._grant_tenant_runtime_role_access("org_123", "org_123_role")
```

Also update or add one existing `create_organization` orchestration test to record calls for:

```text
_provision_tenant_database
_store_password_in_key_vault
_setup_internal_crm
```

Expected order: `_provision_tenant_database`, then `_store_password_in_key_vault`, then `_setup_internal_crm`.

- [ ] **Step 2: Run test to verify red**

Run:

```powershell
uv run pytest tests\service\test_organization_management_shared_tenant.py -v
```

Expected: fail until provisioning has a clean testable database provisioning method.

- [ ] **Step 3: Extract a tiny database provisioning method**

In `OrganizationManagementService`, extract only the DB provisioning sequence:

```python
from src.infra.config.configs import database_config
from src.infra.database.sessions import master_session_scope
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.database.tenant_runtime_grants import TenantRuntimeGrantService


def _provision_tenant_database(self, schema_name: str, role_name: str, role_password: str) -> None:
    self._create_database_role_and_schema(schema_name, role_name, role_password)
    self._run_organization_migrations(schema_name)
    self._grant_tenant_runtime_role_access(schema_name, role_name)


def _grant_tenant_runtime_role_access(self, schema_name: str, schema_owner_role: str) -> None:
    if not database_config.TENANT_DATABASE_USER:
        return
    config = validate_tenant_runtime_config(
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )
    with master_session_scope() as session, session.begin():
        TenantRuntimeGrantService(session).grant_tenant_runtime_role_access(
            schema_name=schema_name,
            schema_owner_role=schema_owner_role,
            migration_owner_role=database_config.DATABASE_USER,
            runtime_role=config.user,
        )
```

Then call it from `create_organization`. Keep storage, CRM setup, and status updates unchanged. Preserve the existing per-org secret timing: call `_store_password_in_key_vault(organisation_id, role_password)` after `_provision_tenant_database(...)` and before `_setup_internal_crm(...)`, because internal CRM still opens a legacy org session when `USE_SHARED_TENANT_DATABASE=false`.

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\service\test_organization_management_shared_tenant.py -v
```

Expected: all tests pass.

## Task 9: Static Guardrails

**Files:**
- Create: `scripts/maintenance/check_tenant_runtime_bypasses.py`
- Test: `tests/scripts/maintenance/test_check_tenant_runtime_bypasses.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from scripts.maintenance.check_tenant_runtime_bypasses import find_bypasses


def test_flags_direct_tenant_create_engine() -> None:
    files = {
        "src/bad.py": "create_async_engine('postgresql+asyncpg://org_user:pwd@host/db')",
    }

    findings = find_bypasses(files)

    assert findings == ["src/bad.py: direct tenant engine creation"]


def test_allows_migrations_and_maintenance_explicit_org_schema_sql() -> None:
    files = {
        "migrations/org_migrations/versions/x.py": "SELECT * FROM org_123.table",
        "scripts/maintenance/x.py": "SELECT * FROM org_123.table",
    }

    assert find_bypasses(files) == []


def test_allows_database_provider_engine_creation() -> None:
    files = {
        "src/infra/database/provider_async.py": "create_async_engine(url)",
        "src/infra/database/provider_sync.py": "create_engine(url)",
    }

    assert find_bypasses(files) == []


def test_allows_known_migration_script_engine_creation() -> None:
    files = {
        "scripts/run_master_migrations.py": "create_engine(url)",
        "scripts/run_org_migrations.py": "create_engine(url)",
        "scripts/maintenance/grant_tenant_runtime_schema_access.py": "create_engine(url)",
    }

    assert find_bypasses(files) == []


def test_flags_explicit_org_schema_sql_outside_allowlist() -> None:
    files = {
        "src/service/bad.py": "SELECT * FROM org_123.crm_deals",
    }

    assert find_bypasses(files) == ["src/service/bad.py: explicit org schema reference"]


def test_checker_is_strict_by_default() -> None:
    from scripts.maintenance.check_tenant_runtime_bypasses import exit_code_for_findings

    assert exit_code_for_findings(["src/service/bad.py: explicit org schema reference"]) == 1
    assert (
        exit_code_for_findings(
            ["src/service/bad.py: explicit org schema reference"],
            advisory=True,
        )
        == 0
    )


def test_release_gate_scans_runtime_src_only_by_default() -> None:
    from scripts.maintenance.check_tenant_runtime_bypasses import _default_scan_roots

    assert _default_scan_roots() == ["src"]
    assert _default_scan_roots(include_scripts=True) == ["src", "scripts"]
    assert _default_scan_roots(include_tests=True) == ["src", "tests"]
```

Do not make the production CLI scan scripts or its own tests by default. Existing top-level migration scripts may legitimately create engines, maintenance scripts may intentionally reference org schemas, `scripts/test_local/**` contains local investigation/debug scripts, and unit tests may contain intentionally bad `org_123` examples. Non-maintenance production scripts and tests can be checked explicitly while tuning allowlists, but they must not make the release-gate command fail by default. `--include-scripts` should be strict after the known migration, top-level cleanup, maintenance-script, and local-investigation exclusions are applied.

- [ ] **Step 2: Run test to verify red**

Run:

```powershell
uv run pytest tests\scripts\maintenance\test_check_tenant_runtime_bypasses.py -v
```

Expected: fail because script does not exist.

- [ ] **Step 3: Implement static checker**

Create `scripts/maintenance/check_tenant_runtime_bypasses.py` with:

- `find_bypasses(files: dict[str, str]) -> list[str]`
- CLI mode scanning runtime `src` by default; v1 exits non-zero by default. Use `--include-scripts` to include production scripts outside `scripts/maintenance/**` and `scripts/test_local/**`, or `--include-tests` for local exploration while tuning allowlists. Use `--advisory` only when you want findings without a nonzero exit.
- Allow direct engine creation only in `src/infra/database/provider_*.py`, `scripts/run_master_migrations.py`, `scripts/run_org_migrations.py`, `scripts/cleanup_internal_stakeholders.py`, and `scripts/maintenance/**`.
- Allow explicit `org_` schema references only in `migrations/**` and `scripts/maintenance/**`.
- Flag direct `create_engine` or `create_async_engine` calls outside the provider allowlist.
- Flag explicit `org_` schema SQL outside migrations and maintenance.

```python
from __future__ import annotations

import argparse
import re
from pathlib import Path

ENGINE_PATTERN = re.compile(r"\bcreate_(?:async_)?engine\s*\(")
ORG_SCHEMA_PATTERN = re.compile(r"\borg_[0-9a-fA-F_]{8,}\b")


def _allows_direct_engine_creation(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized in {
        "src/infra/database/provider_async.py",
        "src/infra/database/provider_sync.py",
        "scripts/run_master_migrations.py",
        "scripts/run_org_migrations.py",
    } or normalized.startswith("scripts/maintenance/")


def _allows_explicit_org_schema(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("migrations/") or normalized.startswith("scripts/maintenance/")


def find_bypasses(files: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for path, content in files.items():
        if ENGINE_PATTERN.search(content) and not _allows_direct_engine_creation(path):
            findings.append(f"{path}: direct tenant engine creation")
        if ORG_SCHEMA_PATTERN.search(content) and not _allows_explicit_org_schema(path):
            findings.append(f"{path}: explicit org schema reference")
    return findings


def exit_code_for_findings(findings: list[str], *, advisory: bool = False) -> int:
    if advisory:
        return 0
    return 1 if findings else 0


def _read_files(paths: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for root in paths:
        for path in Path(root).rglob("*.py"):
            normalized = path.as_posix()
            if normalized.startswith("scripts/maintenance/"):
                continue
            files[str(path)] = path.read_text(encoding="utf-8")
    return files


def _default_scan_roots(
    *,
    include_scripts: bool = False,
    include_tests: bool = False,
) -> list[str]:
    roots = ["src"]
    if include_scripts:
        roots.append("scripts")
    if include_tests:
        roots.append("tests")
    return roots


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--advisory", action="store_true")
    parser.add_argument("--include-scripts", action="store_true")
    parser.add_argument("--include-tests", action="store_true")
    args = parser.parse_args()
    findings = find_bypasses(
        _read_files(
            _default_scan_roots(
                include_scripts=args.include_scripts,
                include_tests=args.include_tests,
            )
        )
    )
    for finding in findings:
        print(finding)
    raise SystemExit(exit_code_for_findings(findings, advisory=args.advisory))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify green**

Run:

```powershell
uv run pytest tests\scripts\maintenance\test_check_tenant_runtime_bypasses.py -v
```

Expected: all static checker tests pass.

## Task 10: Documentation and Rollout Checklist

**Files:**
- Modify: `docs/Technical Documentation/search-path-tenant-pooling.md`

- [ ] **Step 1: Replace patch-era language**

Update the document to describe:

```text
Phase 1:
  dedicated tenant runtime login named app_tenant_runtime by default
  password in the master/shared Key Vault
  one tenant engine per process
  SET LOCAL search_path per transaction to the tenant schema plus locked extensions schema
  public is not in the tenant runtime search_path
  master/public tables should use master sessions; any exceptional tenant-runtime public access must explicitly qualify public.table and be reviewed
  runtime role default search_path = pg_catalog for the target database
  public schema CREATE revoked from PUBLIC
  trusted extensions schema exists, has CREATE revoked from PUBLIC, grants USAGE to tenant runtime, and grants USAGE to per-org owner roles only for rollback lookup
  pgvector extension lives in the trusted extensions schema
  per-org roles untouched
  USE_SHARED_TENANT_DATABASE remains the rollback flag

Phase 2:
  convert per-org roles to NOLOGIN PASSWORD NULL after rollback window

Phase 3:
  future shared-schema + organisation_id + RLS migration
```

- [ ] **Step 2: Add lower-env verification**

Add this checklist:

```text
1. Provision tenant runtime role in lower env.
2. Confirm default runtime role `SHOW search_path` returns `pg_catalog`.
3. Confirm runtime role cannot `CREATE` in `public`.
4. Backfill grants dry-run.
5. Backfill grants execute.
6. Enable `USE_SHARED_TENANT_DATABASE=true`.
7. Run two-org same-process isolation test.
8. Query after commit inside same session and verify correct schema.
9. Verify tenant runtime `SHOW search_path` inside an acquired tenant session contains the org schema and `extensions`, not `public`.
10. Query without provider tenant context and verify tenant table access fails.
11. Query `SELECT '[1,2,3]'::vector` through a tenant session and verify pgvector resolves through `extensions`.
12. Query any required master table through a master session; tenant-runtime unqualified master table access must fail.
13. Inspect PgBouncer pools by user/database.
14. Roll back by disabling `USE_SHARED_TENANT_DATABASE` and using legacy per-org roles.
15. If provisioning/rotation writes Key Vault but PostgreSQL commit fails, re-run `provision_tenant_runtime_role.py --execute --rotate-password` and verify the app can connect with the current Key Vault password before enabling shared runtime.
```

- [ ] **Step 3: Run markdown/basic verification**

Run:

```powershell
rg "master DB password|SET ROLE" "docs\Technical Documentation\search-path-tenant-pooling.md"
```

Expected:

- No claim that master DB password is acceptable for tenant runtime.
- `SET ROLE` appears only in the rejected/phase-later discussion.

## Full Verification

Run:

```powershell
uv run pytest tests\infra\test_tenant_runtime_config.py tests\infra\test_tenant_runtime_credentials.py tests\infra\test_tenant_search_path.py tests\infra\test_tenant_runtime_grants.py tests\infra\test_async_db_provider.py tests\infra\test_sync_db_provider.py tests\service\test_organization_management_shared_tenant.py tests\api\test_fastapi_app_tenant_runtime_startup.py tests\workers\test_celery_tenant_runtime_startup.py tests\scripts\maintenance\test_provision_tenant_runtime_role.py tests\scripts\maintenance\test_grant_tenant_runtime_schema_access.py tests\scripts\maintenance\test_check_tenant_runtime_bypasses.py tests\migrations\test_org_migration_search_path.py -v
```

Expected: all selected tests pass.

Run:

```powershell
uv run ruff check src\api\fastapi_app.py src\workers\base\celery_base.py src\infra\config\configs.py src\infra\database\tenant_runtime_config.py src\infra\database\tenant_runtime_credentials.py src\infra\database\tenant_search_path.py src\infra\database\tenant_runtime_grants.py src\infra\database\provider_async.py src\infra\database\provider_sync.py src\infra\utils\async_runner.py src\service\orgnisation\organization_management.py scripts\maintenance\provision_tenant_runtime_role.py scripts\maintenance\grant_tenant_runtime_schema_access.py scripts\maintenance\check_tenant_runtime_bypasses.py tests\api\test_fastapi_app_tenant_runtime_startup.py tests\workers\test_celery_tenant_runtime_startup.py tests\infra\test_tenant_runtime_config.py tests\infra\test_tenant_runtime_credentials.py tests\infra\test_tenant_search_path.py tests\infra\test_tenant_runtime_grants.py tests\infra\test_async_db_provider.py tests\infra\test_sync_db_provider.py tests\service\test_organization_management_shared_tenant.py tests\scripts\maintenance\test_provision_tenant_runtime_role.py tests\scripts\maintenance\test_grant_tenant_runtime_schema_access.py tests\scripts\maintenance\test_check_tenant_runtime_bypasses.py
```

Expected: `All checks passed!`

Run:

```powershell
python -m py_compile scripts\maintenance\provision_tenant_runtime_role.py scripts\maintenance\grant_tenant_runtime_schema_access.py scripts\maintenance\check_tenant_runtime_bypasses.py
```

Expected: exit code 0.

Run:

```powershell
uv run python -m scripts.maintenance.check_tenant_runtime_bypasses
```

Expected: exit code 0. Any finding is a release blocker unless it is moved behind an explicit migration or maintenance allowlist.

## Non-Goals

- Do not move tenant data into `public`.
- Do not implement the future shared-schema `organisation_id` model in this plan.
- Do not drop per-org roles.
- Do not delete per-org Key Vault secrets.
- Do not use master DB credentials for tenant runtime.
- Do not add `SET ROLE` to runtime v1.

## Resolved Execution Decisions

1. Replace the current patch-shaped implementation with the cleaner module split in this plan. Reuse correct code only when it matches the planned file boundary.
2. Default production role name is `app_tenant_runtime`; environments can override with `TENANT_DATABASE_USER`.
3. Tenant runtime password lives in the master/shared Key Vault through `AZURE_MASTER_KEY_VAULT_URL`.
4. `USE_SHARED_TENANT_DATABASE` is the first-rollout rollback flag. When false, legacy per-org role/password runtime remains available.
5. Password rotation is never implicit. Use `scripts/maintenance/provision_tenant_runtime_role.py --execute --rotate-password` only for a deliberate rotation.
6. Static tenant-runtime bypass checks are strict by default over runtime `src`; `--include-scripts` includes production scripts after allowing known migration/cleanup/maintenance engine usage and excluding `scripts/test_local/**`, while `--include-tests` and `--advisory` are only for local exploration while allowlists are tuned.
7. Runtime role default `search_path` is set with `ALTER ROLE ... IN DATABASE ...`, not a global role setting. Shared tenant runtime sessions use `SET LOCAL search_path` with the tenant schema plus the locked `extensions` schema; `public` is not added to shared tenant runtime search paths. Legacy rollback sessions use tenant schema, `extensions`, then `public` so pre-shared per-org behavior remains available.
8. Sync tenant runtime credential loading uses `src.infra.secrets.master_key_vault_sync` with the master/shared vault URL and the shared sync Azure credential strategy; it never calls `asyncio.run()` inside the provider and never builds `DefaultAzureCredential` directly in provider/provisioning code.
9. Runtime role password creation/rotation stores the master/shared Key Vault secret inside the open PostgreSQL transaction after password SQL and before commit in Key Vault mode, so Key Vault write failure rolls back the DB password change. Direct-password mode skips Key Vault client creation and secret writes.
10. The release-gate static checker scans runtime `src` by default. Existing local investigation scripts and intentionally bad test examples are outside the production gate; maintenance scripts and the known top-level cleanup script remain allowlisted.
11. Providers keep `_shared_tenant_engine` as the shared-runtime singleton field so existing fork/loop cleanup continues to clear the correct pooled engine.
12. The trusted PostgreSQL extension schema is `extensions`; it exists only to expose extension objects such as pgvector to tenant sessions without trusting `public`.
13. Org migrations set `search_path` to tenant schema, `extensions`, then `public` so initial unqualified pgvector `VECTOR` columns work after extension hardening.

## Self-Review

- Spec coverage: covers production tenant runtime login, Key Vault password, per-org schemas, trusted extension schema for pgvector, transaction-scoped search path safety across commit boundaries, startup fail-fast validation, provider session-context glue, grant backfill, new-org provisioning with legacy secret order preserved, stale patch cleanup, strict bypass checks, tests, docs, and future shared-schema direction.
- Placeholder scan: no unresolved placeholder markers.
- Type consistency: config, credential loader, provider, and script names are consistent across tasks.
