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

## Rate Limits

- Most organisation admin endpoints are not explicitly rate-limited at the handler level today.
- `GET /countries` is rate-limited (per-admin, read budget) and may return `429 Too Many Requests`.

## Key Enums

| Enum                    | Values                                                                                                                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `OrganisationStatus`    | `provisioning`, `pending`, `approved`, `rejected`, `suspended`, `deactivated`, `failed`                                                                                                    |
| `OrganisationEventType` | `created`, `approved`, `rejected`, `suspended`, `reinstated`, `status_changed`, `field_changed`, `user_added`, `user_removed`, `role_changed`, `note_added`, `system_migration`, `deleted` |

---

## 1) Create Organisation

### Endpoint — Create Organisation

```http
POST /create_organisation
```

#### Role — Create Organisation

`super_admin`

#### Body — Create Organisation

| Field               | Type     | Required | Description                                                            |
| ------------------- | -------- | -------- | ---------------------------------------------------------------------- |
| `organisation_name` | string   | Yes      | Display name (must be unique)                                          |
| `agent_name`        | string   | No       | Agent display/name used in agent contexts (e.g. Celcius)               |
| `domain`            | string[] | No       | Email domains for the org (used for org-specific email identification) |
| `country`           | string   | No       | Country/region                                                         |
| `sales_team_size`   | integer  | No       | Optional headcount                                                     |
| `company_address`   | string   | No       | Mailing address                                                        |
| `linked_urls`       | string[] | No       | Related social/profile links                                           |
| `website_urls`      | string[] | No       | Primary website URLs                                                   |
| `summary`           | string   | No       | Short description                                                      |
| `logo_url`          | string   | No       | Logo URL                                                               |

#### Success — Create Organisation

```json
{
  "status": "SUCCESS",
  "message": "Organisation creation job accepted",
  "data": {
    "job_id": "uuid",
    "job_type": "create",
    "status": "pending",
    "message": "Organisation creation job accepted",
    "stream_url": "/admin/organisations/jobs/<job_id>/stream"
  }
}
```

Notes:

- This endpoint is async (job-based): it returns immediately with a `job_id`.
- **Use `GET /jobs/{job_id}/stream` (SSE) for real-time status updates** — this is the only approach for tracking job progress.
- Active jobs also appear in `GET /list_organisations` via the `active_job` field.

---

### Endpoint — Create Organisation (Sync, Deprecated)

```http
POST /create_organisation_sync
```

#### Role — Create Organisation (Sync)

`super_admin`

#### Notes — Create Organisation (Sync)

- Deprecated: blocks while provisioning runs (may timeout).
- Prefer `POST /create_organisation` + SSE.

---

## 2) List Organisations (paginated)

### Endpoint — List Organisations

```http
GET /list_organisations?limit=20&cursor=<opaque>
```

#### Role — List Organisations

`super_admin` or `admin`

#### Query params — List Organisations

| Param    | Type            | Default | Description                      |
| -------- | --------------- | ------- | -------------------------------- |
| `limit`  | integer (1-100) | 20      | Page size                        |
| `cursor` | string          | null    | Opaque cursor from previous page |

#### Response — List Organisations

Each organisation in the list includes an optional `active_job` field if there's a pending or in-progress job (e.g., provisioning).

```json
{
  "status": "SUCCESS",
  "message": "Organizations retrieved successfully",
  "data": {
    "organizations": [
      {
        "organisation_id": "uuid",
        "organisation_name": "Acme Corp",
        "agent_name": "Shield",
        "domain": ["acme.com"],
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
        "updated_at": "2025-11-28T09:30:00Z",
        "active_job": null
      },
      {
        "organisation_id": "uuid-2",
        "organisation_name": "NewCorp",
        "status": "provisioning",
        "active_job": {
          "job_id": "job-uuid",
          "job_type": "create",
          "status": "in_progress",
          "progress": 40,
          "status_message": "Running migrations",
          "stream_url": "/admin/organisations/jobs/job-uuid/stream",
          "created_at": "2025-12-23T10:15:00Z"
        }
      }
    ],
    "cursor": "eyJjcmVhdGVkX2F0Ijoi..."
  }
}
```

#### Active Job Field

When an organisation has an active job (status = `pending` or `in_progress`), the `active_job` field is populated:

| Field            | Type   | Description                                      |
| ---------------- | ------ | ------------------------------------------------ |
| `job_id`         | UUID   | Job identifier                                   |
| `job_type`       | string | `create`, `update`, `delete`, `migrate`          |
| `status`         | string | `pending` or `in_progress`                       |
| `progress`       | int    | 0-100 progress percentage                        |
| `status_message` | string | Human-readable status                            |
| `stream_url`     | string | SSE endpoint for real-time updates               |
| `created_at`     | string | ISO timestamp                                    |

Use `stream_url` to subscribe to real-time job updates via SSE.

---

## 2a) Organisation Job SSE Streaming

Organisation provisioning runs as an async job. Track job progress in real-time using SSE.

### Job Enums

| Enum                    | Values                                                      |
| ----------------------- | ----------------------------------------------------------- |
| `OrganisationJobType`   | `create`, `update`, `delete`, `migrate`                     |
| `OrganisationJobStatus` | `pending`, `in_progress`, `completed`, `failed`, `cancelled`|

### Endpoint — Stream Job Status (SSE) ⭐ Recommended

```http
GET /jobs/{job_id}/stream
```

**This is the only approach for tracking job progress.** It provides real-time updates without polling overhead.

#### Role — Stream Job Status

`super_admin` or `admin`

#### SSE Events

This endpoint returns a Server-Sent Events stream. Events include:

- `status`: job status update with progress
- `complete`: job reached a terminal state (`completed`, `failed`, or `cancelled`)
- `ping`: heartbeat (every 25 seconds)
- `timeout`: stream timed out (10 minutes default)
- `error`: stream error (client should reconnect)

#### Headers — Stream Job Status (SSE)

The stream requires admin authentication (same as other admin endpoints).

```http
Authorization: Bearer <admin_access_token>
Accept: text/event-stream
Cache-Control: no-cache
```

Server-side streaming headers:

- The API sets `Cache-Control: no-cache` and `X-Accel-Buffering: no` on the SSE response to reduce proxy buffering and ensure updates flush to the client in real time.

Important frontend note:

- The browser `EventSource` API does **not** allow setting `Authorization` headers.
- Use a fetch-based SSE client (streaming `fetch`) or an EventSource polyfill that supports headers.

#### Event Payload — `status`

`status` events carry the latest job snapshot (either from DB on connect, or from Redis updates).

```json
{
  "job_id": "uuid",
  "status": "pending|in_progress|completed|failed|cancelled",
  "progress": 0,
  "status_message": "string|null",
  "result_data": { "...": "..." },
  "error_message": "string|null",
  "error_code": "string|null",
  "updated_at": "2025-12-23T10:15:10Z",
  "completed_at": "2025-12-23T10:16:12Z|null"
}
```

Notes:

- The first event after connecting is always a `status` snapshot.
- The final `status` event will typically contain `result_data` on success or `error_message`/`error_code` on failure.

#### Event Payload — `complete`

`complete` is a lightweight terminal signal; clients should stop listening after receiving it.

```json
{ "message": "job_completed|job_already_terminal" }
```

#### Event Payload — `ping`

Heartbeat to keep the connection alive.

```json
{ "timestamp": 1734940000 }
```

#### Event Payload — `timeout`

The server closes the stream after ~10 minutes; the client should reconnect if the job is still not terminal.

```json
{
  "message": "Stream timeout reached. Please reconnect.",
  "timeout_seconds": 600,
  "reconnect": true
}
```

#### Event Payload — `error`

Errors are sent as SSE `error` events (not as a JSON HTTP response). Common cases:

- Forbidden:

```json
{ "message": "Access denied", "code": "FORBIDDEN" }
```

- Not found / not owned by current admin:

```json
{ "message": "Job not found or access denied", "code": "NOT_FOUND" }
```

- Internal stream error (client should reconnect):

```json
{
  "message": "Internal stream error. Please reconnect.",
  "error_type": "ExceptionType",
  "reconnect": true
}
```

#### Frontend Integration (Fetch-based SSE)

Minimal pattern (pseudo-code):

