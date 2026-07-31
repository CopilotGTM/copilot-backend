# Shared Row Tenancy Production Rollout Implementation Plan

> **For implementation:** Execute this plan in order. Each task has a runnable
> check and an exit gate. Production mutation requires fresh approval for the
> exact environment and resources.

**Goal:** Move the one populated organisation dataset into a permanent
`tenant_data` schema protected by tenant-qualified constraints and forced RLS,
then remove the old per-organisation PostgreSQL infrastructure.

**Architecture:** Preserve the repository's existing ownership boundaries.
Master session functions and master migrations continue to own `public`.
Organisation session functions keep accepting `organisation_id`, but their
providers permanently use `tenant_data` and bind that ID as transaction-local
RLS context. The organisation migration chain runs once against `tenant_data`.
There is no runtime schema selector, compatibility provider, route table,
dual-write path, or legacy provisioning branch.

**Cutover:** The existing `org_*` schema is a copy source, not a second runtime
architecture. During one approved maintenance window, stop all writers, take a
restorable backup and tenant export, copy and validate the dataset, deploy the
permanent runtime, smoke-test it, then delete the exact source schema, role,
and Key Vault secret before reopening traffic. Recovery restores the backup or
tenant export into `tenant_data`; it does not re-enable schema-per-organisation
runtime behavior.

**Tech stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy, PostgreSQL 16,
Alembic, Celery, Redis, Supabase Auth, Azure Key Vault, pytest.

**Architecture reference:**
`docs/Technical Documentation/two-schema-tenant-storage-plan.md`

---

## Existing Boundaries to Reuse

### Master data

Keep these APIs and their `public` behavior unchanged:

- `master_session_scope()`
- `master_async_session_scope()`
- master Alembic migrations

Master data includes organisations, users, memberships, and other control-plane
records.

### Organisation data

Keep these APIs and their call signatures:

- `acquire_org_session(organisation_id)`
- `org_session_scope(organisation_id)`
- `acquire_org_async_session(organisation_id)`
- `org_async_session_scope(organisation_id)`

Change their sync and async providers once:

- search path is always `tenant_data, extensions`
- `app.current_organisation_id` is set transaction-locally from the trusted
  `organisation_id`
- the runtime role is subject to forced RLS

Business services continue to use the organisation boundary. They do not
choose schemas and do not need a second set of repositories or services.

### Migrations

- master migrations continue to target `public`
- organisation migrations target `tenant_data`
- the organisation chain runs once per environment, not once per organisation
- do not edit already-applied revisions

### Connection configuration

`USE_SHARED_TENANT_DATABASE` keeps its current meaning: use the shared
database connection/credential path. It is not a schema mode flag and must not
be repurposed.

## Delivery Rules

- Execute tasks in order; each exit gate is the next task's entry gate.
- Do not introduce `USE_SHARED_TENANT_SCHEMA` or any equivalent runtime switch.
- Do not add per-organisation routing, migration-state tables, dual writes,
  compatibility providers, or reverse-copy behavior.
- Do not edit applied Alembic migrations. Re-read both migration heads before
  creating a revision and choose the next valid revision ID.
- Do not mutate production without fresh approval for the exact action.
- The destructive cleanup accepts exact schema, role, and secret names; never
  wildcards or discovered prefixes.
- Preserve unrelated worktree changes and stage explicit paths.
- Every non-trivial database change leaves one focused runnable check.
- Documentation and local tests are not production proof. Live catalog,
  backup, copy, smoke, deletion, and post-state evidence are separate gates.

## Target File Map

### Existing files likely to change

- `src/infra/database/tenant_search_path.py`
- `src/infra/database/provider_async.py`
- `src/infra/database/provider_sync.py`
- `src/infra/database/tenant_runtime_config.py`
- `src/infra/database/tenant_runtime_grants.py`
- `src/service/orgnisation/organization_management.py`
- `migrations/org_migrations/env.py`
- `scripts/run_org_migrations.py`

