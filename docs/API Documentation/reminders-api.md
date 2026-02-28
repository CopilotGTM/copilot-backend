# Reminders API Documentation

## Base URL

```text
/users/reminders
```

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope.

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

---

## Authentication

- Required: Bearer token (`Authorization: Bearer <access_token>`)
- User identity (organisation, user, role) resolved from the token; clients never pass these explicitly
- Reminders are scoped to the authenticated user — a user can only see and manage their own reminders

---

## Rate Limiting (defaults)

- Write endpoints (`POST`, `PATCH`, `DELETE`): ~15/min, 100/hour per user
- Read endpoints (`GET`): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After`

---

## Data Types

### Enums

#### ReminderType

| Value | Description |
|-------|-------------|
| `static` | Delivers a fixed text message at the trigger time |
| `agentic` | Runs an AI agent with a stored prompt at the trigger time and delivers the analysis result |

#### ReminderRecurrence

| Value | Description |
|-------|-------------|
| `none` | One-time reminder (fires once, then completes) |
| `daily` | Fires every day at the same time |
| `weekdays` | Fires Monday through Friday at the same time |
| `weekly` | Fires every 7 days |
| `biweekly` | Fires every 14 days |
| `monthly` | Fires on the same day each month |

#### ReminderStatus

| Value | Description | Terminal? |
|-------|-------------|-----------|
| `pending_confirmation` | Created via agent chat, awaiting user confirmation | No |
| `active` | Scheduled and will fire at `next_trigger_at` | No |
| `paused` | Temporarily stopped, can be resumed | No |
| `triggered` | Currently executing (transient state) | No |
| `completed` | One-time reminder fired, or recurring reminder ended | Yes |
| `failed` | Exceeded 3 consecutive failures, stopped retrying | Yes |
| `cancelled` | Permanently cancelled by user | Yes |

#### DeliveryChannel

| Value | Description | Prerequisite |
|-------|-------------|--------------|
| `email` | Deliver via email | User email available |
| `teams` | Deliver via Microsoft Teams | Teams bot installed for workspace |
| `slack` | Deliver via Slack | Slack bot installed for workspace |

### State Machine

```
                      ┌──────────────────────────────────┐
                      │                                  │
  [API Create] ──→ ACTIVE ──[Due]──→ TRIGGERED           │
                   ↑  │                │                  │
                   │  │         ┌──────┴──────┐           │
                   │  │    [Success,       [Success,      │
                   │  │    recurring]      one-time]      │
                   │  │         │              │           │
                   │  │      ACTIVE        COMPLETED      │
                   │  │                                   │
                   │  └──[Pause]──→ PAUSED ──[Resume]──→──┘
                   │
          [Cancel from active/paused/pending_confirmation
           when not running] ──→ CANCELLED
          [3 consecutive failures] ──→ FAILED
```

**Allowed transitions via API:**

- **Pause**: `active` → `paused`
- **Resume**: `paused` → `active` (trigger time must be in the future)
- **Cancel**: `active` | `paused` | `pending_confirmation` → `cancelled`
- **Cancel while `triggered`**: blocked with `409 Conflict`
- **Update**: Only allowed when status is `active` or `paused`

### Response Models (TypeScript)

```ts
type UUID = string;

/**
 * Reminder item returned in list views.
 * Optimized for table/card display.
 */
export interface ReminderListItem {
  reminder_id: UUID;
  title: string;
  reminder_type: "static" | "agentic";
  status: ReminderStatus;
  next_trigger_at: string;           // ISO8601
  next_trigger_local_at: string;     // ISO8601 in user's timezone
  schedule_summary: string;          // e.g. "Weekly on Monday at 12:00 AM IST (next: Mon, Mar 03)"
  timezone: string;                  // IANA timezone
  recurrence: ReminderRecurrence;
  delivery_channel: "email" | "teams" | "slack";
  is_running: boolean;
  current_run_status: "in_progress" | null;
  can_pause: boolean;
  can_cancel: boolean;
  can_delete: boolean;
  last_triggered_at: string | null;  // ISO8601
  trigger_count: number;
  created_at: string;                // ISO8601
}

