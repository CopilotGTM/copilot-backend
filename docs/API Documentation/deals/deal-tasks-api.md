# Deal Tasks API

This API manages tasks associated with CRM deals. Tasks can be synced from external CRMs or created by users inside Copilot.

**Base URL**: `/users/crm`

---

## Overview

### Sources

| Source | Description | Notes |
| --- | --- | --- |
| CRM-synced | Tasks imported from HubSpot/Salesforce | Read-only |
| User-created | Tasks created via this API | Read/write |

User-created tasks have `integration_id = null` and `properties.is_user_created = true`.

---

## Response Envelope

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

---

## Authentication

- Bearer token required (`Authorization: Bearer <access_token>`)
- Organisation/user are inferred from the token

---

## Rate Limiting

| Operation | Limit |
| --- | --- |
| Read (`GET`) | ~60/min, 1000/hour per user |
| Write (`POST`/`PATCH`/`DELETE`) | ~30/min, 500/hour per user |

---

## Data Shapes (TypeScript)

```ts
export type UUID = string;

export interface TaskListItem {
  task_id: UUID;
  external_id: string;
  subject: string | null;
  body: string | null;
  status: string | null;
  task_type: string | null; // TASK | TODO
  priority: string | null;  // NONE | LOW | MEDIUM | HIGH | URGENT
  due_date: string | null;
  completed_date: string | null;
  reminder_date: string | null;
  owner_external_id: string | null;
  owner_name: string | null;
  owner_email: string | null;
  deal_id: UUID | null;
  deal_external_id: string | null;
  is_overdue: boolean;
  created_at: string;
  updated_at: string;
  synced_at: string;
}

export interface TaskDetail extends TaskListItem {
  source_created_at: string | null;
  source_modified_at: string | null;
  is_deleted_remote: boolean;
  properties: Record<string, any> | null;
  metrics: Record<string, any> | null;
  integration_id: UUID | null;
  organisation_id: UUID;
  last_seen_at: string | null;
}
```

---

## 1. List Tasks for a Deal

```http
GET /deals/{deal_id}/tasks
```

### Query Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `limit` | number | No | 1-100 (default 50) |
| `page` | number | No | Page number (1-indexed, default 1) |
| `statuses` | string[] | No | Filter by status |
| `priorities` | string[] | No | Filter by priority |
| `task_types` | string[] | No | Filter by TASK/TODO |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Tasks retrieved successfully",
  "data": {
    "items": [],
    "page": 1,
    "limit": 50,
    "total_count": 0,
    "total_pages": 0
  }
}
```

---

## 2. Get Task Detail

```http
GET /tasks/{task_id}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Task retrieved successfully",
  "data": {
    "task_id": "e7c4c3b1-0b70-4a5c-9071-3a21e1d9f3e1",
    "external_id": "e7c4c3b1-0b70-4a5c-9071-3a21e1d9f3e1",
    "subject": "Prep discovery agenda",
    "body": "Collect last call notes and build agenda.",
    "status": "NOT_STARTED",
    "task_type": "TASK",
    "priority": "MEDIUM",
    "due_date": "2025-12-24T10:30:00Z",
    "completed_date": null,
    "reminder_date": null,
    "owner_external_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
    "owner_name": "Jordan Lee",
    "owner_email": "jordan@example.com",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "deal_external_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "is_overdue": false,
    "source_created_at": "2025-12-24T10:00:00Z",
    "source_modified_at": "2025-12-24T10:00:00Z",
    "is_deleted_remote": false,
    "properties": { "is_user_created": true, "created_via": "api" },
    "metrics": {},
    "integration_id": null,
    "organisation_id": "3ed09720-971e-4fb1-a1d9-bd5e57b432f1",
    "synced_at": "2025-12-24T10:00:00Z",
    "last_seen_at": "2025-12-24T10:00:00Z",
    "created_at": "2025-12-24T10:00:00Z",
    "updated_at": "2025-12-24T10:00:00Z"
  }
}
```

---

## 3. Create Task

```http
POST /deals/{deal_id}/tasks
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | No | Task subject/title (max 500 chars) |
| `body` | string | No | Task body (max 10000 chars) |
| `status` | string | No | NOT_STARTED, IN_PROGRESS, COMPLETED, WAITING, DEFERRED, CANCELLED |
| `task_type` | string | No | TASK or TODO |
| `priority` | string | No | NONE, LOW, MEDIUM, HIGH, URGENT |
| `due_date` | string | No | ISO8601 datetime |
| `reminder_date` | string | No | ISO8601 datetime |
| `completed_date` | string | No | ISO8601 datetime |
| `owner_user_id` | UUID | No | Assign to an org user |

Notes:
- `subject` or `body` is required.
- If `status` is COMPLETED and `completed_date` is omitted, it is set to now.
- If `status` is not COMPLETED, `completed_date` is cleared.

### Success Response (201)

Returns `TaskDetail`.

---

## 4. Update Task

```http
PATCH /tasks/{task_id}
```

### Request Body

Any subset of the create fields. `task_type`, `status`, and `priority` cannot be null.

Notes:
- If `completed_date` is provided, the task status is set to COMPLETED.
- If `status` is not COMPLETED, `completed_date` is cleared.
- External CRM tasks are read-only and return 409.

### Success Response (200)

Returns `TaskDetail`.

---

## 5. Delete Task

```http
DELETE /tasks/{task_id}
```

### Notes

- Soft deletes the task.
- External CRM tasks are read-only and return 409.

### Success Response (200)

Returns `TaskDetail`.