### New files only where the codebase has no equivalent

- `src/infra/database/tenant_policy.py`
- `src/service/orgnisation/shared_tenancy_cutover_service.py`
- `src/service/orgnisation/shared_tenancy_lifecycle_service.py`
- `scripts/maintenance/provision_shared_tenant_schema.py`
- `scripts/maintenance/cutover_shared_tenant_schema.py`
- `scripts/maintenance/manage_shared_tenant_data.py`
- `docs/Technical Documentation/shared-row-tenancy-operations-runbook.md`

Before creating any listed file, search for an existing helper and extend it
when that is the smaller correct change.

## Phase 0: Prove the Clean-Break Assumptions

### Task 1: Complete the shared-tenancy readiness audit

**Purpose:** Prove that exactly one organisation schema contains data and
enumerate every tenant identity, constraint, index, and RLS gap.

**Files:**

- Inspect/Modify: `scripts/maintenance/audit_shared_tenant_readiness.py`
- Inspect/Modify:
  `tests/scripts/maintenance/test_audit_shared_tenant_readiness.py`
- Reference: `src/domain/models/organisation_tables/__init__.py`

- [ ] Write structural-audit tests for:
  - unclassified organisation tables
  - absent or nullable `organisation_id`
  - unscoped uniqueness
  - child relationships that are not tenant-qualified
  - missing tenant-leading indexes
  - tables without enabled and forced RLS
  - unsafe ownership or `BYPASSRLS`
- [ ] Add a read-only live mode that reports populated `org_*` schemas, table
  counts, estimated bytes, migration heads, and the exact schema-to-
  organisation mapping without printing credentials.
- [ ] Make `clean_break_eligible=true` only when exactly one source schema has
  tenant rows and its identity maps to one master organisation.
- [ ] Run:

  ```powershell
  uv run pytest tests/scripts/maintenance/test_audit_shared_tenant_readiness.py -v
  uv run python -m scripts.maintenance.audit_shared_tenant_readiness --models --json
  uv run python -m scripts.maintenance.audit_shared_tenant_readiness --database --json
  ```

**Exit gate:** The audit provides a machine-readable gap list and live
evidence of one populated source dataset. If this assumption is false, stop
and redesign; do not add speculative mixed-mode machinery.

## Phase 1: Make Tenant Identity a Database Invariant

### Task 2: Bind organisation identity to every org transaction

**Files:**

- Modify: `src/infra/database/tenant_search_path.py`
- Modify: `src/infra/database/provider_async.py`
- Modify: `src/infra/database/provider_sync.py`
- Modify: `tests/infra/test_tenant_search_path.py`
- Create only if needed: `tests/infra/test_tenant_context_pooling.py`

- [ ] Write failing sync and async tests proving:
  - malformed or missing organisation UUID fails before yielding a session
  - `set_config('app.current_organisation_id', :id, true)` is parameterised
  - pooled connection reuse from A to B exposes only B's context
  - commit and rollback clear the transaction-local value
  - nested transactions cannot broaden context
- [ ] Store the validated UUID on the existing org session and set it from the
  existing transaction-begin hook. Do not create a new session abstraction.
- [ ] Keep master sessions untouched.
- [ ] Run:

  ```powershell
  uv run pytest tests/infra/test_tenant_search_path.py tests/infra/test_tenant_context_pooling.py -v
  ```

**Exit gate:** Every org transaction carries a trusted, transaction-local
organisation UUID, while master transactions remain unchanged.

### Task 3: Add missing organisation identity to the empty shared schema

**Files:**

- Modify only organisation models reported by Task 1
- Create the next org migration for missing organisation identity
- Create one focused migration test

- [x] Test that the revision:
  - validates the explicit destination schema name
  - adds columns nullable first
  - performs no organisation-row copy or backfill
  - adds tenant indexes and the required master-organisation reference
