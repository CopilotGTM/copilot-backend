# Users Auth API Documentation

## Base URL

```text
/users/auth
```

---

## Response Envelope

- All endpoints return a consistent envelope:

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

- Timestamps use ISO 8601 with `Z` suffix (UTC), e.g. `2025-10-05T10:30:00Z`.

---

## 1) Login

### Login Endpoint

```http
POST /users/auth/login
```

### Login Description

Authenticates a user via Supabase and returns access/refresh tokens. If the account requires a password change, returns a reset link and a `PENDING_SETUP` state instead of tokens.

### Login Authentication

Not required

### Recommended Headers

- `X-User-Timezone: <IANA timezone>` to capture the user's preferred timezone at login.
  Example values: `Asia/Kolkata`, `America/New_York`.

### Login Rate Limiting

- Default: 10 requests/minute, 50/hour per (IP + email)
- On limit breach: `429 Too Many Requests` with `Retry-After` header

### Request Body

```json
{
  "email": "user@example.com",
  "password": "your-password"
}
```

### Success Responses (200)

### Active user

```json
{
  "status": "SUCCESS",
  "message": "Login successful",
  "data": {
    "email": "user@example.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "role": "ADMIN | MEMBER",
    "state": "ACTIVE",
    "created_at": "2025-09-01T08:00:00Z",
    "email_verified": true,
    "last_login_at": "2025-10-06T08:12:00Z",
    "timezone": "Asia/Kolkata",
    "user_preferences": {
      "crm": {
        "contacts": {
          "list": {
            "columns": [
              "full_name",
              "job_title",
              "company.name",
              "email",
              "phone",
              "linkedin_url",
              "owner.name",
              "deal_count",
              "last_contacted_at",
              "last_activity_at"
            ]
          }
        },
        "accounts": {
          "list": {
            "columns": [
              "name",
              "domain",
              "industry",
              "lifecycle_stage",
              "owner.name",
              "contact_count",
              "deal_count",
              "pipeline_value",
              "number_of_employees",
              "annual_revenue",
              "country",
              "linkedin_url"
            ]
          }
        },
        "deals": {
          "list": {
            "columns": [
              "name",
              "company.name",
              "stage.name",
              "amount",
              "expected_close_date",
              "owner.name",
              "primary_contact.name",
              "last_activity_at",
              "deal_status",
              "pipeline.name"
            ]
          }
        },
        "tasks": {
          "list": {
            "columns": [
              "subject",
              "due_date",
              "assigned_to.name",
              "deal.name",
              "priority",
              "status",
              "has_reminder"
            ]
          }
        }
      }
    },
    "tokens": {
      "access_token": "<JWT>",
      "refresh_token": "<REFRESH_TOKEN>"
    }
  }
}
```

### Password change required

```json
{
  "status": "SUCCESS",
  "message": "Login successful, password change required",
  "data": {
    "state": "PASSWORD_CHANGE_REQUIRED",
    "user_preferences": {
      "crm": {
        "contacts": {
          "list": {
            "columns": [
              "full_name",
              "job_title",
              "company.name",
              "email",
              "phone",
              "linkedin_url",
              "owner.name",
              "deal_count",
              "last_contacted_at",
              "last_activity_at"
            ]
          }
        },
        "accounts": {
          "list": {
            "columns": [
              "name",
              "domain",
              "industry",
              "lifecycle_stage",
              "owner.name",
              "contact_count",
              "deal_count",
              "pipeline_value",
              "number_of_employees",
              "annual_revenue",
              "country",
              "linkedin_url"
            ]
          }
        },
        "deals": {
          "list": {
            "columns": [
              "name",
              "company.name",
              "stage.name",
              "amount",
              "expected_close_date",
              "owner.name",
              "primary_contact.name",
              "last_activity_at",
              "deal_status",
              "pipeline.name"
            ]
          }
        },
        "tasks": {
          "list": {
            "columns": [
              "subject",
              "due_date",
              "assigned_to.name",
              "deal.name",
              "priority",
              "status",
              "has_reminder"
            ]
          }
        }
      }
    },
    "password_reset_link": "https://.../reset?token=..."
  }
}
```

`user_preferences` is returned during login/session bootstrap so listing endpoints can stay focused on data rows. If a user has saved contact-list, account-list, deal-list, or task-list column preferences, those override the backend defaults; otherwise the ordered defaults above are returned.

### Error Responses

- 401 Unauthorized
  - `USER_NOT_FOUND`
  - `INVALID_CREDENTIALS`
- 429 Too Many Requests
  - `Too many login attempts. Please try again later.`
- 500 Internal Server Error
  - `login failed`

### Example cURL

```bash
curl -X POST "$BASE_URL/users/auth/login" \
  -H "X-User-Timezone: Asia/Kolkata" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"your-password"}'
```

---

## 2) Reset Password

### Reset Password Endpoint

```http
POST /users/auth/reset-password
```

### Reset Password Description

Completes a password reset using a token. Supports two flows via `action`:

- `force_change`: First-time password change after initial setup; also updates Supabase user metadata and verifies email.
- `forgot`: Regular password reset; password is updated, metadata unchanged.

