# Unknown External Sign-In Frontend Contract

## Purpose

When a first-time Google Workspace or Microsoft Entra user chooses **Sign in**, authentication may
succeed at the provider even though no CopilotGTM user exists. Sign in remains authentication-only:
it must not create a signup draft, user, membership, identity mapping, or organisation.

This behavior applies equally to Google and Microsoft and changes only the unknown-user result.
Existing active-user sign-in, invited-user identity linking, authentication-method guidance,
disabled-account checks, and identity-conflict handling remain unchanged.

## Backend response

The frontend calls one of:

```http
POST /users/auth/google/complete
POST /users/auth/microsoft/complete
Authorization: Bearer <verified-supabase-access-token>
```

If the verified identity has neither an external identity mapping nor an email-matched CopilotGTM
user, the backend returns:

```http
HTTP/1.1 404 Not Found
```

```json
{
  "status": "ERROR",
  "message": "We couldn't find a CopilotGTM account for this email.",
  "code": "ACCOUNT_NOT_FOUND",
  "data": null
}
```

Frontend logic must branch on `code === "ACCOUNT_NOT_FOUND"`, never on the message text.

## Required frontend behavior

1. Preserve the verified Supabase session for `ACCOUNT_NOT_FOUND` only. Existing behavior that
   signs out on every other completion error remains unchanged.
2. Show the neutral message returned by the backend.
3. Show **Continue to sign up** as the primary action.
4. Do not mention an organisation, domain match, membership policy, admin, or invitation.
5. Do not call signup automatically from the Sign-in callback.
6. When the user explicitly clicks **Continue to sign up**, navigate to the existing signup route
   with the provider preserved:

```text
Google:    /signup?provider=google
Microsoft: /signup?provider=azure
```

The signup page then uses the retained provider session to call:

```http
PUT /users/auth/google/signup/draft
PUT /users/auth/microsoft/signup/draft
```

The signup preflight—not Sign in—privately determines the next action:

| `flow` | Frontend action |
| --- | --- |
| `create_organisation` | Show workspace creation. |
| `join_existing_organisation` | Submit the existing-organisation join request. |
| `invitation_required` | Show the request/admin-approval state without organisation identity. |
| `sign_in` | Complete normal sign-in; do not repeat signup. |
| `resume_signup` | Resume durable signup status. |
| `organisation_unavailable` | Show the unavailable/support state. |

The existing signup page's **Use a different Google/Microsoft account** action must clear the
retained Supabase session before returning to provider selection.

## Existing outcomes that do not change

| Backend code | Frontend behavior |
| --- | --- |
| `PASSWORD_SIGN_IN_REQUIRED` | Clear provider session and show password sign-in. |
| `GOOGLE_SIGN_IN_REQUIRED` | Clear the mismatched session and start Google sign-in. |
| `MICROSOFT_SIGN_IN_REQUIRED` | Clear the mismatched session and start Microsoft sign-in. |
| `IDENTITY_CONFLICT` | Stop and show support guidance. |
| `ACCOUNT_INACTIVE` | Show account-disabled guidance. |
| `ORGANISATION_INACTIVE` | Show organisation-disabled guidance. |
| `AUTHENTICATION_FAILED` | Clear the session and restart authentication. |

## Acceptance cases

- Unknown Google Sign in returns `404 ACCOUNT_NOT_FOUND` and performs no application database
  writes.
- Unknown Microsoft Sign in returns the same provider-neutral result and performs no writes.
- The response does not reveal whether the email domain belongs to an existing organisation.
- **Continue to sign up** is an explicit user action and preserves the selected provider.
- Existing invited Google and Microsoft users still link their identity and sign in.
- Existing active Google and Microsoft users still sign in normally.
- Every non-`ACCOUNT_NOT_FOUND` completion failure keeps the existing session-clearing behavior.