- [x] Reuse existing identity mixins and touch only audit findings.
- [x] Fail closed if pointed at populated data; selective cutover owns all
  legacy row copying.
- [x] Run the focused migration test, migration head contract, and model audit.

**Exit gate:** Every tenant table in the empty destination has a non-null,
indexed `organisation_id` contract before selected data is copied.

## Phase 2: Prevent Cross-Tenant Relationships

### Task 4: Harden CRM tenant integrity

**Files:**

- Modify only affected CRM models
- Create the next org migration
- Create one focused migration contract test

- [x] Derive the test cases from Task 1 rather than maintaining a hand-written
  duplicate table inventory.
- [x] Add tenant-qualified candidate keys, child foreign keys, business
  uniqueness, provider uniqueness, and query indexes where findings require
  them.
- [x] Preserve globally unique UUID primary keys.
- [x] For large tables, create supporting indexes safely, add constraints
  `NOT VALID` where PostgreSQL allows, validate, then remove superseded
  unscoped rules.
- [x] Run the migration test, CRM service tests, and structural audit.

**Exit gate:** The database cannot form a cross-tenant CRM relationship.

### Task 5: Harden remaining tenant integrity

**Files:**

- Modify only remaining organisation models reported by Task 1
- Create the next org migration
- Create one focused migration contract test

- [x] Cover every remaining audit finding, with special attention to queue
  jobs, integrations, messaging, email, calendar, knowledge, attachments, and
  externally supplied identifiers.
- [x] Add tenant-qualified relationships, scoped uniqueness, and tenant-leading
  indexes.
- [x] Run the focused test, affected service tests, and the structural audit.

Expected structural result:

```text
missing_organisation_id = []
nullable_organisation_id = []
unscoped_unique_rules = []
unscoped_tenant_foreign_keys = []
missing_deterministic_keys = []
```

**Exit gate:** Every organisation model satisfies the shared-row integrity
contract.

## Phase 3: Create and Protect the Permanent Org Schema

### Task 6: Terraform owns `tenant_data`; Alembic targets explicit schemas

**Files:**

- Modify: `infra/terraform/azure/dev/provider.tf`
- Modify: `infra/terraform/azure/dev/main.tf`
- Modify: `infra/terraform/azure/prod/provider.tf`
- Modify: `infra/terraform/azure/prod/main.tf`
- Modify: `migrations/org_migrations/env.py`
- Modify: `scripts/run_org_migrations.py`
- Create focused Terraform and migration-target tests

- [x] Write failing tests proving:
  - Terraform creates and protects `tenant_data` once in dev and prod
  - Alembic never creates a schema
  - the existing org Alembic chain accepts an explicit schema
  - `tenant_data` and arbitrary empty test schemas require no organisation UUID
  - the schema migration chain performs no legacy organisation backfill
  - unsafe schema identifiers are rejected
- [x] Extend the existing org migration runner rather than create a second
  migration system.
- [x] Keep schema selection as an explicit CLI argument, not an environment
  variable or runtime routing mode.
- [x] Verify against ephemeral PostgreSQL that `tenant_data` reaches the
  current org head.

**Exit gate:** Terraform owns the permanent schema and the unchanged org
Alembic chain can populate `tenant_data` or an explicitly named empty test
schema without per-organisation database infrastructure.

### Task 7: Enable and force RLS

**Files:**

- Create only if no equivalent exists:
  `src/infra/database/tenant_policy.py`
- Create the next org migration for RLS
- Create focused migration and real-PostgreSQL isolation tests
- Modify the readiness audit to inspect policies

- [x] Under the actual runtime role, test:
  - no context reads zero rows
  - no-context writes fail
  - A reads and mutates only A
  - A cannot insert, update, or delete B
  - A cannot change an owned row to B
  - A cannot create a child relationship to B
  - pooled connections leak no tenant context
  - runtime is neither table owner nor `BYPASSRLS`