/**
 * Full reminder detail returned for single-item views.
 */
export interface ReminderDetail {
  reminder_id: UUID;
  title: string;
  reminder_type: "static" | "agentic";
  status: ReminderStatus;

  // Content (mutually exclusive based on type)
  message_content: string | null;    // For static reminders
  agent_prompt: string | null;       // For agentic reminders
  deal_id: UUID | null;              // Optional deal scope for agentic

  // Schedule
  next_trigger_at: string;           // ISO8601
  next_trigger_local_at: string;     // ISO8601 in user's timezone
  schedule_summary: string;          // Human-friendly recurrence summary
  timezone: string;                  // IANA timezone
  recurrence: ReminderRecurrence;
  recurrence_end_at: string | null;  // ISO8601

  // Delivery
  delivery_channel: "email" | "teams" | "slack";

  // Tracking
  is_running: boolean;
  current_run_status: "in_progress" | null;
  can_pause: boolean;
  can_cancel: boolean;
  can_delete: boolean;
  last_triggered_at: string | null;  // ISO8601
  trigger_count: number;
  consecutive_failures: number;
  last_error: string | null;

  // Timestamps
  created_at: string;                // ISO8601
  updated_at: string;                // ISO8601
}

/**
 * Paginated list response.
 */
export interface ReminderListResponse {
  items: ReminderListItem[];
  page: number;
  limit: number;
  total_count: number;
  total_pages: number;
}

/**
 * Single execution history row for a reminder.
 */
export interface ReminderRunListItem {
  run_id: UUID;
  attempt_number: number;
  status: "in_progress" | "succeeded" | "delivery_failed" | "failed";
  triggered_for_at: string;         // ISO8601
  started_at: string;               // ISO8601
  completed_at: string | null;      // ISO8601
  input_snapshot: string;           // Static message or agent prompt executed for that run
  result_text: string | null;
  error_message: string | null;
}

/**
 * Paginated reminder history response.
 */
