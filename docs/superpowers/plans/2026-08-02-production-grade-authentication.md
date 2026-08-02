# Production-Grade Authentication Plan

> **Superseded product direction:** this plan describes the currently implemented invite-only
> authentication slice. It is not the approved target for self-service onboarding. Do not extend
> it until `docs/Technical Documentation/self-service-signup-and-organisation-invitations.md` is
> reviewed and approved.

## Goal

Deliver invite-only password, Google Workspace, and Microsoft Entra authentication with one
deterministic backend policy, stable frontend contracts, durable provider connections, recovery
isolation, revocation, auditability, and a safe rollout.

## Fixed architecture

- There is one frontend Sign in page and no public Sign up endpoint.
- An admin provisions every user with exactly one `authentication_method`: `password`,
  `google_workspace`, or `microsoft_entra`.
- Supabase owns credentials, OAuth, and sessions. CopilotGTM owns user/organisation eligibility
  and the permitted authentication method.
- `external_identity_mappings` is the provider-neutral connection block. It stores provider,
  immutable provider subject, tenant/domain, and app user. Returning users resolve by this
  connection before email.
- Email is used only to link the first verified provider session to a pre-existing invitation.
- Unknown external identities never create a user or organisation.
- Hybrid/multi-method app accounts are not supported. Ambiguous multi-provider Supabase OAuth
  sessions fail closed unless a signed `authentication_method` claim identifies the method.

## Implementation tasks

- [x] Add a non-null authentication method column, enum, constraint, downgrade, and migration test.
- [x] Persist the method in admin provisioning and return it from admin user APIs.
- [x] Keep password creation in Supabase; create provider invitations without passwords.
- [x] Add stable auth error codes and generic invalid-credential/recovery responses.
- [x] Remotely verify Supabase sessions, derive the session method, and enforce one shared access
  policy for user, organisation, and method.
- [x] Enforce the policy on password login, provider completion, refresh, `/me`, and protected APIs.
- [x] Replace recovery-link-based first setup with an authenticated temporary-password session and
  `POST /users/auth/complete-password-setup`.
- [x] Suppress forgot-password delivery for unknown and external-auth accounts; reject their
  recovery tokens before mutation.
- [x] Implement provider-neutral Google and Microsoft completion using the existing external
  identity table; reject consumer domains, unverified email, provider mismatch, tenant mismatch,
  unknown invitations, and identity conflicts.
- [x] Restrict persisted provider avatars to trusted HTTPS hosts.
- [x] Revoke the caller's actual session token on logout; support global refresh-session revocation
  and provider-side suspension/restore.
- [x] Compensate a failed app-user transaction by deleting a just-created Supabase password user.
- [x] Make login metadata and operational audit writes best effort so they cannot cause auth outage.
- [x] Normalize organisation domains at API boundaries.
- [x] Rate-limit login, recovery/setup, provider completion, and refresh endpoints.
- [x] Record identity-link, auth-method-rejection, recovery-suppression, and session-revocation audit
  events.
- [x] Publish a frontend contract for the single page, all endpoints, payloads, states, and codes.
- [x] Separate Google login credential names from Google data-integration credentials; allow an
  explicit non-production compatibility alias while requiring dedicated production secrets.

## Deployment gates

1. Before migration, run a read-only production query to classify existing external identity
   mappings and active users without a Supabase identity. Do not assume all existing users are
   password users if either group is non-empty.
2. Apply combined migration 012 before deploying code that reads either authentication field.
3. Configure and verify both Supabase OAuth providers and redirect allowlists in a non-production
   project. Configure the signed method claim hook before allowing linked provider identities.
   Google login uses `google-login-client-id` and `google-login-client-secret` in production;
   never point production sign-in at the data-integration OAuth client.
4. Run the test and lint gates below and execute the manual matrix in development.
5. Deploy backend before frontend. Roll back code before downgrading the migration.

## Required verification

Automated:

```text
uv run pytest tests/migrations/test_user_authentication_method_migration.py tests/service/users tests/service/admin/test_admin_user_service_identity.py tests/infra/auth tests/api/routers/users -q
uv run ruff check <changed Python paths>
uv run mypy <changed auth modules>
```

Manual non-production matrix:

- Password active, first setup, bad password, forgot/reset, provider-button rejection.
- Google invited first link, returning link, unknown user, wrong domain, consumer account, suspended
  user/org, password attempt, recovery attempt, and changed provider email.
- Microsoft equivalents including missing/wrong tenant.
- Refresh after user suspension and organisation suspension.
- Local logout, global logout, and access-token expiry behavior.
- Duplicate/concurrent first provider completion produces one mapping and no cross-user link.

## Production evidence still required

Code completion is not deployment approval. Record the migration precondition result, development
Supabase smoke evidence, pipeline/test IDs, migration revision, and verified post-deploy state before
calling the production rollout complete.
