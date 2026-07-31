# Shared Row Tenancy Architecture

Status: Proposed
Decision owner: Backend platform
Last updated: 2026-07-30
Implementation plan: `docs/superpowers/plans/2026-07-30-shared-row-tenancy-production-rollout.md`

## Decision

CopilotGTM will use one permanent PostgreSQL storage architecture:

- `public` for organisations, users, authentication mappings, billing,
  provisioning, and other control-plane data.
- `tenant_data` for CRM, deals, chat, workflows, integrations, knowledge,
  communications, meetings, jobs, and every organisation-owned row.
- `extensions` for trusted PostgreSQL extension objects such as pgvector.

The current populated `org_*` schema is a one-time migration source, not a
supported runtime mode. After data copy, integrity validation, RLS validation,
and application smoke tests complete, its schema, database role, and Key Vault
secret are deleted during the same approved cutover.

There is no tenant route table, schema-mode flag, compatibility resolver,
legacy runtime branch, dual write, retained old schema, or reverse migration.
After cutover, the system moves forward only on `tenant_data`.

## Existing Architecture We Reuse

This design keeps the repository's existing session ownership model.

### Master boundary

These existing functions remain unchanged:

- `master_session_scope()`
- `master_async_session_scope()`

They continue to use the master provider and own `public` control-plane
operations.

### Organisation boundary

These existing functions keep their current signatures:

- `org_session_scope(organisation_id)`
- `org_async_session_scope(organisation_id)`
- `acquire_org_session(organisation_id)`
- `acquire_org_async_session(organisation_id)`

After cutover, their providers always use:

```text
search_path = tenant_data, extensions
app.current_organisation_id = <trusted organisation UUID>
```

Business services continue passing `organisation_id` to the existing
organisation-session boundary. We do not add new master/session abstractions
and do not make services select a schema.

### Migration boundary

The current migration separation also remains:

- `migrations/master_migrations` owns `public`.
- `migrations/org_migrations` owns organisation tables and is run once against
  `tenant_data`.

The organisation migration runner accepts an explicit admin-only target schema
for creating and upgrading `tenant_data`. It no longer sweeps customer
schemas after cutover.

### Runtime connection boundary

`USE_SHARED_TENANT_DATABASE` continues to select the existing shared database
connection and `app_tenant_runtime` credential. It is not repurposed as a
schema-selection flag. Schema selection becomes constant in the organisation
providers: `tenant_data`.

## Why This Is the Smallest Correct Design

Today, organisation provisioning can create:

1. A `public.organisations` record.
2. A PostgreSQL schema.
3. A PostgreSQL login role and password.
4. A per-organisation Key Vault secret.
5. A complete organisation migration run.
6. Internal CRM defaults.
7. A storage container.

Open signup should create control-plane rows and tenant defaults, not database
infrastructure.

Because only one current organisation dataset must be moved, supporting two
runtime storage systems would add permanent code for a temporary event. A
maintenance-window clean break is simpler:

1. Stop every writer.
2. Back up and export.
3. Create and protect `tenant_data`.
4. Copy and validate the current data.
5. Deploy the permanent organisation provider.
6. Smoke test.
7. Delete the source schema, role, and secret.
8. Reopen traffic.

## Preconditions

The cutover starts only when:

1. Exactly one `org_*` schema contains customer rows.
2. Its size fits the approved maintenance window.
3. API and Celery processes can be fully stopped.
4. Active, reserved, and scheduled organisation jobs can be drained.
5. A restorable database backup exists.
6. A deterministic tenant export exists in encrypted storage.
7. Restore into `tenant_data` has been rehearsed in non-production.

If these assumptions fail, the cutover stops. We do not add compatibility
architecture automatically.

## Goals

- Make `tenant_data` the only organisation runtime schema.
- Preserve existing master and organisation session APIs.
- Run organisation migrations once.
- Enforce tenant isolation with PostgreSQL RLS and database constraints.
- Make missing or malformed tenant context fail closed.
- Remove per-organisation database provisioning.
- Delete the old schema, database role, and Key Vault secret during cutover.
- Provide verified backup, export, and restore for disaster recovery.
- Support open signup without database DDL.

## Non-Goals

- Supporting both old and new runtime storage.
- Per-organisation storage routing or feature flags.
- One database per organisation.
- Direct customer PostgreSQL access.
- New service/repository abstractions for schema selection.
- Renaming or duplicating tenant models.
- Multi-organisation user membership in this change.
- Partitioning tables before measurements justify it.
- Using the old schema as a recovery mechanism.

