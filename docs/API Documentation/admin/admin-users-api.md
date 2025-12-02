# Admin Users API

Admin-facing user management: create users, update roles/status, and review history.

## Base URL

```text
/admin/users
```

## Response Envelope

`{ "status": "SUCCESS|ERROR", "message": "...", "data": {...}|null }`

## Authentication & Roles

- Header: `Authorization: Bearer <admin_access_token>`
- Roles:
  - `super_admin` only: update admin status.
  - `super_admin` or `admin`: create, list, fetch profile, update user details, and view history.

## Key Enums

| Enum | Values |
|------|--------|
| `UserAppRole` | `member`, `admin` |
| `SupabaseUserStatus` | `ACTIVE`, `INACTIVE`, `PASSWORD_CHANGE_REQUIRED` |
| `AdminUserStatus` | `pending`, `approved`, `rejected`, `suspended`, `deactivated` |
| `UserEventType` | `created`, `updated`, `status_changed`, `role_changed`, `deleted`, `login`, `password_reset`, `email_verified`, `field_changed` |

---

## 1) Create User

Creates a Supabase user and sends a temporary password email.

### Endpoint

```http
POST /create_user
```

### Role

`super_admin` or `admin`

### Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `first_name` | string | Yes | User first name |
| `last_name` | string | Yes | User last name |
| `email` | string (email) | Yes | Unique user email |
| `organisation_id` | UUID | Yes | Organisation the user belongs to |
| `user_role` | `member` \| `admin` | Yes | App role |
| `organisation_role` | string | Yes | Job/role title inside the org |
| `job_title` | string | No | Optional job title |

### Success

```json
{
  "status": "SUCCESS",
  "message": "User created successfully",
  "data": {
    "user_id": "uuid",
    "email": "jane@acme.com",
    "created_at": "2025-12-02T10:18:00Z"
  }
}
```

Errors: `400` for missing fields/invalid role, `403` if role is insufficient, `404` if organisation not found, `409` if user already exists.

---

## 2) Get User Profile

Only returns approved users; non-approved users yield `403`.

**Endpoint**

```http
GET /{user_id}/profile
```

### Role

`super_admin` or `admin`

### Success

```json
{
  "status": "SUCCESS",
  "message": "User retrieved successfully",
  "data": {
    "user_id": "uuid",
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane@acme.com",
    "organisation_id": "org-uuid",
    "organisation_role": "Account Executive",
    "job_title": "AE",
    "status": "ACTIVE",
    "admin_status": "approved",
    "created_at": "2025-11-20T10:00:00Z",
    "updated_at": "2025-11-25T09:30:00Z"
  }
}
```

---

## 3) List Users (paginated)

**Endpoint**

```http
GET /list_users?limit=20&cursor=<opaque>&organisation_id=<uuid>
```

### Role

`super_admin` or `admin`

### Query params

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | integer (1-100) | 20 | Page size |
| `cursor` | string | null | Opaque cursor from previous page |
| `organisation_id` | UUID | null | Filter to a single organisation |

### Response

```json
{
  "status": "SUCCESS",
  "message": "Users retrieved successfully",
  "data": {
    "users": [
      {
        "user_id": "uuid",
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@acme.com",
        "organisation_id": "org-uuid",
        "organisation_role": "Account Executive",
        "job_title": "AE",
        "status": "ACTIVE",
        "admin_status": "approved",
        "created_at": "2025-11-20T10:00:00Z",
        "updated_at": "2025-11-25T09:30:00Z"
      }
    ],
    "cursor": "eyJjcmVhdGVkX2F0Ijoi..."
  }
}
```

---

## 4) Update User Admin Status

Approve/reject/suspend a user; records history and approval metadata.

**Endpoint**

```http
PUT /{user_id}/admin_status
```

### Role

`super_admin`

### Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `admin_status` | `AdminUserStatus` | Yes | New admin status |
| `note` | string | No | Optional audit note |

### Success

```json
{
  "status": "SUCCESS",
  "message": "User admin status updated to 'approved'",
  "data": {
    "user_id": "uuid",
    "status": "ACTIVE",
    "admin_status": "approved",
    "updated_at": "2025-12-02T10:22:00Z",
    "approved_at": "2025-12-02T10:22:00Z",
    "approved_by": "admin-uuid"
  }
}
```

---

## 5) Update User Details

**Endpoint**

```http
PUT /{user_id}/update
```

### Role

`super_admin` or `admin`

### Body

All fields optional; at least one required.

| Field | Type | Description |
|-------|------|-------------|
| `first_name` | string | Updated first name |
| `last_name` | string | Updated last name |
| `job_title` | string | Job title |
| `organisation_role` | string | Org role/title |
| `user_role` | `member` \| `admin` | App role |

### Response

```json
{
  "status": "SUCCESS",
  "message": "User updated successfully",
  "data": { "...user fields..." }
}
```

Errors: `400` if no update fields; `404` if user not found.

---

## 6) User Action History by Organisation (paginated)

**Endpoint**

```http
GET /history/organisation/{organisation_id}?limit=20&cursor=<opaque>
```

### Role

`super_admin` or `admin`

### Response data

```json
{
  "history": [
    {
      "id": "uuid",
      "user_id": "uuid",
      "action": "status_changed",
      "note": "Admin status changed to approved",
      "details": { "new_status": "approved" },
      "created_at": "2025-11-25T12:00:00Z",
      "actor_admin_id": "admin-uuid"
    }
  ],
  "cursor": "eyJjcmVhdGVkX2F0Ijoi..."
}
```

`action` values follow `UserEventType`. Treat `cursor` as opaque.
