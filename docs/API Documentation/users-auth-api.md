# User Authentication API

> **Current implementation:** verified Google Workspace and Microsoft Entra users can create a
> workspace or join an existing same-domain workspace as members. Organisation admins can switch
> same-domain admission to invite-only and can invite members in either mode.

Frontend contract for password, Google Workspace, and Microsoft Entra sign-in.

## Product flow

Use separate **Sign in** and **Sign up** routes. Sign in has email/password, Continue with Google,
and Continue with Microsoft. It authenticates existing users only and never creates an
organisation. Sign up supports verified Google Workspace and Microsoft Entra work accounts and
creates the founding `admin`. Later verified same-domain users join as `member` unless the
organisation has selected invite-only admission. Each user has one immutable
`authentication_method`:

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

The frontend sequence is `/signup` -> provider OAuth -> `PUT` preflight -> follow the returned
server-owned `flow`. Preserve `intent=signup` in signed OAuth state; a successful `/signin`
callback must never be repurposed as signup.

After OAuth, call `PUT /google/signup/draft` or `PUT /microsoft/signup/draft` with the remotely verified
Supabase access token. The backend creates or resumes a server-side draft and returns the
provider-owned identity used by the confirmation screen:

```http
Authorization: Bearer <supabase-access-token>
```

```json
{
  "status": "SUCCESS",
  "message": "Signup draft ready",
  "data": {
    "flow": "create_organisation",
    "signup_request_id": "uuid",
    "status": "draft",
    "provider": "google",
    "email": "jane@acme.com",
    "primary_domain": "acme.com",
    "full_name": "Jane Doe",
    "profile_image_url": "https://lh3.googleusercontent.com/avatar.png",
    "organisation_name": null
  }
}
```

`flow` is one of:

| Flow | Frontend action |
| --- | --- |
| `create_organisation` | Show organisation name and submit the existing organisation-creation endpoint. |
| `join_existing_organisation` | Call the join endpoint below; do not show organisation creation. |
| `invitation_required` | Call the join endpoint to persist the request and notify admins. |
| `sign_in` | Continue normal sign-in; no signup request is created. |
| `resume_signup` | Resume durable status polling for the returned request. |
| `organisation_unavailable` | Stop and show that the workspace is unavailable. |

The response never reveals an existing organisation's ID or name. `signup_request_id`, `status`,
and `organisation_name` are nullable for `sign_in`.

Submit only the editable workspace name to `POST /google/signup` or `POST /microsoft/signup`:

```json
{
  "organisation_name": "Acme"
}
```

Do not send names, company domain, profile fields, roles, policy versions, or an idempotency key.
The backend derives the founder identity and canonical `primary_domain` from the remotely verified
Google Workspace hosted-domain claim or Microsoft Entra identity. It selects the current Terms and
Privacy versions when finalising the draft. Consumer email domains are rejected.

Organisation matching prefers the unique scalar `primary_domain`. Legacy organisations are
considered only when that scalar is missing and their configured domain list contains the verified
domain. One legacy match is repaired automatically; multiple legacy matches fail closed with
`SIGNUP_RECONCILIATION_REQUIRED` so a user is never attached to an arbitrary tenant.

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

Poll `GET /signup-requests/{signup_request_id}` with the same verified provider session. A closed
tab can resume at `draft`; after Create workspace, continue polling through `received`,
`control_plane_created`, `tenant_defaults_created`, and
`storage_created`; enter onboarding only at `active`. `reconciliation_required` is retryable via
support/operations and is not success. The UUID is not authorization: another identity receives
`SIGNUP_REQUEST_NOT_FOUND`. Request ownership includes the provider, provider subject, and email,
so a request created through one provider cannot be polled through another.

Stable signup errors are `AUTHENTICATION_FAILED`, `SIGNUP_PROVIDER_INVALID`,
`SIGNUP_IDENTITY_INVALID`, `CONSUMER_ACCOUNT_NOT_SUPPORTED`, `ACCOUNT_ALREADY_EXISTS`,
`ACCOUNT_IN_OTHER_ORGANISATION`, `ORGANISATION_ACCESS_REQUIRED`, `SIGNUP_DISPATCH_FAILED`,
`SIGNUP_REQUEST_NOT_FOUND`, `SIGNUP_STATUS_UNAVAILABLE`, `SIGNUP_RECONCILIATION_REQUIRED`, and
`SIGNUP_STATE_INVALID`.
Rate-limit storage failure returns `503`; the frontend must fail closed and retry later with the
same verified provider session.

### Join an existing same-domain organisation

For `join_existing_organisation` or `invitation_required`, submit only the server-created request:

```http
POST /users/auth/google/signup/join
Authorization: Bearer <supabase-access-token>
Content-Type: application/json
```

```json
{ "signup_request_id": "uuid" }
```

Use `/microsoft/signup/join` for Microsoft. The backend re-verifies request ownership, domain,
organisation state, and the current policy. An open policy creates one active approved `member`,
its provider mapping, and tenant CRM user. Invite-only returns durable status
`invitation_required`, creates no user or mapping, and notifies active organisation admins once.
Retries return the same durable result. The request accepts no organisation ID, domain, role, or
approval fields.

`invitation_required` is a successful durable flow/status, not an error code and not an
organisation-creation state. A notification delivery failure is logged after commit and does not
undo a successfully created member. Delivery is currently best-effort rather than outbox-backed.
Organisation and user IDs remain null until the request is `active`, so an unadmitted applicant
cannot discover the matched tenant identifier.

### Organisation-admin membership controls

- `GET /users/preference/membership` returns `{ "domain_join_policy": "open|invite_only" }`.
- `PATCH /users/preference/membership` accepts the same field. Missing policy defaults to `open`.
- `POST /users/organisation/invitations` accepts `email`, `first_name`, `last_name`, and
  `authentication_method` (`google_workspace` or `microsoft_entra`). It always creates a member
  invitation and works in both policy modes.

All three endpoints require an authenticated organisation `admin`; members receive `403`.
Invitation email must match one of the organisation's configured domains. Repeating the same
invitation for the same still-unlinked member reuses that member and resends the email. It never
changes an existing user's organisation, role, provider, or linked identity; those conflicts return
`USER_ALREADY_EXISTS`.

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
| 404 | `ACCOUNT_NOT_FOUND` | Verified provider identity has no CopilotGTM account. Offer an explicit **Continue to sign up** action without mentioning organisations or invitations. |
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