export interface ReminderHistoryResponse {
  items: ReminderRunListItem[];
  page: number;
  limit: number;
  total_count: number;
  total_pages: number;
}
```

---

## 1. Create Reminder

Create a new reminder or scheduled AI alert. The reminder goes directly to `active` status and is scheduled for execution.

### Endpoint (Create Reminder)

```http
POST /
```

### Request Body

```json
{
  "title": "Weekly deal health check",
  "reminder_type": "agentic",
  "agent_prompt": "Analyze the current state of this deal. Summarize recent activity, flag any risks, and suggest next steps.",
  "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "trigger_time": "2026-03-03T09:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "recurrence": "weekly",
  "recurrence_end_at": null,
  "delivery_channel": "teams"
}
```

#### Request Schema (`ReminderCreateRequest`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `title` | string | Yes | - | Short label (max 255 chars) |
| `reminder_type` | enum | Yes | - | `static` or `agentic` |
| `message_content` | string | Conditional | null | Text to deliver (required when `reminder_type` is `static`) |
| `agent_prompt` | string | Conditional | null | AI instruction to execute (required when `reminder_type` is `agentic`) |
| `deal_id` | UUID | No | null | Deal scope for agentic reminders |
| `trigger_time` | datetime | Yes | - | When to fire (ISO 8601 with timezone, must be in the future) |
| `timezone` | string | Yes | - | IANA timezone (e.g., `Asia/Kolkata`, `America/New_York`) |
| `recurrence` | enum | No | `none` | Recurrence pattern |
| `recurrence_end_at` | datetime | No | null | Stop recurring after this UTC datetime |
| `delivery_channel` | string | Yes | - | `email`, `teams`, or `slack` |

### Validation Rules

| Rule | Description |
|------|-------------|
| Static requires message | `message_content` is required when `reminder_type` is `static` |
| Agentic requires prompt | `agent_prompt` is required when `reminder_type` is `agentic` |
| Future trigger time | `trigger_time` must be in the future |
| Valid timezone | Must be a valid IANA timezone string |
| Valid channel | `delivery_channel` must be `email`, `teams`, or `slack` |

### Success Response (201) (Create Reminder)

```json
{
  "status": "SUCCESS",
  "message": "Reminder created successfully",
  "data": {
    "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Weekly deal health check",
    "reminder_type": "agentic",
    "status": "active",
    "message_content": null,
    "agent_prompt": "Analyze the current state of this deal. Summarize recent activity, flag any risks, and suggest next steps.",
    "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "next_trigger_at": "2026-03-03T03:30:00Z",
    "next_trigger_local_at": "2026-03-03T09:00:00+05:30",
    "schedule_summary": "Weekly on Tuesday at 9:00 AM IST (next: Tue, Mar 03)",
    "timezone": "Asia/Kolkata",
    "recurrence": "weekly",
    "recurrence_end_at": null,
    "delivery_channel": "teams",
    "is_running": false,
    "current_run_status": null,
    "can_pause": true,
    "can_cancel": true,
    "can_delete": true,
    "last_triggered_at": null,
    "trigger_count": 0,
    "consecutive_failures": 0,
    "last_error": null,
    "created_at": "2026-02-28T10:00:00Z",
    "updated_at": "2026-02-28T10:00:00Z"
  }
}
```

### Static Reminder Example

```json
{
  "title": "Follow up with Acme Corp",
  "reminder_type": "static",
  "message_content": "Remember to send the updated proposal to Acme Corp before end of day.",
  "trigger_time": "2026-03-01T16:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "recurrence": "none",
  "delivery_channel": "slack"
}
```

### Error Responses (Create Reminder)

| Code | Description |
|------|-------------|
| `400` | Validation error (missing content, past trigger time, invalid timezone) |
| `401` | Missing or invalid authentication token |
| `422` | Request body validation failure |
| `500` | Internal server error |

### Example cURL (Create Reminder)

```bash
curl -X POST "$BASE_URL/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Daily pipeline review",
    "reminder_type": "agentic",
    "agent_prompt": "Give me a summary of all deals that had activity today.",
    "trigger_time": "2026-03-01T17:00:00+05:30",
    "timezone": "Asia/Kolkata",
    "recurrence": "weekdays",
    "delivery_channel": "teams"
  }'
```

---

## 2. List Reminders

Get a paginated list of the current user's reminders. Supports filtering by status and type.

### Endpoint (List Reminders)

```http
GET /
```

### Query Parameters (List Reminders)

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | No | `20` | Items per page (1-100) |
| `page` | integer | No | `1` | Page number (1-indexed) |
| `status` | string | No | - | Comma-separated status filter (e.g., `active,paused`) |
| `reminder_type` | enum | No | - | Filter by `static` or `agentic` |
| `sort` | string | No | `desc` | Sort by `next_trigger_at` (`asc` or `desc`) |

### Success Response (200) (List Reminders)

```json
{
  "status": "SUCCESS",
  "message": "Reminders retrieved successfully",
  "data": {
    "items": [
      {
        "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "title": "Weekly deal health check",
        "reminder_type": "agentic",
        "status": "active",
        "next_trigger_at": "2026-03-03T03:30:00Z",
        "next_trigger_local_at": "2026-03-03T09:00:00+05:30",
        "schedule_summary": "Weekly on Tuesday at 9:00 AM IST (next: Tue, Mar 03)",
        "timezone": "Asia/Kolkata",
        "recurrence": "weekly",
        "delivery_channel": "teams",
        "is_running": false,
        "current_run_status": null,
        "can_pause": true,
        "can_cancel": true,
        "can_delete": true,
        "last_triggered_at": null,
        "trigger_count": 0,
        "created_at": "2026-02-28T10:00:00Z"
      },
      {
        "reminder_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        "title": "Follow up with Acme Corp",
        "reminder_type": "static",
        "status": "active",
        "next_trigger_at": "2026-03-01T10:30:00Z",
        "next_trigger_local_at": "2026-03-01T16:00:00+05:30",
        "schedule_summary": "One-time on Sun, Mar 01 at 4:00 PM IST",
        "timezone": "Asia/Kolkata",
        "recurrence": "none",
        "delivery_channel": "slack",
        "is_running": false,
        "current_run_status": null,
        "can_pause": true,
        "can_cancel": true,
        "can_delete": true,
        "last_triggered_at": null,
        "trigger_count": 0,
        "created_at": "2026-02-28T09:00:00Z"
      }
    ],
    "page": 1,
    "limit": 20,
    "total_count": 2,
    "total_pages": 1
  }
}
```

### Error Responses (List Reminders)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `500` | Internal server error |

### Example cURL (List Reminders)

```bash
# List active reminders
curl -X GET "$BASE_URL/?status=active&sort=asc" \
  -H "Authorization: Bearer $TOKEN"

