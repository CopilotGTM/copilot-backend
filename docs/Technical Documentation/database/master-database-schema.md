# Master Database Schema

Principle‑first overview of global (master) tables in the `public` schema. Use this as a template when you need a platform‑level table that is not tenant‑scoped.

## Overview

- Scope: Global. No `organisation_id` column on the table itself (except where referencing a tenant is intrinsic, e.g., a user that belongs to an organisation).
- Location: `public` schema.
- Keys: UUID primary keys, strict referential integrity, and timestamp mixins.
- Purpose: Platform governance (organisations, users, admins, auth mapping), and cross‑tenant configuration.

### Organisations (public.organisations)

- Model: `Organisation`
- Path: `src/domain/models/master_tables/organisation_models.py`
- Purpose: Registry of organisations, lifecycle/status, and audit history.

Pattern: A master entity with a corresponding history/audit table for append‑only events.

### Users (public.users)

- Model: `User`
- Path: `src/domain/models/master_tables/user_models.py`
- Purpose: Users attached to organisations; carries `supabase_user_id` for auth mapping and app/admin status flags.

Pattern: User record with both application role (`user_role`) and auth provider mapping (`supabase_user_id`).

### Super Admins (public.super_admins)

- Model: `SuperAdmin`
- Path: `src/domain/models/master_tables/super_admin_models.py`
- Purpose: Platform administrators with roles and statuses; linked to action histories.

### Organisation Integration Details (public.organisation_integration_details)

- Model: `OrganisationIntegrationDetails`
- Path: `src/domain/models/master_tables/organisation_integration_models.py`
- Purpose: Mapping of organisation connections to providers (e.g., Nango `connection_id`, `provider_config_key`).

## Conventions

- UUID primary keys; `CreatedAtMixin`/`TimestampMixin` for auditable timestamps.
- Enums for constrained state fields (e.g., organisation status, user/admin status).
- Append‑only histories for governance; avoid hard deletes in master tables.
- JSON/JSONB for flexible metadata when necessary.

## Minimal Master Table Template

```python
class MasterEntity(TimestampMixin, table=True):
	__tablename__ = "master_entities"
	__table_args__ = {"schema": "public", "comment": "Global master entity."}

	entity_id: UUID = Field(default_factory=uuid4, primary_key=True)
	name: str = Field(max_length=255, index=True)
	meta: dict[str, Any] = Field(sa_column=Column(JSONB, server_default=text("'{}'::jsonb")), default_factory=dict)
```

## Design Checklist (Master)

- Is this truly global? If tenant‑scoped, move to organisation schema and add `organisation_id`.
- Do we need an audit trail? Consider a separate `<entity>_action_history` table with append‑only events.
- Are there auth/provider mappings? Keep them in master, referenced by org/user where needed.
- Are deletes reversible? Prefer “status” fields and histories over hard deletes.

For tenant‑scoped data (chat, knowledge, integrations, synced entities, jobs), see `organisation-database-schema.md` and the consolidated `database-architecture.md`.