```ts
const res = await fetch(`/admin/organisations/jobs/${jobId}/stream`, {
  headers: {
    Authorization: `Bearer ${token}`,
    Accept: "text/event-stream",
  },
});

// Parse SSE frames from res.body (ReadableStream)
// Handle events: status | ping | timeout | error | complete
// Stop on `complete`; reconnect on `timeout`/`error` when `reconnect: true`.
```

Example frames:

```text
event: status
data: {"job_id":"...","status":"in_progress","progress":20,"status_message":"Creating organisation record","result_data":null,"error_message":null,"error_code":null,"updated_at":"2025-12-23T10:15:10Z"}

event: complete
data: {"message":"job_completed"}
```

## 3) Get Organisation by ID

### Endpoint — Get Organisation by ID

```http
GET /{organisation_id}
```

#### Role — Get Organisation by ID

`super_admin` or `admin`

Returns the same shape as a single item in the list response.

---

## 4) Update Organisation

### Endpoint — Update Organisation

```http
PUT /{organisation_id}/update
```

#### Role — Update Organisation

`super_admin` or `admin`

#### Body — Update Organisation (all fields optional; at least one is required)

| Field               | Type     | Description                                                            |
| ------------------- | -------- | ---------------------------------------------------------------------- |
| `organisation_name` | string   | New name                                                               |
| `agent_name`        | string   | Agent display/name used in agent contexts (e.g. Celcius)               |
| `domain`            | string[] | Email domains for the org (used for org-specific email identification) |
| `country`           | string   | Country/region                                                         |
| `sales_team_size`   | integer  | Updated headcount                                                      |
| `company_address`   | string   | Address                                                                |
| `linked_urls`       | string[] | Social/profile links                                                   |
| `website_urls`      | string[] | Website URLs                                                           |
| `summary`           | string   | Description                                                            |
| `logo_url`          | string   | Logo URL                                                               |
| `is_active`         | boolean  | Toggle active flag                                                     |

#### Response — Update Organisation

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

### Endpoint — Update Organisation Status

```http
PUT /{organisation_id}/status
```

#### Role — Update Organisation Status

`super_admin`

#### Body — Update Organisation Status

| Field    | Type                 | Required | Description           |
| -------- | -------------------- | -------- | --------------------- |
| `status` | `OrganisationStatus` | Yes      | New lifecycle status  |
| `note`   | string               | No       | Optional audit note   |

#### Success — Update Organisation Status

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

### Endpoint — All Organisations History

```http
GET /all_organisations_history?limit=20&cursor=<opaque>
```

Note: `limit` is capped at `20` (even if a higher value is provided).

#### Role — All Organisations History

`super_admin`

#### Response data — All Organisations History

```json
{
  "history": [
    {
      "id": "uuid",
      "organisation_id": "uuid",
      "organisation_name": "Acme Inc",
      "organisation_status": "approved",
      "action": "created",
      "note": "Organization provisioned and ready for approval.",
      "details": { "metrics": { "migrations": 1.2 } },
      "created_at": "2025-11-20T10:00:00Z",
      "actor_admin_id": "admin-uuid",
      "actor_admin_name": "Jane Doe",
      "actor_admin_email": "admin@company.com"
    }
  ],
  "cursor": "eyJjcmVhdGVkX2F0Ijoi..."
}
```

Each `action` is one of `OrganisationEventType`. Treat `cursor` as opaque.

---

## 7) Organisation History by ID (paginated)

### Endpoint — Organisation History by ID

```http
GET /{organisation_id}/history?limit=20&cursor=<opaque>
```

#### Role — Organisation History by ID

`super_admin` or `admin`

Same response shape as "All Organisations History", filtered to a single organisation.

---

## 8) List Allowed Countries

Returns the allowed countries list used by the admin console when selecting an organisation's country.

### Endpoint — List Allowed Countries

```http
GET /countries
```

#### Role — List Allowed Countries

`super_admin` or `admin`

#### Rate limiting — List Allowed Countries

per-admin read limiter (may return `429`).

#### Response — List Allowed Countries

```json
{
  "status": "SUCCESS",
  "message": "Countries retrieved successfully",
  "data": {
    "countries": [
      { "code": "US", "name": "United States" }
    ]
  }
}
```

Open question:

- What should the allowed list be (e.g., countries meeting a specific policy/human-rights/compliance criteria)?
- Current default behavior is USA-only via code-owned metadata (future: DB-backed table).
