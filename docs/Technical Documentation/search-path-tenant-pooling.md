# Search Path and Tenant Runtime Pooling

This document describes the phase-1 production model for tenant database runtime
access. It keeps per-organization schemas, removes per-org runtime connection
pool fragmentation, and preserves per-org roles for rollback.

## Current Problem

CopilotGTM isolates tenant tables in schemas named from the organization id.
The old runtime path also connected with a per-organization PostgreSQL login.
That shape does not scale cleanly with PgBouncer transaction pooling because
PgBouncer pools by database and user:

```text
postgres + org_a_role -> pool A
postgres + org_b_role -> pool B
postgres + org_c_role -> pool C
```

At large tenant counts, idle capacity is fragmented across many user pools.

## Phase 1 Target

Phase 1 introduces a dedicated tenant runtime login named
`app_tenant_runtime` by default. Environments can override it with
`TENANT_DATABASE_USER`.

The runtime password is stored in the master/shared Key Vault under
`TENANT_DATABASE_PASSWORD_SECRET_NAME`. Local or emergency deployments may set
`TENANT_DATABASE_PASSWORD` directly, but production should prefer Key Vault.
API and Celery startup validate both the tenant runtime config and actual
password availability before serving requests or running jobs.
Celery prefork child initialization performs config-only validation; the
network Key Vault password check runs in the main worker startup path to avoid
blocking child fork startup.

The runtime shape is:

```text
master/admin provider
  -> DATABASE_USER / DATABASE_PASSWORD
  -> public schema, provisioning, migrations, admin operations

tenant runtime provider
  -> TENANT_DATABASE_USER / tenant runtime password
  -> one shared tenant engine per process
  -> SET LOCAL search_path per transaction
```

Tenant sessions set the transaction-scoped path to the tenant schema plus the
locked trusted extension schema:

```sql
SET LOCAL search_path TO "<org_schema>", "extensions";
```

`public` is not in the tenant runtime `search_path`. Master/public tables should
use master sessions. Any exceptional access from tenant runtime code must
explicitly qualify `public.table` and be reviewed.

The runtime role default path is fail-closed for the target database:

```sql
ALTER ROLE "app_tenant_runtime" IN DATABASE "<database>" SET search_path = pg_catalog;
```

The provisioning script also revokes unnecessary public schema writes:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

## Trusted Extensions

PostgreSQL extensions expose types, operators, and functions through a schema.
This repo uses pgvector casts such as `::vector`, so tenant sessions need a
trusted extension schema in the path without trusting `public`.
Organization migrations also include `extensions` in their migration
`search_path` because the initial org migration emits an unqualified pgvector
`VECTOR` type.

Phase 1 provisions:

```sql
CREATE SCHEMA IF NOT EXISTS "extensions" AUTHORIZATION "<admin_role>";
REVOKE CREATE ON SCHEMA "extensions" FROM PUBLIC;
GRANT USAGE ON SCHEMA "extensions" TO "app_tenant_runtime";
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA "extensions";
ALTER EXTENSION vector SET SCHEMA "extensions";
```

The grant backfill also grants `USAGE` on `extensions` to each per-org owner
role. That does not make the schema writable; it only keeps rollback mode able
to resolve extension types/operators if pgvector has moved out of `public`.

Only extension objects belong in `extensions`; tenant tables remain in org
schemas and global tables remain in `public`.

## Provider Boundary

The provider owns tenant context. Application services should acquire tenant
sessions through the provider and should not set `search_path` directly.

Async runtime password loads use `master_secrets_manager`. Sync runtime password
loads use `src.infra.secrets.master_key_vault_sync`, which builds the master
Key Vault client through the shared Azure credential strategy instead of
provider-local `DefaultAzureCredential` construction.

SQLAlchemy applies tenant context when each transaction begins. This matters for
PgBouncer transaction pooling because a session can use different backend
connections across transaction boundaries.

If code commits and then queries again in the same acquired tenant session, the
next transaction runs the same provider hook and reapplies:

```sql
SET LOCAL search_path TO "<org_schema>", "extensions";
```

When `USE_SHARED_TENANT_DATABASE=false`, rollback sessions use:

```sql
SET LOCAL search_path TO "<org_schema>", "extensions", "public";
```

This preserves the old `public` fallback for per-org roles while still allowing
extension lookup after the trusted extension schema has been provisioned.

## Provisioning and Backfill

New organization provisioning still creates the per-org schema and per-org role.
Those roles and their Key Vault secrets remain untouched in the first release.

Provisioning then grants the dedicated tenant runtime role access to:

- the org schema with `USAGE`
- existing tables with `SELECT, INSERT, UPDATE, DELETE`
- existing sequences with `USAGE, SELECT, UPDATE`
- existing functions with `EXECUTE`
- the trusted `extensions` schema with `USAGE` for the per-org owner role,
  solely for rollback compatibility
- default privileges for future objects created by migration and owner roles

Existing organizations are handled by the dry-run-first backfill script:

```powershell
uv run python -m scripts.maintenance.grant_tenant_runtime_schema_access --all-orgs
uv run python -m scripts.maintenance.grant_tenant_runtime_schema_access --all-orgs --execute
```

`values-dev.yaml` and `values-prod.yaml` enable the shared tenant runtime path
with `USE_SHARED_TENANT_DATABASE=true`,
`TENANT_DATABASE_USER=app_tenant_runtime`, and
`TENANT_DATABASE_PASSWORD_SECRET_NAME=shared-tenant-db-password`. Do not deploy
those values until the grant backfill has been executed and verified for all
existing organisation schemas in that environment; otherwise approved tenants
without grants will fail after the rollout.

When `TENANT_DATABASE_PASSWORD` is supplied directly, provisioning creates or
verifies the PostgreSQL role with that password and does not require a Key Vault
client. In Key Vault mode, the script creates or verifies the secret in the
master/shared vault.

## Rollback

`USE_SHARED_TENANT_DATABASE` remains the runtime rollback flag.

When it is `false`, legacy per-org runtime credentials remain available. Do not
drop per-org roles, delete per-org Key Vault secrets, or convert roles to
`NOLOGIN` in phase 1.

Rollback sessions keep `public` in their path because that is the pre-shared
runtime behavior. The shared tenant runtime path still excludes `public`.

If runtime role provisioning or password rotation writes Key Vault but the
PostgreSQL commit fails afterward, rerun:

```powershell
uv run python -m scripts.maintenance.provision_tenant_runtime_role --execute --rotate-password
```

Then verify the app can connect with the current Key Vault password before
enabling shared runtime.

## Phase 2

After the rollback window, per-org roles can be converted to:

```sql
ALTER ROLE "<org_role>" NOLOGIN PASSWORD NULL;
```

They can still own schemas and objects if avoiding ownership churn is valuable.
Do this only after evidence shows shared runtime is stable.

`SET ROLE` remains a later hardening option, not part of phase 1 runtime.
Changing effective role does not remove the need for provider-owned tenant
schema selection.

## Phase 3

The future long-term model can move selected tenant data to shared tables with
`organisation_id` and PostgreSQL Row Level Security. That is a separate
migration. Phase 1 does not move tenant data into `public`.

## Lower-Env Verification

1. Provision tenant runtime role in lower env.
2. Confirm default runtime role `SHOW search_path` returns `pg_catalog`.
3. Confirm runtime role cannot `CREATE` in `public`.
4. Backfill grants dry-run.
5. Backfill grants execute.
6. Enable `USE_SHARED_TENANT_DATABASE=true`.
7. Run two-org same-process isolation test.
8. Query after commit inside same session and verify correct schema.
9. Verify tenant runtime `SHOW search_path` inside an acquired tenant session
   contains the org schema and `extensions`, not `public`.
10. Query without provider tenant context and verify tenant table access fails.
11. Query `SELECT '[1,2,3]'::vector` through a tenant session and verify pgvector
    resolves through `extensions`.
12. Query any required master table through a master session; tenant-runtime
    unqualified master table access must fail.
13. Inspect PgBouncer pools by user/database.
14. Roll back by disabling `USE_SHARED_TENANT_DATABASE` and using legacy per-org
    roles.
15. If provisioning/rotation writes Key Vault but PostgreSQL commit fails, rerun
    `provision_tenant_runtime_role.py --execute --rotate-password` and verify
    the app can connect with the current Key Vault password before enabling
    shared runtime.

## SOC 2 Evidence

- Architecture: per-org schemas, dedicated tenant runtime login,
  transaction-scoped `search_path`, and immutable application
  `organisation_id` boundaries.
- Access control: SQL role attributes, schema grants, default privileges,
  `public` schema `CREATE` audit, and trusted `extensions` schema grants.
- Change management: PR, focused provider/provisioning tests, rollout checklist,
  lower-env verification, and rollback to legacy per-org role mode.
- Monitoring: tenant context setup failures, PgBouncer pool count by
  user/database, DB connection saturation, and API/worker error rates during
  rollout.
- Security tests: normal provider access succeeds only with tenant context, and
  explicit org schema references outside migrations/maintenance are blocked by
  static checks and review.
- Static gate: `check_tenant_runtime_bypasses` scans runtime `src` by default,
  and `--include-scripts` is clean after migration, maintenance, cleanup, and
  `scripts/test_local/**` exclusions are applied.