# List all agentic reminders including paused
curl -X GET "$BASE_URL/?status=active,paused&reminder_type=agentic&limit=50" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 3. Get Reminder Detail

Get full details of a specific reminder including content, schedule, and execution tracking.

### Endpoint (Get Reminder Detail)

```http
GET /{reminder_id}
```

### Path Parameters (Get Reminder Detail)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reminder_id` | UUID | Yes | The unique identifier of the reminder |

### Success Response (200) (Get Reminder Detail)

```json
{
  "status": "SUCCESS",
  "message": "Reminder retrieved successfully",
  "data": {
    "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Weekly deal health check",
    "reminder_type": "agentic",
    "status": "active",
    "message_content": null,
    "agent_prompt": "Analyze the current state of this deal. Summarize recent activity, flag any risks, and suggest next steps.",
    "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "next_trigger_at": "2026-03-03T03:30:00Z",
    "next_trigger_local_at": "2026-03-03T09:00:00+05:30",
    "schedule_summary": "Weekly on Tuesday at 9:00 AM IST (next: Tue, Mar 03)",
    "timezone": "Asia/Kolkata",
    "recurrence": "weekly",
    "recurrence_end_at": null,
    "delivery_channel": "teams",
    "is_running": false,
    "current_run_status": null,
    "can_pause": true,
    "can_cancel": true,
    "can_delete": true,
    "last_triggered_at": "2026-02-24T03:30:00Z",
    "trigger_count": 3,
    "consecutive_failures": 0,
    "last_error": null,
    "created_at": "2026-02-03T10:00:00Z",
    "updated_at": "2026-02-24T03:31:00Z"
  }
}
```

### Error Responses (Get Reminder Detail)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `404` | Reminder not found (or belongs to another user) |
| `500` | Internal server error |

### Example cURL (Get Reminder Detail)

```bash
curl -X GET "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 3A. Get Reminder History

Get paginated execution history for a reminder. Each row represents one run attempt and includes the exact content that was sent (or attempted), whether it succeeded, and any error captured during execution.

### Endpoint (Get Reminder History)

```http
GET /{reminder_id}/history
```

### Query Parameters (Get Reminder History)

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | No | `20` | Items per page (`1-100`) |
| `page` | integer | No | `1` | Page number (`1`-indexed) |

### Success Response (200) (Get Reminder History)

```json
{
  "status": "SUCCESS",
  "message": "Reminder history retrieved successfully",
  "data": {
    "items": [
      {
        "run_id": "c6f4d0be-4a22-4f11-8a37-a14be65fb2c0",
        "attempt_number": 4,
        "status": "succeeded",
        "triggered_for_at": "2026-03-03T03:30:00Z",
        "started_at": "2026-03-03T03:30:02Z",
        "completed_at": "2026-03-03T03:30:08Z",
        "input_snapshot": "Analyze the current state of this deal.",
        "result_text": "The deal remains on track with one procurement risk to monitor.",
        "error_message": null
      }
    ],
    "page": 1,
    "limit": 20,
    "total_count": 4,
    "total_pages": 1
  }
}
```

### Notes (Get Reminder History)

- `attempt_number` increments for every execution attempt, including failed runs
- `trigger_count` on the reminder record still counts only successful runs
- Use this endpoint for frontend comparison views and past-run drilldowns

---

## 4. Update Reminder

Update any fields of a reminder. Only include fields you want to change. If the trigger time or recurrence changes, the schedule is automatically adjusted.

### Endpoint (Update Reminder)

```http
PATCH /{reminder_id}
```

### Path Parameters (Update Reminder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reminder_id` | UUID | Yes | The unique identifier of the reminder |

