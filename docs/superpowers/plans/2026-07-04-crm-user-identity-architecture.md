# CRM User Identity Architecture

## Context

Copilot has two user concepts:

- `public.users`: Copilot application users who can authenticate, log in, receive app roles, and own product preferences.
- `crm_users`: CRM owner and assignee identities used by CRM records such as accounts, contacts, deals, leads, activities, and tasks.

The July 2026 prod incident exposed a broken invariant. A Copilot app user created a contact. Contact creation derived an account from the contact email domain and tried to insert that account with `owner_id` set to the current app user's UUID. The org schema had no matching `crm_users` row, so Postgres rejected the insert with `fk_crm_company_owner_id` and the whole contact transaction rolled back.

The problem is not that two tables exist. The problem is that we allowed an app user to become a CRM actor before the org had the matching CRM identity row.

## Decision

Keep `public.users` and `crm_users` separate.

Use `public.users` as the source of truth for login, Supabase linkage, authorization, admin approval, app role, session identity, and app preferences.

Use `crm_users` as the source of truth for CRM ownership, assignment, authorship, and audit foreign keys.

Do not create HubSpot or Salesforce users in `public.users` unless they are actually invited or onboarded as Copilot application users. Provider CRM users can exist without Supabase auth, without a Copilot login, and without app authorization state.

## Core Invariant

For any human who is both a Copilot app user and a CRM user in the same organisation:

```text
public.users.user_id == crm_users.user_id
```

That shared UUID is the application-local identity. HubSpot and Salesforce IDs remain provider identifiers and belong in the existing source mapping table, not in `public.users.user_id` or `crm_users.user_id`.

`crm_users.user_id` is the table primary key. That means there can be only one CRM user row for a local UUID. Do not try to create one provider row and one internal CRM projection row with the same UUID; that will violate the primary key and recreate this class of bug in a different path.

`crm_users.integration_id` is the source that owns or first created the row. It is not a safe many-to-many membership model for all systems where the same person appears. Provider external identities belong in `crm_source_object_mappings`, where multiple provider identities can point to one local `crm_users.user_id`.

Provider sync already follows this direction:

- If a HubSpot owner or Salesforce user email matches an existing `public.users` row in the same org, the provider `crm_users.user_id` reuses `public.users.user_id`.
- If that UUID already exists in `crm_users` but the provider source mapping is missing, provider sync reuses and updates the existing local row, then writes the provider mapping. It must not insert another `crm_users` row with the same UUID.
- If the provider user has no app-user match, sync creates a provider-only `crm_users` row with a new UUID.
- The provider external ID maps to that local UUID through source mappings.

The missing inverse must be true too:

- When a Copilot app user is created, first check whether the org already has an active, non-deleted `crm_users` row with the same normalized email.
- If exactly one CRM identity exists, create `public.users` with that existing `crm_users.user_id`.
- If no CRM identity exists, create `public.users` with a new UUID and immediately create a `crm_users` row under the internal CRM integration with the same UUID.

This makes onboarding the primary place where the invariant is created. Manual CRM write paths still call `ensure`, but that becomes defense-in-depth for old data, races, scripts, and missed paths.

## Why Not Merge The Tables

Merging CRM identities into `public.users` would turn an authentication table into a mixed identity warehouse.

Problems with merging:

- CRM providers can expose hundreds of users while only a small subset should be able to log into Copilot.
- Provider-only CRM users do not have Supabase auth state.
- `public.users.email` is globally unique today, while CRM users are provider and organisation scoped.
- Provider owners can be inactive, deleted, duplicated across providers, missing email, or represented differently by each CRM.
- Login authorization becomes ambiguous if non-login CRM users live beside real app users.
- Auth fields become nullable or fake for provider users, increasing security risk and code branching.

The durable boundary is:

- `public.users` answers "who can use Copilot?"
- `crm_users` answers "who can own or be assigned CRM objects?"

## App User Creation Flow

The app-user creation service is the first-class identity boundary.

Required flow:

1. Normalize the requested email by trimming and lowercasing.
2. Verify the organisation exists.
3. Reject if `public.users` already has that email.
4. Query the org's `crm_users` for active, non-deleted rows with the same normalized email.
5. If exactly one distinct `crm_users.user_id` is found, use that UUID as the new `public.users.user_id`.
6. If no CRM user is found, let `public.users.user_id` be newly generated.
7. Create the Supabase auth user.
8. Insert `public.users`.
9. Ensure the org has the internal CRM integration and default CRM setup.
10. If no CRM row was reused, create a `crm_users` row for the new app user under the `internal_crm` integration, using the same UUID.
11. If a provider CRM row was reused, do not create a duplicate internal CRM row; the reused row is already the canonical CRM user identity.
12. Commit the app user and org CRM identity work with explicit compensation for Supabase/auth failure cases.