- [x] Add one fail-closed function that reads
  `app.current_organisation_id`.
- [x] Enable and force RLS on every classified tenant table.
- [x] Give every policy both `USING` and `WITH CHECK`.
- [x] Make future org-table migrations use the same policy helper or
  convention.
- [x] Run the isolation suite and live catalog audit.

**Exit gate:** Forced RLS and tenant-qualified constraints independently
prevent cross-tenant access.

## Phase 4: Make the Existing Org Boundary Permanent

### Task 8: Point existing org providers permanently at `tenant_data`

**Files:**

- Modify: `src/infra/database/provider_async.py`
- Modify: `src/infra/database/provider_sync.py`
- Modify: `src/infra/database/tenant_runtime_config.py`
- Create: `src/infra/database/tenant_runtime_readiness.py`
- Modify existing provider/session tests
- Create: `tests/infra/test_shared_tenant_provider_postgresql.py`

- [x] Write failing tests proving:
  - every org provider uses exactly `tenant_data, extensions`
  - callers can supply only `organisation_id`, never a schema
  - sync and async behavior is identical
  - both paths bind the trusted organisation UUID
  - `public` is absent from the org search path
  - startup fails if schema, RLS, policies, ownership, or grants are unsafe
  - master providers still target `public`
  - no schema mode setting or per-organisation branch exists
- [x] Replace schema derivation in the existing org providers with the
  `tenant_data` constant.
- [x] Keep all existing master and org session function signatures.
- [x] Remove runtime use of `get_schema_name(organisation_id)` and
  per-organisation credentials from providers.
- [x] Do not add `USE_SHARED_TENANT_SCHEMA`; keep
  `USE_SHARED_TENANT_DATABASE` semantics unchanged.
- [x] Run provider, session, context-pooling, RLS, and master-session tests.

**Exit gate:** There is one deployed org runtime path: existing org session
APIs backed by `tenant_data` and RLS.

### Task 9: Delete per-organisation provisioning behavior

**Files:**

- Modify: `src/service/orgnisation/organization_management.py`
- Modify affected tests
- Modify downstream setup code only if it bypasses org sessions

- [x] Write tests proving organisation creation:
  - creates or reuses the master organisation and records creator identity;
    membership creation belongs to the later user lifecycle workflow
  - creates required tenant defaults through the existing org session
  - is idempotent
  - creates no schema
  - creates no database role or password
  - writes no per-org Key Vault database secret
  - runs no per-org Alembic sweep
- [x] Remove the old schema/role/password/secret/migration provisioning calls
  from `OrganizationManagementService.create_organization`.
- [x] Remove now-dead helpers and tests only after repository-wide caller
  searches prove they have no other supported use.
- [x] Do not retain a conditional legacy branch.
- [x] Run organisation-management and default-data setup tests.

**Exit gate:** Organisation creation has one path and no longer provisions
database infrastructure.

## Phase 5: Build Forward-Only Cutover and Recovery Tools

### Task 10: Build transactional copy and validation

**Files:**

- Create or extend:
  `src/service/orgnisation/shared_tenancy_cutover_service.py`
- Create: `scripts/maintenance/cutover_shared_tenant_schema.py`
- Create one focused failure-injection test

- [x] Test:
  - read-only precheck is default
  - exactly one populated source schema is required
  - source schema and organisation identity must match
  - source and destination migration heads must be compatible
  - destination must be empty for that organisation
  - advisory lock prevents concurrent execution
  - writer-shutdown evidence and a backup reference are required
  - tables copy in FK dependency order
  - any copy failure rolls back the complete destination transaction
  - counts, primary-key sets, checksums, FKs, uniqueness, and RLS validate
  - sentinel data for another organisation remains unchanged
- [x] Implement only:

  ```text
  precheck --organisation-id --report
  copy-and-validate --organisation-id --writers-stopped-evidence --backup-reference --report --apply
  ```