### Request Body (Update Reminder)

Only include fields you want to change. Send `null` explicitly to clear an optional field.

```json
{
  "agent_prompt": "Analyze this deal with focus on competitor mentions and budget concerns. Include a risk assessment.",
  "trigger_time": "2026-03-03T10:00:00+05:30",
  "recurrence": "daily"
}
```

#### Editable Fields

| Field | Type | Notes |
|-------|------|-------|
| `title` | string | Max 255 chars |
| `message_content` | string \| null | For static reminders |
| `agent_prompt` | string \| null | For agentic reminders |
| `deal_id` | UUID \| null | Send `null` to clear deal scope |
| `trigger_time` | datetime | Must be in the future |
| `timezone` | string | Valid IANA timezone |
| `recurrence` | enum | Changes schedule pattern |
| `recurrence_end_at` | datetime \| null | Send `null` to remove end date |
| `delivery_channel` | string | `email`, `teams`, or `slack` |

### Status Restrictions

Updates are only allowed when the reminder status is `active` or `paused`. Attempting to update a `completed`, `failed`, or `cancelled` reminder returns `400`.

### Auto-Reschedule

When `trigger_time` or `recurrence` changes and the reminder is `active`, the system automatically:

1. Removes the old schedule from the execution queue
2. Adds the new schedule

If the reminder is `paused`, the new time is saved but not scheduled until the user resumes.

### Success Response (200) (Update Reminder)

This endpoint returns the full `ReminderDetail` shape. The example below highlights the most relevant changed fields.

```json
{
  "status": "SUCCESS",
  "message": "Reminder updated successfully",
  "data": {
    "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Weekly deal health check",
    "reminder_type": "agentic",
    "status": "active",
    "agent_prompt": "Analyze this deal with focus on competitor mentions and budget concerns. Include a risk assessment.",
    "next_trigger_at": "2026-03-03T04:30:00Z",
    "recurrence": "daily",
    "delivery_channel": "teams",
    "updated_at": "2026-02-28T11:00:00Z"
  }
}
```

### Error Responses (Update Reminder)

| Code | Description |
|------|-------------|
| `400` | No fields to update, invalid status for editing, past trigger time, invalid timezone |
| `401` | Missing or invalid authentication token |
| `404` | Reminder not found |
| `422` | Request body validation failure |
| `500` | Internal server error |

### Example cURL (Update Reminder)

```bash
# Update the prompt
curl -X PATCH "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_prompt": "Give me a comprehensive risk analysis for this deal."
  }'

# Change delivery channel
curl -X PATCH "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "delivery_channel": "slack"
  }'

# Clear the deal scope (send explicit null)
curl -X PATCH "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "deal_id": null
  }'
```

---

## 5. Pause Reminder

Temporarily pause an active reminder. The reminder is removed from the execution queue but retains all its configuration. Use Resume to re-activate.

### Endpoint (Pause Reminder)

```http
POST /{reminder_id}/pause
```

### Path Parameters (Pause Reminder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reminder_id` | UUID | Yes | The unique identifier of the reminder |

### Allowed Transition

Only reminders with status `active` can be paused.

### Success Response (200) (Pause Reminder)