## Permanent Runtime

```text
Supabase authentication
        |
        v
public.users
  user_id
  organisation_id
        |
        v
existing org_session_scope(organisation_id)
        |
        v
AsyncSqlAlchemyProvider / SyncSqlAlchemyProvider
  search_path = tenant_data, extensions
  SET LOCAL app.current_organisation_id = trusted UUID
        |
        v
app_tenant_runtime
  NOBYPASSRLS
  not a table owner
  no DDL
        |
        v
tenant_data
  NOT NULL organisation_id
  tenant-qualified keys and FKs
  ENABLE + FORCE ROW LEVEL SECURITY
```

The master path remains separate:

```text
master_session_scope()
        |
        v
master provider
        |
        v
public
```

`public` is absent from tenant runtime `search_path`. Reviewed cross-schema
constraints qualify `public.organisations` explicitly.

## Non-Negotiable Invariants

1. Every organisation-owned row has non-null `organisation_id`.
2. Tenant `organisation_id` references
   `public.organisations.organisation_id` with `ON DELETE RESTRICT`.
3. Tenant-owned unique business keys include `organisation_id`.
4. Tenant relationships use composite foreign keys containing
   `organisation_id`, unless a reviewed exception proves cross-tenant linkage
   impossible.
5. Common tenant access paths have tenant-leading indexes.
6. Every tenant table has RLS enabled and forced.
7. Every tenant table defines both `USING` and `WITH CHECK`.
8. Runtime is `NOBYPASSRLS`, owns no tenant objects, and has no DDL.
9. Missing tenant context sees zero rows and cannot mutate.
10. Client input never chooses trusted tenant identity.
11. API, Celery, webhook, bot, and agent paths use the existing organisation
    session boundary.
12. Master operations remain on master sessions.
13. Cutover cannot proceed without backup, export, parity, isolation, and
    smoke evidence.
14. Old database resources are deleted only after the new runtime passes the
    cutover gates, but they are not retained beyond that cutover.
15. CI fails when any new tenant table lacks identity, constraints, RLS, or
    policies.

## Trusted Tenant Identity

### API

The tenant UUID comes from the authenticated `public.users` row. It never
comes from a request header, query parameter, body field, URL identifier, or
LLM/tool argument.

### Celery

The UUID comes from the durable job/event record. The worker rejects a queue
payload whose organisation does not match the loaded row.

### Webhooks and bots

External identity resolves through reviewed `public` mappings before the
organisation session is acquired.

### Maintenance

The one-time migration command receives the source organisation UUID,
validates it against `public.organisations`, derives the source schema through
the existing naming helper, and prints the exact source and destination.

## Transaction Context

The existing session listener sets tenant context at transaction scope:

```sql
SELECT set_config(
    'app.current_organisation_id',
    :organisation_id,
    true
);
```

The third argument must remain `true`. Session-scoped `SET`, connection
startup tenant state, and mutable global connection state are prohibited
because pooled connections are reused.

The same transaction sets:

```sql
SELECT set_config(
    'search_path',
    '"tenant_data", "extensions"',
    true
);
```

No caller supplies a schema name.

## RLS Function and Policies

The shared schema exposes one security-invoker context function:

```sql
CREATE FUNCTION tenant_data.current_organisation_id()
RETURNS uuid
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(
        current_setting('app.current_organisation_id', true),
        ''
    )::uuid
$$;

REVOKE ALL ON FUNCTION tenant_data.current_organisation_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant_data.current_organisation_id()
TO app_tenant_runtime;
```

Missing context returns `NULL`; malformed UUID text fails the transaction.

Every tenant table uses a permissive context gate plus a restrictive equality
boundary:

```sql
ALTER TABLE tenant_data.example ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_data.example FORCE ROW LEVEL SECURITY;

CREATE POLICY example_tenant_context
ON tenant_data.example
AS PERMISSIVE
FOR ALL
TO app_tenant_runtime
USING (tenant_data.current_organisation_id() IS NOT NULL)
WITH CHECK (tenant_data.current_organisation_id() IS NOT NULL);

CREATE POLICY example_tenant_isolation
ON tenant_data.example
AS RESTRICTIVE
FOR ALL
TO app_tenant_runtime
USING (
    organisation_id = tenant_data.current_organisation_id()
)
WITH CHECK (
    organisation_id = tenant_data.current_organisation_id()
);
```

