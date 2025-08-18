### Users API

Base path: `/admin/users`

All endpoints return the standard wrapper:

```json
{
  "status": "SUCCESS" | "ERROR",
  "message": "string",
  "data": any | null
}
```

Authentication: Requires admin authentication. Role requirements are noted per endpoint.

---

### Create user

- Method: POST
- Path: `/admin/users/create_user`
- Roles: Super admin or Admin

Request body:

```json
{
  "first_name": "string",
  "last_name": "string",
  "email": "user@example.com",
  "organisation_id": "uuid",
  "user_role": "member|admin",
  "job_title": "string",
  "organisation_role": "string"
}
```

Response data:

```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  "created_at": "2024-01-01T00:00:00Z"
}
```

Errors:
- 400 if required fields are missing
- 400 if invalid user role
- 400 if organisation_id is missing

---

### Get user profile

- Method: GET
- Path: `/admin/users/{user_id}/profile`
- Roles: Super admin or Admin

Path params:
- `user_id` (uuid)

Response data:

```json
{
  "user_id": "uuid",
  "first_name": "string",
  "last_name": "string",
  "email": "user@example.com",
  "organisation_id": "uuid",
  "organisation_role": "string",
  "job_title": "string",
  "status": "active|inactive|pending_setup",
  "admin_status": "pending|approved|rejected|suspended",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

Errors:
- 404 if user not found
- 403 if user is not approved

---

### List users (paginated)

- Method: GET
- Path: `/admin/users/list_users`
- Query params:
  - `limit` (int, 1-100, default 20)
  - `cursor` (string, optional)
  - `organisation_id` (uuid, optional) - Filter users by organization
- Roles: Super admin or Admin

Response data:

```json
{
  "users": [
    {
      "user_id": "uuid",
      "first_name": "string",
      "last_name": "string",
      "email": "user@example.com",
      "organisation_id": "uuid",
      "organisation_role": "string",
      "job_title": "string",
      "status": "active|inactive|pending_setup",
      "admin_status": "pending|approved|rejected|suspended",
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:00:00Z"
    }
  ],
  "cursor": "opaque-string-or-null"
}
```

---

### Update user admin status

- Method: PUT
- Path: `/admin/users/{user_id}/admin_status`
- Roles: Super admin only

Path params:
- `user_id` (uuid)

Request body:

```json
{
  "admin_status": "pending|approved|rejected|suspended",
  "note": "string"
}
```

Response data:

```json
{
  "user_id": "uuid",
  "status": "active|inactive|pending_setup",
  "admin_status": "pending|approved|rejected|suspended",
  "updated_at": "2024-01-01T00:00:00Z",
  "approved_at": "2024-01-01T00:00:00Z",
  "approved_by": "uuid"
}
```

Errors:
- 400 if invalid status transition

---

### Update user details

- Method: PUT
- Path: `/admin/users/{user_id}/update`
- Roles: Super admin or Admin

Path params:
- `user_id` (uuid)

Request body (send only fields to update; all optional):

```json
{
  "first_name": "string",
  "last_name": "string",
  "job_title": "string",
  "organisation_role": "string",
  "user_role": "member|admin"
}
```

Response data:

```json
{
  "user_id": "uuid",
  "first_name": "string",
  "last_name": "string",
  "email": "user@example.com",
  "organisation_id": "uuid",
  "organisation_role": "string",
  "job_title": "string",
  "status": "active|inactive|pending_setup",
  "admin_status": "pending|approved|rejected|suspended",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

Errors:
- 404 if user not found
- 400 if invalid data

---

### List user action history

- Method: GET
- Path: `/admin/users/history/organisation/{organisation_id}`
- Query params:
  - `limit` (int, 1-100, default 20)
  - `cursor` (string, optional)
- Roles: Super admin or Admin

Path params:
- `organisation_id` (uuid)

Response data:

```json
{
  "history": [
    {
      "id": "uuid",
      "user_id": "uuid",
      "action": "created|updated|status_changed|role_changed|deleted|login|password_reset|email_verified|field_changed",
      "note": "string",
      "details": {},
      "created_at": "2024-01-01T00:00:00Z",
      "actor_admin_id": "uuid"
    }
  ],
  "cursor": "opaque-string-or-null"
}
```

---

### Status Enums

#### UserAppRole
- `member` - Regular user
- `admin` - Organization admin

#### SupabaseUserStatus
- `active` - User account is active in Supabase
- `inactive` - User account is inactive in Supabase
- `pending_setup` - User account is pending setup

#### AdminUserStatus
- `pending` - User is pending admin approval
- `approved` - User has been approved by admin
- `rejected` - User has been rejected by admin
- `suspended` - User has been suspended by admin

#### UserEventType
- `created` - User was created
- `updated` - User was updated
- `status_changed` - User status was changed
- `role_changed` - User role was changed
- `deleted` - User was deleted
- `login` - User logged in
- `password_reset` - User password was reset
- `email_verified` - User email was verified
- `field_changed` - User field was changed
