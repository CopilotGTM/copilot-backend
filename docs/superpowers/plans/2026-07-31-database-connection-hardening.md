# Database Connection Hardening Implementation Plan

> **For Codex:** Follow the TDD red-green-refactor loop and verify each task before continuing.

**Goal:** Make the existing master and shared-tenant SQLAlchemy connection providers safe under pool exhaustion, process forks, event-loop replacement, and application shutdown without introducing another connection abstraction.

**Architecture:** Keep the current four runtime pools: master and shared-tenant pools in each of the sync and async providers. Add the missing common pool controls, make provider disposal a public lifecycle operation, and have the factory own singleton reset/closure so worker and API lifecycle code never reaches into provider internals.

**Tech Stack:** Python, SQLAlchemy sync/async engines, FastAPI lifespan, Celery prefork signals, pytest.

---

### Task 1: Make pool behavior consistent

**Files:**
- Modify: `tests/infra/test_sync_db_provider.py`
- Modify: `tests/infra/test_async_db_provider.py`
- Modify: `src/infra/database/provider_sync.py`
- Modify: `src/infra/database/provider_async.py`

1. Add assertions that all four engine builders receive bounded `pool_timeout`, stale-connection validation, recycling, LIFO reuse, and rollback-on-return.
2. Run the focused provider tests and confirm the new sync/LIFO assertions fail for the expected missing options.
3. Add only the missing SQLAlchemy pool options to both providers.
4. Re-run the focused provider tests and confirm they pass.

### Task 2: Replace stale private-field cleanup with provider lifecycle APIs

**Files:**
- Modify: `tests/infra/test_async_runner.py`
- Create: `tests/infra/test_database_provider_factory.py`
- Modify: `src/infra/database/interface.py`
- Modify: `src/infra/database/provider_sync.py`
- Modify: `src/infra/database/provider_async.py`
- Modify: `src/infra/database/factory.py`
- Modify: `src/infra/utils/async_runner.py`

1. Add failing tests proving normal disposal closes both engines, fork reset detaches both singletons and disposes inherited pools with `close=False`, and loop replacement resets the async singleton through the factory.
2. Run the new tests and confirm failure comes from the absent lifecycle API and stale `_tenant_engines`/`_shared_tenant_engine` assumptions.
3. Add provider `dispose()` methods and factory-owned `close_database_providers()` / `reset_database_providers_after_fork()` functions.
4. Simplify `async_runner` to call those factory functions instead of inspecting provider private fields.
5. Re-run the lifecycle tests and confirm they pass.

### Task 3: Prove the tenant credential can authenticate

**Files:**
- Modify: `tests/infra/test_tenant_runtime_credentials.py`
- Modify: `src/infra/database/tenant_runtime_credentials.py`

1. Add failing sync and async tests requiring startup to connect through the existing shared-tenant provider and verify `current_user`.
2. Run the focused tests and confirm they fail because startup currently resolves the secret but never authenticates it.
3. Add a read-only login probe through the existing tenant session boundary; do not create another engine or connection path.
4. Re-run the credential tests and confirm they pass.

### Task 4: Close database pools during API shutdown

**Files:**
- Modify: `tests/infra/test_otel_runtime.py`
- Modify: `src/api/fastapi_app.py`

1. Extend the lifespan-order test to require database provider closure during both normal and exceptional shutdown.
2. Run that single test and confirm it fails because no closure occurs.
3. Call the factory shutdown function from the FastAPI lifespan teardown.
4. Re-run the lifespan test and confirm it passes.

### Task 5: Verify connection and tenant-isolation contracts

**Files:**
- Verify only.

1. Run:
   `pytest tests/infra/test_sync_db_provider.py tests/infra/test_async_db_provider.py tests/infra/test_database_provider_factory.py tests/infra/test_async_runner.py tests/infra/test_tenant_runtime_credentials.py tests/infra/test_tenant_search_path.py tests/infra/test_shared_tenancy_telemetry.py tests/infra/test_otel_runtime.py -q`
2. Run static checks on the changed Python files.
3. Inspect `git diff` for the scoped paths and confirm no legacy per-organisation pool or retry abstraction was introduced.
4. Report any unrelated pre-existing test failure separately; do not expand scope to fix it.
