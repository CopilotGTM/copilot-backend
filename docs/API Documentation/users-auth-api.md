# User Authentication API

> **Current implementation:** sign-in remains invite-only, while new organisations can be created
> through the separate Google Workspace or Microsoft Entra self-service signup endpoints below.
> Organisation invitations remain a later phase.

Frontend contract for password, Google Workspace, and Microsoft Entra sign-in.

## Product flow

Use separate **Sign in** and **Sign up** routes. Sign in has email/password, Continue with Google,
and Continue with Microsoft. It authenticates existing users only and never creates an
organisation. Sign up supports verified Google Workspace and Microsoft Entra work accounts and
creates the founding `admin`; later users still require an administrator-created account or
invitation. Each user has one immutable `authentication_method`:

- `password`
- `google_workspace`
- `microsoft_entra`

The backend enforces that method on login, token refresh, authenticated requests, recovery,
and external-identity completion. A user created with password cannot silently enter through
Google or Microsoft; an external-auth user cannot set or reset a password.

All endpoints below are relative to `/users/auth`. Responses use:

```json
{ "status": "SUCCESS|ERROR", "message": "Human-readable text", "code": "STABLE_CODE", "data": null }
```

`code` is present on errors where frontend branching is required. Display `message`; branch on
`code`, never on message text.

## Self-service organisation signup

The frontend sequence is `/signup` -> provider OAuth -> organisation details -> `POST` -> durable
polling -> onboarding. Preserve `intent=signup` in signed OAuth state; a successful `/signin`
callback must never be repurposed as signup.

Submit the remotely verified Supabase access token as `Authorization: Bearer <token>` and a stable
16-255 character `Idempotency-Key` to `POST /google/signup` or `POST /microsoft/signup`:

```json
{
  "first_name": "Jane",
  "last_name": "Doe",
  "job_title": "VP Sales",
  "organisation_name": "Acme",
  "primary_domain": "acme.com",
  "country": "US",
  "sales_team_size": 20,
  "website_url": "https://acme.com",
  "linkedin_url": "https://www.linkedin.com/company/acme",
  "terms_version": "2026-08-02",
  "privacy_version": "2026-08-02"
}
```

`202 Accepted` returns only durable identifiers and state:

```json
{
  "status": "SUCCESS",
  "message": "Organisation signup accepted",
  "data": {
    "signup_request_id": "uuid",
    "status": "received",
    "organisation_id": null,
    "user_id": null
  }
}
```

Poll `GET /signup-requests/{signup_request_id}` with the same verified provider session. Continue
polling through `received`, `control_plane_created`, `tenant_defaults_created`, and
`storage_created`; enter onboarding only at `active`. `reconciliation_required` is retryable via
support/operations and is not success. The UUID is not authorization: another identity receives
`SIGNUP_REQUEST_NOT_FOUND`.

Stable signup errors are `AUTHENTICATION_FAILED`, `SIGNUP_PROVIDER_INVALID`,
`IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`, `SIGNUP_IDENTITY_INVALID`,
`CONSUMER_ACCOUNT_NOT_SUPPORTED`, `SIGNUP_DOMAIN_MISMATCH`, `ACCOUNT_ALREADY_EXISTS`,
`ACCOUNT_IN_OTHER_ORGANISATION`, `ORGANISATION_ACCESS_REQUIRED`, `SIGNUP_DISPATCH_FAILED`,
`SIGNUP_REQUEST_NOT_FOUND`, `SIGNUP_STATUS_UNAVAILABLE`, `SIGNUP_RECONCILIATION_REQUIRED`, and
`SIGNUP_STATE_INVALID`.
Rate-limit storage failure returns `503`; the frontend must fail closed and retry later with the
same key.

The signup write budget is intentionally strict. Status polling has a separate read budget; poll
with backoff (for example, every 2 seconds initially, then every 5 seconds) and always respect
`429 Retry-After`.

## Password sign-in

`POST /login` (public, rate limited)

```json
{ "email": "jane@acme.com", "password": "secret" }
```

Optionally send `X-User-Timezone: Asia/Kolkata`.

Active accounts return access and refresh tokens:

```json
{
  "status": "SUCCESS",
  "message": "Login successful",
  "data": {
    "email": "jane@acme.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "role": "member",
    "state": "ACTIVE",
    "email_verified": true,
    "last_login_at": "2026-08-02T08:00:00Z",
    "timezone": "Asia/Kolkata",
    "profile_image_url": null,
    "organisation_id": "uuid",
    "organisation_preferences": {},
    "user_preferences": {},
    "tokens": { "access_token": "...", "refresh_token": "..." }
  }
}
```

An invited password user's first successful login returns the same tokens with
`state: "PASSWORD_CHANGE_REQUIRED"`. Route that user only to the permanent-password screen,
then call `POST /complete-password-setup` with the returned access token:

```http
Authorization: Bearer <temporary-session-access-token>
```