This endpoint returns the full `ReminderDetail` shape. The example below highlights the state change.

```json
{
  "status": "SUCCESS",
  "message": "Reminder paused successfully",
  "data": {
    "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Weekly deal health check",
    "status": "paused",
    "next_trigger_at": "2026-03-03T03:30:00Z",
    "recurrence": "weekly"
  }
}
```

### Error Responses (Pause Reminder)

| Code | Description |
|------|-------------|
| `400` | Reminder is not in `active` status |
| `401` | Missing or invalid authentication token |
| `404` | Reminder not found |
| `500` | Internal server error |

### Example cURL (Pause Reminder)

```bash
curl -X POST "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890/pause" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 6. Resume Reminder

Resume a previously paused reminder. The reminder is re-added to the execution queue and will fire at `next_trigger_at`.

### Endpoint (Resume Reminder)

```http
POST /{reminder_id}/resume
```

### Path Parameters (Resume Reminder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reminder_id` | UUID | Yes | The unique identifier of the reminder |

### Allowed Transition

Only reminders with status `paused` can be resumed. The `next_trigger_at` must be in the future — if it has passed while paused, update the trigger time first using PATCH.

### Success Response (200) (Resume Reminder)

This endpoint returns the full `ReminderDetail` shape. The example below highlights the state change.

```json
{
  "status": "SUCCESS",
  "message": "Reminder resumed successfully",
  "data": {
    "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Weekly deal health check",
    "status": "active",
    "next_trigger_at": "2026-03-10T03:30:00Z",
    "recurrence": "weekly"
  }
}
```

### Error Responses (Resume Reminder)

| Code | Description |
|------|-------------|
| `400` | Reminder is not in `paused` status, or trigger time is in the past |
| `401` | Missing or invalid authentication token |
| `404` | Reminder not found |
| `500` | Internal server error |

> **Frontend Note**: If resume returns a "trigger time is in the past" error, prompt the user to update the trigger time first via PATCH, then retry the resume.

### Example cURL (Resume Reminder)

```bash
curl -X POST "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890/resume" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 7. Cancel Reminder

Permanently cancel a reminder. This is a terminal state — the reminder cannot be resumed or reactivated after cancellation. The record is preserved for history.

### Endpoint (Cancel Reminder)

```http
POST /{reminder_id}/cancel
```

### Path Parameters (Cancel Reminder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reminder_id` | UUID | Yes | The unique identifier of the reminder |

### Allowed Transition

Cancellation is allowed from: `active`, `paused`, `pending_confirmation`.

Not allowed from terminal states: `completed`, `failed`, `cancelled`.
Also not allowed while a reminder is actively executing (`triggered` / `is_running=true`).

### Difference from Delete

| Action | Effect | Record Preserved? | Reversible? |
|--------|--------|-------------------|-------------|
| **Cancel** | Sets status to `cancelled`, stops execution | Yes | No |
| **Delete** | Removes record entirely | No | No |

### Success Response (200) (Cancel Reminder)

This endpoint returns the full `ReminderDetail` shape. The example below highlights the terminal state change.

```json
{
  "status": "SUCCESS",
  "message": "Reminder cancelled successfully",
  "data": {
    "reminder_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Weekly deal health check",
    "status": "cancelled",
    "trigger_count": 3
  }
}
```

### Error Responses (Cancel Reminder)

| Code | Description |
|------|-------------|
| `400` | Reminder is already in a terminal state |
| `409` | Reminder is currently executing and cannot be cancelled |
| `401` | Missing or invalid authentication token |
| `404` | Reminder not found |
| `500` | Internal server error |

### Example cURL (Cancel Reminder)

```bash
curl -X POST "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890/cancel" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 8. Delete Reminder

Permanently delete a reminder record. Unlike cancel, this removes the record entirely. Consider using cancel instead to preserve execution history.

### Endpoint (Delete Reminder)

```http
DELETE /{reminder_id}
```

### Path Parameters (Delete Reminder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reminder_id` | UUID | Yes | The unique identifier of the reminder |