PostgreSQL requires a permissive policy before restrictive policies can admit
rows. The restrictive equality policy remains an unavoidable `AND`.

The live catalog audit verifies:

- `relrowsecurity = true`
- `relforcerowsecurity = true`
- exactly the expected policies
- `qual` and `with_check`
- runtime has no `rolbypassrls`
- runtime does not own tables
- runtime cannot create in `tenant_data`

Tenant identity may default to
`tenant_data.current_organisation_id()`. `WITH CHECK` rejects an explicit
mismatched UUID.

## RLS Trust Boundary

RLS prevents accidental missing filters and wrong tenant IDs in normal
application paths.

The shared runtime credential remains trusted. A party with that credential
and arbitrary SQL execution could set another custom GUC. Compensating
controls are mandatory:

- PostgreSQL has no public network exposure.
- Key Vault credentials are never sent to clients.
- Workload identity and network policy restrict credential/database access.
- Runtime has DML only, no ownership, DDL, role management, `BYPASSRLS`, or
  `public` writes.
- Function execution is allowlisted, not `EXECUTE ON ALL FUNCTIONS`.
- Credential rotation and compromise response are rehearsed.
- Logs include organisation/request/job identity but no secrets or payloads.

## Keys, Constraints, and Indexes

Tenant parents expose candidate keys:

```sql
UNIQUE (organisation_id, deal_id)
```

Children repeat tenant identity:

```sql
FOREIGN KEY (organisation_id, deal_id)
REFERENCES tenant_data.crm_deals (organisation_id, deal_id)
ON DELETE CASCADE
```

Provider/business uniqueness is scoped:

```sql
UNIQUE (organisation_id, integration_id, external_id)
UNIQUE (organisation_id, lower(email))
UNIQUE (organisation_id, source_domain, source_event_id, event_type)
```

Common indexes lead with tenant identity:

```sql
CREATE INDEX ON tenant_data.crm_deals
    (organisation_id, updated_at DESC, deal_id);
```

PostgreSQL does not automatically index foreign keys. Each tenant FK and query
path is reviewed explicitly.

## Runtime Roles

| Principal | Purpose | Tenant privileges |
|---|---|---|
| `app_tenant_runtime` | API and Celery | RLS-bound DML |
| Master runtime | `public` operations | No tenant DML |
| Migration owner | Alembic and policy DDL | Object owner, never app runtime |
| Break-glass role | Restore and audited repair | Time-bound elevation |

Startup fails if runtime equals an owner/admin role or has unsafe privileges.

## Organisation Provisioning After Cutover

`OrganizationManagementService.create_organization()` keeps ownership of the
workflow but permanently stops creating database infrastructure.

It:

1. Creates `public.organisations`.
2. Creates users/auth mappings through existing master services.
3. Opens `org_async_session_scope(organisation_id)`, which now targets
   `tenant_data`.
4. Creates internal CRM and other required defaults under RLS.
5. Provisions optional external resources asynchronously and idempotently.

It does not:

- create an organisation schema
- create a database role/password
- create a per-org Key Vault secret
- run organisation migrations

There is no old provisioning branch.

## Open Signup

Signup:

1. Validates and rate-limits input.
2. Persists an idempotency record.
3. Creates the Supabase identity.
4. Creates control-plane organisation/user rows.
5. Creates tenant defaults through the existing organisation session.
6. Activates only after required defaults succeed.

Supabase and PostgreSQL cannot share an ACID transaction, so signup needs
idempotent workflow state and reconciliation. This is signup state, not
storage compatibility.

The current one-user-to-one-organisation model remains. A membership table is
not part of this change.

## One-Time Production Cutover

### Preparation

Before maintenance:

- model and live database audits are green
- `tenant_data` exists at organisation migration head
- forced RLS and runtime grants are verified
- encrypted database backup is restorable
- deterministic tenant export is restorable
- exact source schema, role, and secret names are recorded
- delete commands use exact names, never wildcards
- full cutover and restore are rehearsed in non-production

### Stop writers

Scale API and every Celery worker to zero. Verify:

- no running backend pods can accept requests
- Celery active/reserved/scheduled work for the organisation is empty
- no integration/webhook/bot consumer can write tenant data
- only the approved migration session remains

### Copy and validate

The data is small, so use one admin transaction:

1. Acquire one PostgreSQL advisory lock.
2. Copy tables in FK dependency order.
3. Set `organisation_id` from the validated source schema UUID.
4. Verify counts and complete primary-key sets.
5. Verify SHA-256 checksums over binary `COPY` output ordered by the complete
   primary key.
6. Verify FKs, uniqueness, organisation identity, and RLS.
7. Commit only when every check passes.

Failure before commit rolls back all destination writes.

The command emits an immutable JSON report containing revisions, table counts,
checksums, timings, tool version, and result. It does not create migration
state tables.

### Deploy and prove the permanent runtime

Deploy code where organisation providers always select `tenant_data`. Run:

- startup RLS/role checks
- login
- CRM reads and writes
- chat and jobs
- integrations and webhooks
- agents, bots, and scheduled work
- cross-tenant negative checks using sentinel organisation B

Do not reopen traffic until these pass.

### Delete the old resources

After smoke tests pass, in the same approved maintenance change:

1. Confirm the immutable copy report and backup/export references.
2. Reconfirm no code or live connection uses the source role/schema.
3. Revoke source role access.
4. Drop the exact source schema.
5. Drop the exact per-organisation database role.
6. Delete the exact per-organisation Key Vault secret.
7. Remove obsolete grants/configuration.
8. Verify the schema, role, and secret no longer exist.

Then reopen traffic.

The old schema is not a fallback. Disaster recovery restores the verified
backup/export into `tenant_data`.

## Schema Evolution

After cutover:

- master migrations continue against `public`
- organisation migrations run once against `tenant_data`
- organisation migration sweeps are removed
- new tenant tables must include identity, constraints, indexes, and RLS in
  the same change

CI migrates an empty database to head and runs the live catalog audit. No
applied migration is edited.

## Export, Delete, and Restore

Shared storage provides explicit lifecycle tooling:

- export every classified tenant table in FK dependency order
- include organisation, revision, counts, checksums, time, and tool version
- encrypt exports
- default deletion to dry-run
- delete by exact `organisation_id`
- restore only into an empty or explicitly approved destination
- run parity, RLS, and smoke checks after restore

Full database backup and per-tenant export are both required.

## Observability

Structured fields:

- `organisation_id`
- `request_id` or `job_id`
- actor identity
- operation and outcome
- cutover report ID during migration

Metrics:

- missing/invalid tenant context
- RLS policy violations
- tenant session acquisition failures
- signup latency/failure/reconciliation
- copy rows/bytes/duration/checksum failure
- database pool wait and query latency
- deadlocks, lock waits, autovacuum lag, and table growth

Logs never include passwords, tokens, tenant row payloads, email bodies, or
chat content.

## Capacity Controls

Initial production controls:

- tenant-leading indexes
- statement timeouts
- API rate limits
- bounded per-organisation worker concurrency
- queue backpressure and retry budgets
- autovacuum/bloat dashboards
- query-plan review for large tenant queries

Do not partition by organisation without measured need.

## Production Gates

### Architecture gate

- Master functions still own `public`.
- Organisation functions still own tenant sessions.
- Organisation providers select only `tenant_data`.
- No route, schema switch, or old runtime branch exists.
- Organisation migrations run once.

### Security gate

- Trusted tenant context only.
- Runtime role separation and least privilege.
- Forced RLS and both policies on every table.
- Missing-context and cross-tenant negative tests pass.
- Network, Key Vault, rotation, and break-glass controls pass.

### Data gate

- Exactly one populated source schema.
- No missing identity or unsafe tenant constraints.
- Backup and tenant export restore successfully.
- Copy counts, keys, checksums, FKs, and RLS pass.

### Cutover gate

- Every writer is stopped.
- New runtime smoke tests pass before deletion.
- Exact schema/role/secret deletion targets are reviewed.
- Post-deletion absence is verified before traffic opens.

## Success Criteria

- `tenant_data` is the only tenant runtime schema.
- Existing master/org session APIs remain the application boundary.
- New organisation creation performs no database infrastructure work.
- All tenant rows are RLS protected.
- Organisation migrations run once.
- Current data has verified parity in `tenant_data`.
- Old schema, role, secret, and code paths are gone.
- Backup/export restore into `tenant_data` is proven.

## Rejected Complexity

- Per-organisation data routing.
- Runtime schema flags.
- Compatibility providers.
- Migration-state tables.
- Dual writes.
- Reverse migration into the old schema.
- Retained old schemas for rollback.
- New session/repository abstractions.

The production design is one control-plane boundary, one tenant boundary, and
one tenant data schema.