- [x] Produce one immutable JSON report with identities, revisions, counts,
  checksums, timings, tool version, and result. Never log row payloads or
  credentials.
- [x] Do not implement reverse copy.

**Exit gate:** One transaction can copy and validate the existing dataset;
failure leaves `tenant_data` unchanged.

### Task 11: Build exact source-resource deletion

**Files:**

- Extend: `scripts/maintenance/cutover_shared_tenant_schema.py`
- Extend the cutover service only if required
- Extend the focused cutover test

- [x] Add failure tests proving deletion refuses:
  - missing `--apply`
  - wildcard or prefix targets
  - targets that do not exactly match the approved manifest
  - missing backup/export references
  - incomplete copy-validation evidence
  - missing permanent-runtime smoke evidence
  - active source sessions or dependencies
- [x] Implement:

  ```text
  delete-source-resources \
    --source-schema <exact-name> \
    --source-role <exact-name> \
    --source-secret <exact-key-vault-secret-name> \
    --cutover-report <path> \
    --smoke-report <path> \
    --apply
  ```

- [x] Delete in dependency order:
  1. terminate/refuse remaining source usage according to the approved runbook
  2. drop the exact source schema
  3. drop the exact source role
  4. delete the exact Key Vault secret
- [x] Verify all three are absent and write a new immutable, SHA-256-linked
  source-deletion companion beside the original cutover report. Never rewrite
  the original evidence file.
- [x] Never discover deletion targets from `org_*` wildcards.

**Exit gate:** The tested command can remove only the three explicitly
approved source resources and proves their absence.

### Task 12: Support per-tenant export, delete, and restore

**Files:**

- Create or extend:
  `src/service/orgnisation/shared_tenancy_lifecycle_service.py`
- Create: `scripts/maintenance/manage_shared_tenant_data.py`
- Create one focused lifecycle test

- [x] Reuse the cutover table inventory and checksum implementation.
- [x] Test export manifests, exact-tenant delete, empty-target restore,
  optional approved replacement, parity validation, and RLS isolation.
- [x] Provide:

  ```text
  export --organisation-id --destination
  delete --organisation-id
  delete --organisation-id --approval-token --apply
  restore --organisation-id --manifest --apply
  verify --organisation-id --manifest
  ```

- [x] Prove export, delete, and restore of A leaves B unchanged.

**Exit gate:** Disaster recovery and tenant lifecycle operate inside
`tenant_data`; they do not recreate `org_*` runtime architecture.

## Deferred work: Public self-service signup

Public signup is intentionally outside this rollout. Its durable state,
Supabase reconciliation, abuse controls, migration, endpoint, and tests are
specified separately in
`docs/Technical Documentation/deferred-public-signup-design.md`.

Do not add that feature until the shared-row cutover is stable and a separate
signup task is approved.

## Phase 7: Prove and Execute the Clean Break

### Task 15: Add telemetry and an exact operations runbook

**Files:**

- Create:
  `docs/Technical Documentation/shared-row-tenancy-operations-runbook.md`
- Modify already-touched runtime modules only where telemetry is emitted
- Add focused telemetry contract tests

- [x] Log IDs, operation, duration, and outcome without passwords, tokens,
  connection strings, or row payloads.
- [x] Alert on tenant-context failure, RLS violations, startup gate failure,
  copy/validation failure, pool wait, latency,
  deadlocks, locks, autovacuum lag, and table/index growth.
- [x] Document exact commands and evidence for:
  - read-only audit
  - backup restore proof and tenant export
  - API/Celery shutdown and queue drain
  - `tenant_data` provisioning and catalog validation
  - copy and parity validation
  - permanent runtime deployment
  - master/org smoke tests
  - exact source schema/role/secret deletion
  - post-deletion catalog, Key Vault, and static dependency checks
  - traffic reopen
  - disaster recovery into `tenant_data`
- [x] Do not document a legacy runtime fallback.