```json
{ "new_password": "NewStrongPassword", "confirm_password": "NewStrongPassword" }
```

Success revokes the temporary session. Send the user back to Sign in.

Password-login errors:

| HTTP | `code` | Frontend action |
|---|---|---|
| 401 | `INVALID_CREDENTIALS` | Show generic invalid email/password. Do not reveal whether the account exists. |
| 409 | `GOOGLE_SIGN_IN_REQUIRED` | Show “This account uses Google. Continue with Google.” |
| 409 | `MICROSOFT_SIGN_IN_REQUIRED` | Show “This account uses Microsoft. Continue with Microsoft.” |
| 403 | `ACCOUNT_INACTIVE` | Show account-disabled/support message. |
| 403 | `ORGANISATION_INACTIVE` | Show organisation-disabled/support message. |
| 429 | gateway detail | Respect `Retry-After`. |

## Google and Microsoft completion

The frontend starts Supabase OAuth with the corresponding provider. After Supabase returns a
session, call exactly one backend completion endpoint with its access token:

```http
POST /users/auth/google/complete
Authorization: Bearer <supabase-access-token>
```

```http
POST /users/auth/microsoft/complete
Authorization: Bearer <supabase-access-token>
```

There is no body and the backend does not mint a second token. Keep the existing Supabase
session, then use `GET /me` to bootstrap or rehydrate the app. Completion links the immutable
provider subject and tenant to the invited app user on first use; later logins resolve that
connection before considering email. This prevents email changes from taking over another
account.

External completion errors:

| HTTP | `code` | Meaning/action |
|---|---|---|
| 401 | `AUTHENTICATION_FAILED` | Invalid, ambiguous, unsupported, or provider-mismatched session; clear local session and restart OAuth. |
| 403 | `GOOGLE_INVITE_REQUIRED` | Google user was not invited. Contact admin. |
| 403 | `MICROSOFT_INVITE_REQUIRED` | Microsoft user was not invited. Contact admin. |
| 409 | `PASSWORD_SIGN_IN_REQUIRED` | Account is password-owned; clear OAuth session and show password form. |
| 409 | `GOOGLE_SIGN_IN_REQUIRED` | Use Google. |
| 409 | `MICROSOFT_SIGN_IN_REQUIRED` | Use Microsoft. |
| 409 | `IDENTITY_CONFLICT` | Identity is already linked; stop and contact support. |
| 403 | `ACCOUNT_INACTIVE` / `ORGANISATION_INACTIVE` | Access is disabled. |

Only verified company accounts matching an organisation domain are accepted. Consumer domains
such as Gmail, Outlook.com, and Yahoo are rejected. Google additionally requires the Workspace
hosted-domain claim; Microsoft requires an Entra tenant ID. Provider avatars are accepted only
from trusted HTTPS Google image hosts.

## Forgot and reset password

`POST /forgot-password`:

```json
{ "email": "jane@acme.com" }
```

Unknown emails and Google/Microsoft accounts receive the same `200` response and no email:

```json
{ "status": "SUCCESS", "message": "If an account with this email exists, a password reset link has been sent.", "data": null }
```

This prevents account and authentication-method enumeration. The frontend should always show
that neutral message. A valid password-account email receives the recovery email.

`POST /reset-password`:

```json
{
  "token_hash": "<hash-from-email>",
  "new_password": "NewStrongPassword",
  "confirm_password": "NewStrongPassword"
}
```

Invalid, expired, unknown, inactive, and external-auth recovery attempts all return
`400` with `code: "INVALID_RECOVERY_TOKEN"` and do not mutate the password.

## Refresh, session bootstrap, and logout

`POST /refresh-token` (public, IP rate limited):

```json
{ "refresh_token": "..." }
```

The refreshed session is rechecked against the local user, organisation, and configured auth
method before tokens are returned. Disabled or method-mismatched accounts receive the stable
access-policy codes above.

`GET /me` requires `Authorization: Bearer <access-token>` and returns the active user profile in
the login-response shape. Use it when restoring a browser session.

`POST /logout` requires the access token and accepts:

```json
{ "global_logout": false }
```

`false` revokes the current refresh-token family; `true` revokes all refresh sessions. A JWT
already issued can remain usable until its short expiry, so the frontend must always clear its
local tokens immediately.

## Frontend routing summary

1. Keep Sign in and Sign up distinct; Sign in never creates users or organisations.
2. Password success + `ACTIVE` -> app.
3. Password success + `PASSWORD_CHANGE_REQUIRED` -> permanent-password page only.
4. Google/Microsoft OAuth success -> matching `/complete` endpoint -> app.
5. On `*_SIGN_IN_REQUIRED`, switch to the named method without retry loops.
6. On `*_INVITE_REQUIRED`, show contact-admin; never create a user client-side.
7. Clear local Supabase state after failed OAuth completion or logout.
