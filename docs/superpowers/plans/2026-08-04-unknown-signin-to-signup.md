# Unknown External Sign-In to Signup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a first-time Google Workspace or Microsoft Entra user a neutral, one-click path from Sign in to the existing explicit signup flow without mentioning invitations or creating membership during Sign in.

**Architecture:** External completion remains authentication-only. When the verified provider identity has no application user, the backend returns provider-neutral `ACCOUNT_NOT_FOUND`; the frontend offers an explicit **Continue to sign up** action. Only that action calls signup preflight, which privately decides create, join, invite-only, resume, or sign-in.

**Tech Stack:** FastAPI, Supabase OAuth, Pydantic response envelope, pytest, Next.js frontend.

---

## Product contract

| Situation | Result |
| --- | --- |
| Existing active user chooses Sign in | Complete sign-in normally. |
| Existing invited user chooses Sign in | Link the verified provider identity and complete sign-in. |
| Unknown user chooses Sign in | Return `404 ACCOUNT_NOT_FOUND`; create no draft, user, mapping, membership, or organisation. |
| User clicks **Continue to sign up** | Enter the existing provider signup preflight explicitly. |
| Signup preflight finds no organisation | Return `create_organisation`. |
| Signup preflight finds an open same-domain organisation | Return `join_existing_organisation`. |
| Signup preflight finds an invite-only organisation | Return `invitation_required`; disclose no organisation identity. |
| Signup preflight finds the user now exists | Return `sign_in`. |

The unknown Sign-in response must not say whether an organisation or invitation exists. Sign in
must never perform domain admission automatically.

## Files

- Modify: `src/api/routers/users/auth_handler.py`
- Modify: `tests/api/routers/users/test_auth_handler.py`
- Modify: `docs/API Documentation/users-auth-api.md`
- Modify in `copilot-app-frontend`: `src/lib/sso-auth.ts`
- Modify in `copilot-app-frontend`: `src/app/(auth)/auth/callback/page.tsx`
- Test in `copilot-app-frontend`: `__tests__/unit/lib/sso-auth.test.ts`
- Test in `copilot-app-frontend`: `__tests__/unit/app/auth-callback-page.test.tsx`

### Task 1: Return a neutral backend outcome

- [ ] **Step 1: Add failing Google and Microsoft tests**

Add a parameterized test in `tests/api/routers/users/test_auth_handler.py` where provider verification
succeeds but neither an identity mapping nor an email-matched application user exists. Assert:

```python
assert response.status_code == 404
assert result.code == "ACCOUNT_NOT_FOUND"
assert result.message == "We couldn't find a CopilotGTM account for this email."
```

Also assert the fake session has no added objects and no commit, proving Sign in created nothing.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
uv run pytest -q tests/api/routers/users/test_auth_handler.py -k "unknown_external_user"
```

Expected: both provider cases fail because the current code returns provider-specific
`GOOGLE_INVITE_REQUIRED` or `MICROSOFT_INVITE_REQUIRED`.

- [ ] **Step 3: Replace only the unknown-user branch**

In `user_external_auth_complete`, replace the provider-specific invitation response with:

```python
if not user:
    return Response.error_response(
        status_code=404,
        message="We couldn't find a CopilotGTM account for this email.",
        code="ACCOUNT_NOT_FOUND",
        response=response,
    )
```

Do not change invited-user identity linking, existing-user sign-in, authentication-method conflicts,
organisation access checks, or identity-conflict handling.

- [ ] **Step 4: Run backend regression checks**

```powershell
uv run pytest -q tests/api/routers/users/test_auth_handler.py tests/api/routers/users/test_signup_handler.py
uv run ruff check src/api/routers/users/auth_handler.py tests/api/routers/users/test_auth_handler.py
uv run ruff format --check src/api/routers/users/auth_handler.py tests/api/routers/users/test_auth_handler.py
```

Expected: all tests and static checks pass.

### Task 2: Implement the frontend transition

- [ ] **Step 1: Preserve the verified session only for this transition**

In `src/lib/sso-auth.ts`, parse the backend response before signing out. Preserve the Supabase
session only for `ACCOUNT_NOT_FOUND`, because the explicit signup action can reuse it safely:

```typescript
if (!response.ok) {
  if (result.code !== "ACCOUNT_NOT_FOUND") {
    await supabase.auth.signOut();
  }
  throw new AuthError(
    result.message || "SSO login failed",
    result.code,
    response.status,
  );
}
```

All other completion failures keep the current sign-out behavior.

- [ ] **Step 2: Show the neutral first-time-user screen**

In `src/app/(auth)/auth/callback/page.tsx`, retain the existing branch on
`errorCode === "ACCOUNT_NOT_FOUND"`; never branch on message text. Change its link to preserve the
verified provider and use the clearer action label:

```tsx
<Link href={`/signup?provider=${provider}`}>Continue to sign up</Link>
```

The backend message supplies “We couldn't find a CopilotGTM account for this email.” The existing
signup screen already provides **Use a different Google/Microsoft account** and clears the retained
session through `clearSignupSession`. Do not display organisation name, organisation existence,
join policy, or invitation language on the Sign-in error screen.

- [ ] **Step 3: Preserve explicit intent**

`Continue to sign up` navigates into the normal signup route, whose existing `SignupCard` calls
`PUT /users/auth/{provider}/signup/draft` using the retained verified session. The Sign-in callback
must not call signup preflight automatically.

- [ ] **Step 4: Follow the server-owned signup flow**

Handle the returned `flow` exactly as already documented:

```text
create_organisation          -> show workspace creation
join_existing_organisation   -> submit the join request
invitation_required          -> show request/admin-approval state
sign_in                      -> complete normal sign-in
resume_signup                -> resume status polling
organisation_unavailable     -> show unavailable/support state
```

### Task 3: Update and verify the public contract

- [ ] Replace the two provider-specific invite-required rows in
  `docs/API Documentation/users-auth-api.md` with:

```text
404 | ACCOUNT_NOT_FOUND | Offer “Continue to sign up”; do not imply whether an organisation or invitation exists.
```

- [ ] Add acceptance tests for Google and Microsoft covering:

```text
unknown Sign in -> neutral ACCOUNT_NOT_FOUND -> explicit signup preflight
existing invited Sign in -> identity links and login succeeds
existing active Sign in -> login succeeds
unknown Sign in -> no database writes
```

- [ ] In `__tests__/unit/lib/sso-auth.test.ts`, assert `ACCOUNT_NOT_FOUND` does not call
  `mockSignOut`, while another backend rejection still calls it once.

- [ ] In `__tests__/unit/app/auth-callback-page.test.tsx`, reject `completeSsoLogin` with
  `new AuthError(message, "ACCOUNT_NOT_FOUND", 404)` and assert the page renders
  **Continue to sign up** with `/signup?provider=google`; repeat for `azure`.

- [ ] Run frontend checks:

```powershell
yarn test:jest __tests__/unit/lib/sso-auth.test.ts __tests__/unit/app/auth-callback-page.test.tsx
yarn check-types
```

Expected: both targeted suites and TypeScript checks pass.

- [ ] Run `git diff --check` and confirm the diff contains no signup auto-join behavior inside the
  Sign-in completion endpoint.

## Security invariants

- A verified OAuth identity proves identity, not application membership.
- Sign in never creates a user, membership, mapping, signup draft, or organisation.
- Signup preflight is the only component that evaluates same-domain admission policy.
- Unknown Sign in does not reveal whether the company already uses CopilotGTM.
- Provider-specific invitation codes are removed only from the unknown-user branch; invited-user
  completion remains supported.
