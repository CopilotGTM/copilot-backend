# Admin Organisations API

Manage organisations from the admin console, including provisioning, status changes, and history.

## Base URL

```text
/admin/organisations
```

## Response Envelope

`{ "status": "SUCCESS|ERROR", "message": "...", "data": {...}|null }`

## Authentication & Roles

- Header: `Authorization: Bearer <admin_access_token>`
- Roles:
  - `super_admin` required for create, status updates, and full history.
  - `admin` can view and update organisation details, and read organisation-scoped history.

## Key Enums

| Enum | Values |
|------|--------|
| `OrganisationStatus` | `provisioning`, `pending`, `approved`, `rejected`, `suspended`, `deactivated`, `failed` |
| `OrganisationEventType` | `created`, `approved`, `rejected`, `suspended`, `reinstated`, `status_changed`, `field_changed`, `user_added`, `user_removed`, `role_changed`, `note_added`, `system_migration`, `deleted` |

---

## 1) Create Organisation

**Endpoint**

```http
POST /create_organisation
```

**Role**: `super_admin`

**Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `organisation_name` | string | Yes | Display name (must be unique) |
| `country` | string | No | Country/region |
| `sales_team_size` | integer | No | Optional headcount |
| `company_address` | string | No | Mailing address |
| `linked_urls` | string[] | No | Related social/profile links |
| `website_urls` | string[] | No | Primary website URLs |
| `summary` | string | No | Short description |
| `logo_url` | string | No | Logo URL |

**Success**

```json
{
  "status": "SUCCESS",
  "message": "Organization created successfully",
  "data": {
    "organisation_id": "uuid",
    "organisation_name": "Acme Corp",
    "schema_name": "org_12345",
    "created_at": "2025-12-02T10:15:00Z",
    "updated_at": "2025-12-02T10:15:00Z"
  }
}
```

Notes:
- Provisioning runs synchronously (schema, migrations, storage) before returning.
- Initial status transitions to `pending` after provisioning.

---

## 2) List Organisations (paginated)

**Endpoint**

```http
GET /list_organisations?limit=20&cursor=<opaque>
```

**Role**: `super_admin` or `admin`

**Query params**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | integer (1-100) | 20 | Page size |
| `cursor` | string | null | Opaque cursor from previous page |

**Response**

```json
{
  "status": "SUCCESS",
  "message": "Organizations retrieved successfully",
  "data": {
    "organizations": [
      {
        "organisation_id": "uuid",
        "organisation_name": "Acme Corp",
        "is_active": true,
        "country": "US",
        "sales_team_size": 120,
        "company_address": "123 Market St",
        "linked_urls": ["https://www.linkedin.com/company/acme"],
        "website_urls": ["https://acme.com"],
        "summary": "B2B sales platform",
        "logo_url": "https://...",
        "status": "approved",
        "created_at": "2025-11-20T10:00:00Z",
        "updated_at": "2025-11-28T09:30:00Z"
      }
    ],
    "cursor": "eyJjcmVhdGVkX2F0Ijoi..."
  }
}
```

---

## 3) Get Organisation by ID

**Endpoint**

```http
GET /{organisation_id}
```

**Role**: `super_admin` or `admin`

Returns the same shape as a single item in the list response.

---

## 4) Update Organisation

**Endpoint**

```http
PUT /{organisation_id}/update
```

**Role**: `super_admin` or `admin`

**Body** (all fields optional; at least one is required)

| Field | Type | Description |
|-------|------|-------------|
| `organisation_name` | string | New name |
| `country` | string | Country/region |
| `sales_team_size` | integer | Updated headcount |
| `company_address` | string | Address |
| `linked_urls` | string[] | Social/profile links |
| `website_urls` | string[] | Website URLs |
| `summary` | string | Description |
| `logo_url` | string | Logo URL |
| `is_active` | boolean | Toggle active flag |

**Response**

```json
{
  "status": "SUCCESS",
  "message": "Organization updated successfully",
  "data": { "...organisation fields..." }
}
```

Errors: `400` if no fields are provided; `403` if role is insufficient; `404` if not found.

---

## 5) Update Organisation Status

**Endpoint**

```http
PUT /{organisation_id}/status
```

**Role**: `super_admin`

**Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | `OrganisationStatus` | Yes | New lifecycle status |
| `note` | string | No | Optional audit note |

**Success**

```json
{
  "status": "SUCCESS",
  "message": "Organization status updated to 'approved'",
  "data": {
    "organisation_id": "uuid",
    "status": "approved",
    "updated_at": "2025-12-02T10:20:00Z",
    "approved_at": "2025-12-02T10:20:00Z",
    "approved_by": "admin-uuid"
  }
}
```

---

## 6) All Organisations History (paginated)

**Endpoint**

```http
GET /all_organisations_history?limit=20&cursor=<opaque>
```

**Role**: `super_admin`

**Response data**

```json
{
  "history": [
    {
      "id": "uuid",
      "organisation_id": "uuid",
      "action": "created",
      "note": "Organization provisioned and ready for approval.",
      "details": { "metrics": { "migrations": 1.2 } },
      "created_at": "2025-11-20T10:00:00Z",
      "actor_admin_id": "admin-uuid"
    }
  ],
  "cursor": "eyJjcmVhdGVkX2F0Ijoi..."
}
```

Each `action` is one of `OrganisationEventType`. Treat `cursor` as opaque.

---

## 7) Organisation History by ID (paginated)

**Endpoint**

```http
GET /{organisation_id}/history?limit=20&cursor=<opaque>
```

**Role**: `super_admin` or `admin`

Same response shape as "All Organisations History", filtered to a single organisation.