### Reset Password Authentication

Not required

### Reset Password Request Body

```json
{
  "token_hash": "<HASH_FROM_EMAIL_LINK>",
  "new_password": "NewStrongP@ssw0rd",
  "confirm_password": "NewStrongP@ssw0rd",
  "action": "force_change | forgot"
}
```

### Reset Password Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Password changed successfully. You can now log in with your new password.",
  "data": null
}
```

### Reset Password Error Responses

- 400 Bad Request
  - `Recovery token_hash is required.`
  - `New password is required.`
  - `New password and confirm password do not match`
  - `Invalid action. Must be 'force_change' or 'forgot'.`
  - `Invalid or expired recovery token`
- 401 Unauthorized
  - `Unauthorized` (for `force_change` when local user not found)
- 500 Internal Server Error
  - `Failed to update user metadata`
  - `Error updating user data after password reset.`
  - `First-time password change failed`

### Reset Password Example cURL

```bash
curl -X POST "$BASE_URL/users/auth/reset-password" \
  -H "Content-Type: application/json" \
  -d '{
    "token_hash":"<HASH>",
    "new_password":"NewStrongP@ssw0rd",
    "confirm_password":"NewStrongP@ssw0rd",
    "action":"force_change"
  }'
```

---

## 3) Refresh Token

### Refresh Token Endpoint

```http
POST /users/auth/refresh-token
```

### Refresh Token Description

Exchanges a valid refresh token for a new access token (and possibly a new refresh token).

### Refresh Token Authentication

Not required (refresh token is in the request body)

### Refresh Token Request Body

```json
{ "refresh_token": "<REFRESH_TOKEN>" }
```

### Refresh Token Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Token refreshed successfully",
  "data": {
    "access_token": "<NEW_JWT>",
    "refresh_token": "<NEW_REFRESH_TOKEN>"
  }
}
```

### Refresh Token Error Responses

- 400 Bad Request | 401 Unauthorized | 404 Not Found | 409 Conflict
  - Returned when Supabase reports specific auth errors (message varies)
- 401 Unauthorized
  - `Invalid or expired refresh token. User has been logged out. Please log in again.`
- 500 Internal Server Error
  - `Token refresh failed. Please log in again.`

### Refresh Token Example cURL

```bash
curl -X POST "$BASE_URL/users/auth/refresh-token" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<REFRESH_TOKEN>"}'
```

---

## 4) Forgot Password

### Forgot Password Endpoint

```http
POST /users/auth/forgot-password
```

### Forgot Password Description

Initiates a password reset by generating a password reset link and sending it via email. The response is deliberately generic to prevent account enumeration.

### Forgot Password Authentication

Not required

### Forgot Password Request Body

```json
{ "email": "user@example.com" }
```

### Forgot Password Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "If an account with this email exists, a password reset link has been sent.",
  "data": null
}
```

### Forgot Password Error Responses

- 500 Internal Server Error
  - `Failed to generate password reset link`
  - `Failed to send password reset email. Please check email service configuration.`
  - `Forgot password request failed`

### Forgot Password Example cURL

```bash
curl -X POST "$BASE_URL/users/auth/forgot-password" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com"}'
```

---

## 5) Logout

### Logout Endpoint

```http
POST /users/auth/logout
```

### Logout Description

Logs out the current authenticated user. If `global_logout` is true, logs out from all active sessions for this user.

### Logout Authentication

Required (Bearer access token)

### Logout Request Body

```json
{ "global_logout": false }
```

### Logout Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Successfully logged out.",
  "data": null
}
```

When `global_logout` is true, message becomes: "Successfully logged out from all sessions."

### Logout Error Responses

- 401 Unauthorized
  - `Missing or invalid authorization header`
  - `Invalid token: missing user ID`
  - `User account is not active or not approved`
- 500 Internal Server Error
  - `An unexpected error occurred during logout.`

### Logout Example cURL

```bash
curl -X POST "$BASE_URL/users/auth/logout" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"global_logout":false}'
```

---

## Security Notes

- Use the returned `access_token` as a Bearer token for protected endpoints: `Authorization: Bearer <access_token>`.
- Keep the `refresh_token` secure; it can mint new access tokens.
- Login attempts are rate-limited. Defaults can be tuned via env vars:
  - `RATE_LIMIT_AUTH_LOGIN_PER_MINUTE` (default: 10)
  - `RATE_LIMIT_AUTH_LOGIN_PER_HOUR` (default: 50)

## Field Reference

### User Login Response Fields

- `state`: `ACTIVE` or `PASSWORD_CHANGE_REQUIRED`
- `timezone`: Preferred IANA timezone persisted from `X-User-Timezone` when provided
- `tokens.access_token`: JWT used in `Authorization: Bearer` header
- `tokens.refresh_token`: Token to exchange at `/refresh-token`
- `password_reset_link`: Present only when `state` is `PASSWORD_CHANGE_REQUIRED`

## Common Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Bad request/validation error |
| 401 | Unauthorized/invalid credentials or token |
| 404 | Not found (from underlying auth service) |
| 409 | Conflict (from underlying auth service) |
| 429 | Too many requests (rate limit) |
| 500 | Internal server error |