### Success Response (200) (Delete Reminder)

```json
{
  "status": "SUCCESS",
  "message": "Reminder deleted successfully",
  "data": null
}
```

### Error Responses (Delete Reminder)

| Code | Description |
|------|-------------|
| `409` | Reminder is currently executing and cannot be deleted |
| `401` | Missing or invalid authentication token |
| `404` | Reminder not found |
| `500` | Internal server error |

### Example cURL (Delete Reminder)

```bash
curl -X DELETE "$BASE_URL/a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Error Codes Summary

| HTTP Code | Reason | Suggested Action |
|-----------|--------|------------------|
| 400 | Invalid status transition, validation failure | Check current status and request body |
| 409 | Reminder execution is currently in progress | Disable destructive actions until the run completes |
| 401 | Missing/invalid token | Ensure valid bearer token |
| 404 | Reminder not found or belongs to another user | Verify reminder ID |
| 422 | Request body validation failure | Fix request parameters |
| 429 | Rate limit exceeded | Respect `Retry-After` header |
| 500 | Unexpected server error | Retry with backoff; contact support |

---

## Frontend Integration Notes

### Typical Flow

1. **View Reminders**: Call `GET /` to display the reminders list
2. **Create Reminder**:
   - User clicks "New Reminder" button
   - Show form with type selector (Static / Agentic)
   - Based on type, show message field or prompt editor
   - User picks schedule (one-time or recurring) and delivery channel
   - Call `POST /` to create
3. **Edit Reminder**:
   - User clicks edit on a reminder card
   - Show pre-filled form with current values
   - Call `PATCH /{reminder_id}` with only changed fields
   - If trigger time changed, schedule auto-updates
4. **Manage Reminder State**:
   - **Pause**: Show on active recurring reminders (no point pausing one-time)
   - **Resume**: Show on paused reminders (handle past-trigger-time error gracefully)
   - **Cancel**: Show on any non-terminal reminder (confirm dialog recommended)
   - **Delete**: Show in overflow menu (confirm dialog required)

### Prompt Editor (Agentic Reminders)

For agentic reminders, the `agent_prompt` is the most important field — it determines the quality of AI analysis delivered. Consider providing:

- A multi-line text area (not a single-line input)
- Placeholder examples: "Analyze deal risks and suggest mitigations", "Summarize all email activity from the past week"
- Character count indicator
- Optional deal selector when `deal_id` is relevant

### Delivery Channel Selection

Only show channels the user has connected:

- **Teams**: Show if the user's workspace has the Teams bot installed
- **Slack**: Show if the user's workspace has the Slack bot installed
- **Email**: Always available (uses the user's account email)

If no bot channels are connected, only show Email and suggest connecting Teams/Slack.

### Reminder Cards UI

| Field | Display Suggestion |
|-------|--------------------|
| `reminder_type` | Badge: "Static" (blue) or "AI Alert" (purple) |
| `status` | Color-coded: Active (green), Paused (yellow), Completed (gray), Failed (red), Cancelled (gray) |
| `recurrence` | Icon + label: "One-time", "Daily", "Weekdays", "Weekly", "Biweekly", "Monthly" |
| `delivery_channel` | Channel icon (Teams/Slack/Email) |
| `next_trigger_at` | Display in user's timezone using the `timezone` field |
| `trigger_count` | "Fired X times" for recurring reminders |
| `last_error` | Show error banner on failed reminders |

### Timezone Handling

- Always send `trigger_time` with timezone offset in ISO 8601 (e.g., `2026-03-03T09:00:00+05:30`)
- Send the IANA `timezone` string separately (e.g., `Asia/Kolkata`) — this is used for recurring reminders to handle DST transitions correctly
- Display times in the user's timezone using the stored `timezone` field

---

## Related References

- Design Document: `docs/plans/2026-02-27-reminders-api-design.md`
- Service Architecture: `docs/Technical Documentation/service-architecture-base.md`
