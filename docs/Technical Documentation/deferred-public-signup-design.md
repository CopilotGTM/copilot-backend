# Deferred Public Signup Design

> **Historical and superseded:** this password-signup proposal and its client-owned idempotency
> contract are not implemented. The active first-user contract is defined in
> `first-user-self-service-signup-flow.md` and `../API Documentation/users-auth-api.md`: verified
> provider identity, a resumable server-side draft, and one browser-authored organisation name.

## Status and scope

This feature is intentionally deferred. It is not part of the shared-row
tenancy cutover and must not be deployed with that change.

The tenancy cutover moves existing organisation data into the permanent
`tenant_data` schema, enables forced row-level security, and removes the legacy
per-organisation database resources. Public self-service signup is a separate
product workflow that can be implemented after the cutover is stable.

No code, migration, route, or configuration described below should be assumed
to exist until a separate signup task is approved and implemented.

## Why the signup workflow needs durable state

Public signup crosses systems that cannot participate in one atomic
transaction:

1. Supabase creates the authentication identity.
2. PostgreSQL creates the public organisation and founding user.
3. The organisation session creates idempotent defaults in `tenant_data`.
4. The request becomes active.

A timeout can happen after any system has committed. Retrying a simple
create-user endpoint could therefore produce duplicate control-plane records or
leave an orphaned Supabase identity. The deferred design uses one durable
`public.signup_requests` record as the reconciliation anchor.

This table belongs to the master `public` schema because it exists before a
tenant session is available. It is not an organisation table and does not
create a schema, role, database credential, or Key Vault secret.

## Proposed API contract

`POST /users/auth/signup`

Required header:

- `Idempotency-Key`: 16-255 characters.

Accepted body:

- `email`
- `password`
- `first_name`
- `last_name`
- `organisation_name`

The request must reject extra fields so a public caller cannot choose an
organisation UUID, schema, database role, storage mode, or any other tenant
authority.

The response should be HTTP 202 with a password-free durable resource:

- `signup_request_id`
- `status`
- `organisation_id`, when created
- `user_id`, when created

Conflicts must return one generic response. The API must not reveal whether an
email, idempotency key, Supabase identity, organisation, or user already exists.

## Proposed durable model

Table: `public.signup_requests`

| Column | Purpose |
| --- | --- |
| `signup_request_id` | Stable UUID and deterministic Supabase identity ID |
| `idempotency_key_hash` | SHA-256 digest; never retain the caller's raw key |
| `normalized_email` | Validated lowercase identity used for safe retries |
| `first_name`, `last_name` | Founding user input |
| `organisation_name` | New organisation input |
| `status` | Last durable workflow milestone |
| `supabase_user_id` | Provider identity after it is proven |
| `organisation_id`, `user_id` | Created public control-plane identities |
| `attempt_count` | Reconciliation attempts |
| `last_error_code`, `last_error_at` | Bounded operational failure state |
| `completed_at` | Activation time |
| `created_at`, `updated_at` | Audit timestamps |

Passwords must never enter this table, a model representation, a log record, an
exception message, telemetry, or an API response.

Unique constraints are required on:

- `idempotency_key_hash`
- `normalized_email`
- `supabase_user_id`
- `organisation_id`
- `user_id`

The migration revision must be generated from the master migration head that
exists when this feature is resumed. Do not reuse the deferred revision ID
`f1a2b3c4d5ee` if another master migration has taken that position.

## State machine

The proposed states are:

1. `received`
2. `auth_created`
3. `control_plane_created`
4. `tenant_defaults_created`
5. `active`
6. `reconciliation_required`
7. `failed`

`received` must commit before calling Supabase. Every later milestone must
commit before moving to the next external boundary. A retry loads and locks the
same request, verifies that its key and normalized input describe the same
intent, and resumes from the last proven milestone.

The Supabase identity should use `signup_request_id` as its deterministic UUID.
If the provider times out after committing, reconciliation performs one bounded
lookup by that UUID rather than listing users.

The organisation and founding user must be created in the same master
transaction. Tenant defaults must be created through the normal organisation
session so forced RLS and transaction-local organisation context remain in
force. Signup must never perform schema DDL, role creation, migration, or secret
creation.

## Abuse and privacy controls

Before making this endpoint public:

- Enforce request-body size before orchestration.
- Rate-limit by verified client IP and normalized-email digest.
- Fail closed when the signup abuse-control backend is unavailable.
- Normalize equivalent international email domains before hashing.
- Never put raw email addresses in Redis keys.
- Suppress passwords from dataclass/model representations.
- Log stable error codes and object IDs only, never provider error payloads.
- Require the permanent shared-tenant runtime readiness gate to pass.

## Proposed implementation map

Create or modify these files only in the later signup task:

- `migrations/master_migrations/versions/<revision>_add_signup_requests.py`
- `migrations/master_migrations/env.py`
- `src/domain/models/master_tables/signup_request_models.py`
- `src/domain/models/master_tables/__init__.py`
- `src/domain/models/user_models.py`
- `src/service/users/signup_service.py`
- `src/infra/auth/supabase_user_client.py`
- `src/api/dependencies/rate_limit.py`
- `src/infra/config/configs.py`
- `src/api/routers/users/auth_handler.py`

Dedicated verification should cover:

- master migration upgrade and downgrade;
- password-free persistence;
- idempotent replay and conflicting intent;
- concurrent retries;
- Supabase timeout after identity creation;
- PostgreSQL failure after Supabase creation;
- organisation/user transaction failure;
- tenant-default failure and retry;
- activation failure and retry;
- caller authority-field rejection;
- generic duplicate responses;
- IP/email rate limiting and Redis fail-closed behavior;
- no password in logs, exceptions, responses, or database rows.

## Re-entry gate

Resume this feature only after:

1. the shared-row cutover and old-resource deletion are complete;
2. `tenant_data` readiness and forced RLS are proven in the target environment;
3. organisation creation no longer performs per-organisation infrastructure
   work;
4. the public signup product behavior is approved;
5. the master migration head is rechecked;
6. the complete failure/reconciliation test matrix passes.
