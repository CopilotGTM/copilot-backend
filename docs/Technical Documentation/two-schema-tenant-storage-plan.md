# Two-Schema Tenant Storage Plan

## Decision

Use two logical schemas in the same PostgreSQL database:

- `public`: control-plane data such as organisations, users, admin state, auth mappings, billing, and provisioning metadata.
- organisation data schema: product/business tables such as chat, CRM, workflows, integrations, knowledge, meetings, and other organisation-owned rows.

This is not one schema per organisation as the long-term target. The target is one shared organisation data shape, with `organisation_id` on organisation-owned tables.

## Why

The current codebase already has the useful split:

- master migrations for `public`
- organisation migrations for organisation-owned tables
- `master_session_scope` / `master_async_session_scope`
- `org_session_scope` / `org_async_session_scope`
- tenant runtime search-path handling
- organisation schema naming through `get_schema_name(organisation_id)`

The scalable path is to reuse those pieces and reduce the number of physical schemas over time. We should not rename tables to `tenant_*`, create duplicate model folders, or change hundreds of service call sites just to express tenancy in names.

## Foreign Key Rule

Cross-schema foreign keys are allowed in PostgreSQL when both schemas are in the same database. This is the main condition for keeping two schemas.

Example:

```sql
CREATE TABLE public.organisations (
  organisation_id uuid PRIMARY KEY
);

CREATE TABLE organisation_data.chat_sessions (
  session_id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL
    REFERENCES public.organisations (organisation_id)
);
```

Required rules:

- Always schema-qualify cross-schema references in migrations, for example `public.organisations.organisation_id`.
- Add indexes on foreign-key columns manually; PostgreSQL does not auto-index them.
- Run master migrations before organisation migrations.
- Keep both schemas in the same database if foreign keys are required.

## What Changes

Minimal path:

1. Keep existing table names.
2. Keep existing model and service names.
3. Keep using the existing master and organisation migration directories.
4. Add `organisation_id` to organisation-owned tables that do not already have it.
5. Add cross-schema FKs from organisation-owned tables to `public.organisations`.
6. Add required FK/filter indexes, usually `organisation_id` and common composites like `(organisation_id, user_id)`.
7. Update the existing tenant/session routing layer so migrated organisations can point to the shared organisation data schema.
8. Backfill rows from existing per-organisation schemas into the shared organisation data schema with the correct `organisation_id`.
9. Keep old per-organisation schemas read-only during rollback window, then clean them after verification.

The implementation should prefer changing the database/session boundary over changing every service. If a table insert needs `organisation_id`, prefer one of these before broad service edits:

- set `organisation_id` in the existing repository/helper that already owns the write
- use a transaction-scoped session setting plus a database default/trigger where the write surface is too broad
- only pass `organisation_id` through individual services when the local API already naturally owns that value

## What Does Not Change

- Do not create `tenant_chat_sessions` or `shared_chat_sessions`.
- Do not move code into new `tenant` or `shared` model folders for naming only.
- Do not duplicate every table definition under a new naming convention.
- Do not migrate to one database per organisation.
- Do not remove current master/org session helpers; adapt them.

## Existing Tenancy Pieces To Reuse

- `src.infra.database.sessions.master_session_scope`
- `src.infra.database.sessions.master_async_session_scope`
- `src.infra.database.sessions.org_session_scope`
- `src.infra.database.sessions.org_async_session_scope`
- `src.infra.database.provider_async.AsyncDatabaseProvider.tenant_session`
- `src.infra.database.provider_sync.SyncDatabaseProvider.tenant_session`
- `src.infra.utils.resource_naming.get_schema_name`
- `scripts.run_org_migrations`
- `migrations.master_migrations`
- `migrations.org_migrations`

These are already the right ownership boundary. The change should make them route to the right schema shape, not bypass them.

## User Organisation Membership

The current `public.users.organisation_id` is enough when one user belongs to exactly one organisation.

A separate `user_organisation_memberships` table only helps if we need at least one of these product behaviours:

- one login can access multiple organisations
- the user selects an active organisation after login
- different roles per organisation for the same user
- invite/approval state per organisation
- preserving one global user identity while joining/leaving organisations independently

If the product decision is still one user -> one organisation, do not add membership now. Keep `public.users.organisation_id` and revisit membership only when multi-organisation login is a real requirement.

## Compliance Position

Two schemas are not a compliance requirement by themselves, and one schema is not a compliance blocker by itself.

SOC 2, HIPAA, and ISO 27001 readiness comes from controls:

- least-privilege roles
- tenant isolation checks
- audit logging
- encryption and backup controls
- change management
- incident response
- retention and deletion process
- access review evidence

Two schemas can make some grants and migration ownership cleaner. The important isolation control remains `organisation_id`, FK integrity, indexing, tested routing, and optional RLS/policies where needed.

## Production Rollout

1. Inventory organisation-owned tables and mark which already have `organisation_id`.
2. Add missing `organisation_id` columns, indexes, and cross-schema FKs in org migrations.
3. Create the shared organisation data schema using the existing org migration path.
4. Add routing in the existing session/provider layer so an organisation can resolve to either its legacy schema or the shared schema.
5. Backfill one low-risk organisation first.
6. Verify counts, FK validity, app reads, app writes, and rollback.
7. Flip that organisation to shared schema routing.
8. Repeat in batches.
9. Freeze legacy schemas after cutover.
10. Drop old schemas only after the rollback window and backup validation.

## Open Decision

The only remaining product decision is whether users are one-organisation or multi-organisation:

- One-organisation user: keep `public.users.organisation_id`; no membership table now.
- Multi-organisation user: add `public.user_organisation_memberships` and make login resolve an active organisation.

Until that decision is final, the storage migration should not depend on a new membership table.
