# Admin Auth API

## Base URL

```text
/admin/auth
```

## Response Envelope

All endpoints return the standard envelope:

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human readable message",
  "data": { "..." } | null
}
```

## Authentication & Headers

- `Content-Type: application/json`
- `Authorization: Bearer <admin_access_token>` is required for `logout` and `change-password`. Login, reset, forgot-password, and refresh-token are public.

## Rate Limits

- Login: 3/minute and 20/hour per admin email + client IP
- Forgot password: 3 requests per 15 minutes, 10/hour per IP/email

---

## 1) Admin Login

### Endpoint — Admin Login

```http
POST /login
```

#### Body — Admin Login

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `email` | string (email) | Yes | Admin email |
| `password` | string | Yes | Admin password |

#### Success (active admin) — Admin Login

```json
{
  "status": "SUCCESS",
  "message": "Login successful",
  "data": {
    "email": "admin@acme.com",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "role": "super_admin",
    "state": "ACTIVE",
    "created_at": "2025-10-01T12:00:00Z",
    "email_verified": true,
    "last_login_at": "2025-12-02T10:10:00Z",
    "tokens": {
      "access_token": "jwt...",
      "refresh_token": "jwt..."
    }
  }
}
```

#### Success (pending setup / first login) — Admin Login

If Supabase metadata requires a password change, login succeeds with state `PENDING_SETUP` and a reset link:

```json
{
  "status": "SUCCESS",
  "message": "Login successful",
  "data": {
    "state": "PENDING_SETUP",
    "password_reset_link": "https://..."
  }
}
```

#### Error examples — Admin Login

- `401 Unauthorized` for invalid credentials
- `403 Forbidden` if the account is not approved or not active
- `429 Too Many Requests` when rate limits are exceeded

---

## 2) Forgot Password (send reset email)

### Endpoint — Forgot Password

```http
POST /forgot-password
```

#### Body — Forgot Password

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `email` | string (email) | Yes | Admin email to receive the reset link |

#### Response — Forgot Password

Always returns a generic success message to avoid user enumeration:

```json
{
  "status": "SUCCESS",
  "message": "If an account with this email exists, a password reset link has been sent.",
  "data": null
}
```

Rate limited (3 per 15 minutes, 10 per hour).

---

## 3) Reset Password with Recovery Token

Use the Supabase recovery token (from email) to set a new password.

### Endpoint — Reset Password with Recovery Token

```http
POST /reset-password
```

#### Body — Reset Password

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `token_hash` | string | Yes | Supabase recovery token hash |
| `new_password` | string | Yes | New password to set |
| `confirm_password` | string | Yes | Must match `new_password` |
| `action` | "forgot" or "force_change" | No (default: "forgot") | Use `force_change` for first-time setup to clear the pending setup flag |

#### Success — Reset Password

```json
{
  "status": "SUCCESS",
  "message": "Password changed successfully. You can now log in.",
  "data": null
}
```

#### Error examples — Reset Password

- `400 Bad Request` if token/password is missing or passwords do not match
- `400 Bad Request` for invalid or expired token

---

## 4) Refresh Access Token

### Endpoint — Refresh Access Token

```http
POST /refresh-token
```

#### Body — Refresh Access Token

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `refresh_token` | string | Yes | Refresh token issued at login |

#### Success — Refresh Access Token

```json
{
  "status": "SUCCESS",
  "message": "Token refreshed successfully",
  "data": {
    "access_token": "new-access-jwt",
    "refresh_token": "new-refresh-jwt"
  }
}
```

#### Error examples — Refresh Access Token

- `401 Unauthorized` for invalid/expired refresh token
- `400/404/409/500` depending on Supabase error (see message)

---

## 5) Logout

Logs out of the current session by default; supports global logout for a specific admin.

### Endpoint — Logout

```http
POST /logout
```

#### Headers — Logout

`Authorization: Bearer <admin_access_token>`

#### Body — Logout

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `global_logout` | boolean | No (default: `false`) | When `true`, signs the target admin out of all sessions |
| `email` | string (email) | Required if `global_logout` is `true` | Email of the admin to sign out globally |

#### Responses — Logout

- Current session logout: `"Successfully logged out."`
- Global logout: `"Successfully logged out user <email> from all sessions."`

#### Error examples — Logout

- `400 Bad Request` if `global_logout=true` without `email`
- `404 Not Found` if the target admin email does not exist
- `401 Unauthorized` if the access token is invalid/revoked

---

## 6) Change Password (logged-in admin)

Requires the current password to verify identity, then revokes existing sessions.

### Endpoint — Change Password

```http
POST /change-password
```

#### Headers — Change Password

`Authorization: Bearer <admin_access_token>`

#### Body — Change Password

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `current_password` | string | Yes | Existing password |
| `new_password` | string | Yes | New password to set |

#### Success — Change Password

```json
{
  "status": "SUCCESS",
  "message": "Password changed successfully. Please sign in again.",
  "data": null
}
```

#### Error examples — Change Password

- `401 Unauthorized` if the current password is wrong
- `500 Internal Server Error` for unexpected failures