Conflict rules:

- If multiple active, non-deleted CRM identities share the same email but use different UUIDs, do not guess. Return a clear conflict and require an admin/support resolution.
- If multiple provider rows with the same email already point to the same local UUID, reuse that UUID.
- Deleted provider users should not be reused automatically.
- Inactive provider users should not be reused unless product explicitly decides that onboarding should reactivate that CRM identity.
- If a CRM UUID already has a `public.users` row, reject as an identity conflict instead of creating a second login user.

This matches the current provider-sync design and fixes the missing inverse path.

## Canonical CRM User Row

Every Copilot app user must have one canonical `crm_users` row in the org schema.

If the user did not exist in CRM before app-user creation, create that row under the org's `internal_crm` integration.

If the user already existed as a HubSpot or Salesforce CRM user, reuse that provider-created row by UUID. Do not copy it into another `crm_users` row under `internal_crm`.

The canonical row must:

- use `crm_users.user_id = public.users.user_id`,
- keep provider source mappings intact when it was created by provider sync,
- copy or refresh safe display fields from `public.users` when it represents an onboarded Copilot user,
- remain active/non-deleted while the app user is active,
- be idempotent when resolved again.

This row exists so manual Copilot-created CRM records can use ordinary CRM FKs without special-casing app users.

Do not add `crm_users.app_user_id` in the first fix. The existing shared-UUID convention is already used by provider writers and internal CRM sync. Adding a second link column would create another invariant to maintain without solving the current incident class.

## Provider CRM Users

Provider CRM users remain provider-scoped CRM identities.

Provider sync should continue to:

- import all relevant HubSpot/Salesforce owners into `crm_users`,
- reuse `public.users.user_id` when the provider email already matches an app user,
- reuse an existing same-UUID `crm_users` row when the app user already has one and the provider mapping does not exist yet,
- mint a local UUID for provider-only users,
- write provider external ID mappings to the source mapping table,
- never create Supabase users or `public.users` rows.

When a provider-only CRM user is later invited into Copilot, app-user creation must reuse that existing local UUID. That preserves existing CRM owner FKs and source mappings.

## Ensure Boundary

Keep and harden the low-level primitive:

```python
InternalCRMUserService.ensure_crm_user(app_user_id: UUID) -> CRMUser
```

Application CRM write paths should not construct `InternalCRMUserService` directly. They should call the shared wrapper:

```python
ensure_app_user_crm_identity(...) -> CRMUser
```

That keeps actor/owner resolution behavior consistent across account, contact, deal, task, lead, lead form, and participant writes while preserving `InternalCRMUserService` as the single low-level implementation.

Required behavior:

1. Load the app user from `public.users` and verify same org.
2. Look for any active, non-deleted `crm_users` row in the org with `user_id = app_user_id`, regardless of `integration_id`.
3. If found, refresh safe display/status fields from `public.users` and return it.
4. If missing, resolve or create the org's internal CRM integration when the caller is allowed to initialize internal CRM.
5. Create the internal CRM `crm_users` row with `crm_users.user_id = public.users.user_id`.
6. Recover from concurrent insert by re-reading the winning row.

The current lazy sync shape must become conflict-aware. Any bulk or single-user internal sync must first check for an existing `crm_users.user_id` across all integrations, not only under `internal_crm`, otherwise app users who were first imported from HubSpot/Salesforce will hit a primary-key collision.

This method should not be the primary mechanism for normal onboarding. It remains necessary because:

- old production data may be missing canonical CRM user rows,
- background scripts may create or repair data,
- manual write paths need a final guard before assigning owner/audit FKs,
- concurrent requests can race around identity setup.

## Write Path Requirements

Any manual Copilot CRM write that sets owner, creator, updater, author, or assignee from the current authenticated app user must resolve the app user to a canonical CRM user row before writing CRM data.

Initial call sites to audit:

- Contact create.
- Account create.
- Deal create and owner assignment.
- Task create and assignee/owner assignment.
- Lead create and audit fields.
- Lead form create/update audit fields.
- Activity/note author fields where they use the current app user.
- Agent CRM tools that perform manual writes as an app user.

The write path should use the returned `crm_user.user_id`, even though it should equal `public.users.user_id` for app users. That makes the CRM-domain intent explicit.

Current loophole to close first: `ContactWriteService` and `AccountWriteService` set `owner_id` from `context.actor_user_id` directly. Contact creation can also create an account through `AccountWriteService`, so both writers need the same actor-to-CRM-user resolution before assigning owner fields.

## Transaction Boundary

App-user creation spans Supabase auth, `public.users`, and org-scoped CRM identity setup.

The practical transaction shape should be:

- create Supabase auth first because it is external,
- create `public.users`,
- ensure internal CRM integration/defaults when an internal CRM row must be created,
- create or reuse the canonical CRM user row,
- commit database work,
- if DB commit fails after Supabase creation, delete the Supabase user when the auth API supports it; otherwise log a repairable orphan auth user with enough metadata to clean up.

Manual CRM writes should include any missing CRM user row creation and the downstream CRM object write in the same org transaction when possible. The master `public.users` read is read-only.

## Rollout Plan

1. Keep the two-table model.
2. Make app-user creation resolve existing provider CRM identity by email before choosing `public.users.user_id`.
3. When no CRM identity is reused, ensure internal CRM integration/default pipeline/default stages and create the internal CRM user row during app-user creation.
4. When a provider CRM identity is reused, keep the single provider-created `crm_users` row and do not insert an internal duplicate.
5. Harden `InternalCRMUserService.ensure_crm_user()` to resolve by `user_id` across all integrations before creating an internal row.
6. Update contact and account write paths to resolve actor CRM identity before assigning `owner_id`.
7. Keep `ensure_app_user_crm_identity()` on other manual write paths as recovery.
8. Audit deal/task/lead/activity write paths for raw app-user UUID owner assignments.
9. Add a read-only prod audit for active app users missing any canonical `crm_users` row.
10. Add a read-only audit for app users whose email matches provider CRM users with a different UUID; do not rewrite automatically.
11. Backfill missing internal CRM rows only for app users who have no CRM row at all.
12. Only after the new flow is deployed, consider a carefully scoped reconciliation plan for historical mismatched provider CRM users.

## Verification

Minimum automated coverage:

- Creating an app user with no matching CRM user creates `public.users` and one internal CRM `crm_users` row with the same UUID.
- Creating an app user whose email matches exactly one provider CRM user creates `public.users.user_id` with that existing provider `crm_users.user_id` and does not insert a duplicate internal CRM row.
- Creating an app user whose email maps to multiple CRM UUIDs fails with a clear conflict.
- Creating an app user with no matching CRM user ensures internal CRM integration and default CRM setup.
- Creating an app user does not create HubSpot/Salesforce auth users or provider users.
- `ensure_app_user_crm_identity()` returns an existing provider-created CRM user row when its `user_id` matches the app user.
- `ensure_app_user_crm_identity()` creates an internal CRM row only when no CRM row exists for the app user.
- Contact create succeeds when the authenticated app user had no CRM user row before the request.
- Account create succeeds under the same missing-row condition.
- Provider sync still writes provider-only users to `crm_users` and source mappings only.
- Provider sync reuses an existing app/internal `crm_users.user_id` when a matched HubSpot/Salesforce user arrives before any provider mapping exists.

Minimum runtime checks:

- Query all active app users and verify one active, non-deleted `crm_users` row exists for each onboarded Copilot user.
- Query provider CRM users and verify provider-only users do not appear in `public.users`.
- Query matching emails where `public.users.user_id != crm_users.user_id` and review manually before any FK rewrite.
- Check API logs for app-user identity reuse, internal CRM row creation, and identity-conflict counts after deployment.

## Operational Repair Pattern

Manual prod repair is acceptable only as an incident mitigation.

The correct repair for a Copilot app user must:

- first check whether any `crm_users` row already exists with the app user's `public.users.user_id`,
- create a new row only when none exists,
- use the app user's `public.users.user_id` for the new row,
- use the org's `internal_crm` integration ID for the new row,
- copy display fields from `public.users`,
- set `is_deleted = false`,
- set active state from app-user status,
- never attach a newly created repair row to HubSpot or Salesforce integration IDs.

After the onboarding and write-path fixes are deployed, this repair should no longer be needed for normal manual CRM writes.

## Code Observations

Current repo behavior already supports most of this direction:

- HubSpot user sync documents the match-or-mint rule and expects later app signup to reuse the existing `crm_users.user_id`.
- Salesforce user sync uses the same matched-app-user or minted-UUID writer behavior.
- `AdminUserService.create_user()` already has a partial inverse lookup that reuses one active, non-deleted `crm_users.user_id` by normalized email.
- `AdminUserService.create_user()` does not yet ensure the canonical CRM user row when no CRM match exists.
- `InternalCRMSetupService` already owns internal CRM integration and default pipeline/stage creation.
- `InternalCRMUserService` already owns app-user CRM identity creation, but it currently looks under the internal CRM integration first; it must become aware that provider-created rows can already be the canonical row for the same app user UUID.
- `ContactWriteService` and `AccountWriteService` currently assign actor IDs directly to CRM owner fields, which is the incident-class path.

The production-quality fix is to connect these existing pieces at app-user creation and keep write-time ensure as a guardrail.