**Exit gate:** Operators can perform the complete clean break with tested
commands and immutable evidence.

### Task 16: Rehearse the complete clean break in non-production

**Files:** No source changes unless the drill exposes a defect.

- [ ] Seed representative A data across every classified tenant table and B
  sentinel data for isolation.
- [ ] Stop all writers, prove queue drain, create and restore-test the backup,
  export A, provision `tenant_data`, and copy/validate A.
- [ ] Deploy the permanent org provider, run master and org smoke tests, then
  execute exact source schema/role/secret deletion.
- [ ] Restart API and workers and prove all workflows use `tenant_data`.
- [ ] Exercise export/delete/restore into `tenant_data`.
- [ ] Meet these gates:
  - zero cross-tenant observations
  - zero pooled-context leakage
  - no unexplained parity difference
  - no source resource or code dependency after deletion
  - no per-org DDL, role, secret, or migration on organisation creation
  - no more than the approved p95 query regression
  - the full sequence fits the approved maintenance window

**Exit gate:** Non-production proves forward cutover, immediate old-resource
deletion, and recovery into `tenant_data`.

### Task 17: Execute the production clean break

**Files:** Deployment configuration and approved change evidence only. This
task requires fresh approval for every production mutation.

- [ ] Re-run the read-only audit and reconfirm exactly one populated source
  schema, compatible migration heads, clean integrity/RLS results, and no
  unexpected callers.
- [ ] Record exact organisation UUID, source schema, source role, source
  Key Vault secret, backup/export destinations, maintenance window, commands,
  owners, and approval.
- [ ] Stop API and every Celery worker that can write organisation data.
  Verify active, reserved, and scheduled work plus database source sessions.
- [ ] Take the full backup, prove it is readable/restorable, and create the
  tenant export.
- [ ] Provision and validate `tenant_data`, then run transactional
  copy-and-validate and store the immutable report.
- [ ] Deploy the permanent providers and provisioning code. Run master login,
  organisation reads/writes, integrations, bots, agents, jobs, and RLS
  isolation smoke tests while writers remain controlled.
- [ ] Run the approved exact deletion command for the source schema, role, and
  Key Vault secret. Verify:
  - source schema absent
  - source role absent
  - source secret absent
  - no deployed configuration or code references them
  - `tenant_data` counts/checksums and RLS still pass
- [ ] Reopen API/workers and observe the approved health gates.

**Exit gate:** Production has one master architecture (`public` through master
functions), one organisation architecture (`tenant_data` through org
functions), and no old schema, role, secret, provider branch, or provisioning
path.

## Verification Matrix

| Requirement | Authoritative evidence |
|---|---|
| Existing architecture is preserved | Master/org session boundary tests and call-site search |
| One populated source assumption is true | Live read-only readiness audit |
| Every tenant row is identified | Model audit plus live constraints |
| Cross-tenant links are impossible | Tenant-qualified FK tests |
| Runtime cannot bypass RLS | Role, ownership, forced-RLS, and policy catalog checks |
| Pooling cannot leak context | Real sync/async connection reuse tests |
| There is one org runtime | Static search plus permanent-provider tests |
| Copy is lossless and atomic | Counts, key sets, checksums, and failure injection |
| Old architecture is deleted | Catalog, Key Vault, configuration, and code evidence |
| One tenant is recoverable | Export/delete/restore parity |
| Operators can recover | Rehearsed backup and restore into `tenant_data` |

## Completion Definition

The work is complete only when:

- the existing dataset is validated in `tenant_data`
- master functions and migrations still exclusively own `public`
- org functions permanently use `tenant_data` with forced RLS
- organisation creation performs no per-org database provisioning
- the old source schema, role, secret, runtime branch, and provisioning code
  are absent
- recovery restores into `tenant_data`
- production post-state is verified after traffic is reopened

The design can be production-ready before deployment, but production success
is claimed only after the live gates above pass.
