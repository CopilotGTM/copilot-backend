# CRM Tasks Feature — Design Document

**Date:** 2026-03-11
**Status:** Draft
**Branch:** `feature/deal-tasks`

---

## 1. Overview

Build a complete task management system for CopilotGTM that allows users to create, assign, update, complete, and delete tasks — both at the global level (across all deals) and within individual deals. Tasks integrate with the existing CRM sync pipeline (HubSpot/Pipedrive) and the UserReminder system for scheduling notifications.

### Key Capabilities
- Create tasks with or without a deal association
- Assign/reassign tasks to org users
- Track task status (Not Started → In Progress → Completed)
- Set due dates, priorities, and reminders
- View tasks in multiple layouts: Today, Week, Month, List
- Distinguish task source: Manual, CRM-synced, AI-generated (future)
- Link reminders to tasks using the existing UserReminder infrastructure

---

## 2. Data Model

### 2.1 CRMActivity (existing — minor additions)

The `crm_activities` table already has all core fields. Additions needed:

| Field | Type | Description |
|-------|------|-------------|
| `source` | `VARCHAR(50)` | Task origin: `MANUAL`, `CRM_SYNC`, `AI_MEETING`, `AI_INSIGHT` |
| `assigned_to_user_id` | `UUID FK → public.users` | Internal user the task is assigned to (distinct from `owner_id` which is CRM owner) |

**Why `assigned_to_user_id` separate from `owner_id`?**
- `owner_id` / `owner_external_id` / `owner_name` / `owner_email` → CRM provider's concept of ownership (synced from HubSpot/Pipedrive)
- `assigned_to_user_id` → internal assignment within CopilotGTM (who should work on this task)
- For user-created tasks, `assigned_to_user_id` is the primary assignment field
- For CRM-synced tasks, both may exist (CRM owner + internal assignee)

**Source enum:**

```python
class CRMActivitySource(str, Enum):
    MANUAL = "MANUAL"           # User-created via UI/API
    CRM_SYNC = "CRM_SYNC"      # Synced from HubSpot/Pipedrive
    AI_MEETING = "AI_MEETING"   # Extracted from meeting transcript (Phase 2)
    AI_INSIGHT = "AI_INSIGHT"   # Suggested by deal analysis (Phase 2)
```

### 2.2 UserReminder (existing — add task linkage)

Add to the `user_reminders` table:

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `UUID, nullable` | FK to `crm_activities.activity_id` — links reminder to a task |

When a task with a reminder is completed or deleted, the linked UserReminder is auto-cancelled.

---

## 3. API Design

### 3.1 Endpoint Summary

All endpoints require Bearer token authentication and enforce org isolation.

| Method | Path | Description |
|--------|------|-------------|
| `GET /tasks` | List tasks (global or filtered by deal) |
| `GET /tasks/{task_id}` | Get task detail |
| `POST /tasks` | Create task (optional `deal_id`) |
| `PATCH /tasks/{task_id}` | Update task fields (including status, assignment, deal linkage) |
| `DELETE /tasks/{task_id}` | Soft delete task |

### 3.2 `GET /tasks` — List Tasks

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `deal_id` | UUID, optional | — | Filter by deal (used on deal detail page) |
| `status` | string[], optional | — | Filter by status(es): `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, `WAITING`, `DEFERRED`, `CANCELLED` |
| `priority` | string[], optional | — | Filter by priority(ies) |
| `assigned_to_user_id` | UUID, optional | — | Filter by assignee |
| `source` | string[], optional | — | Filter by source: `MANUAL`, `CRM_SYNC`, `AI_MEETING`, `AI_INSIGHT` |
| `due_from` | datetime, optional | — | Due date range start |
| `due_to` | datetime, optional | — | Due date range end |
| `is_overdue` | bool, optional | — | Filter overdue tasks (due_date < now AND status not COMPLETED/CANCELLED) |
| `has_due_date` | bool, optional | — | Filter tasks with/without due date |
| `include_completed` | bool | false | Include completed tasks |
| `sort_by` | string | `due_date` | Sort field: `due_date`, `priority`, `created_at`, `updated_at` |
| `sort_order` | string | `asc` | `asc` or `desc` |
| `page` | int | 1 | Page number |
| `limit` | int | 50 | Page size (1-100) |

**Response:** `GenericResponse[TasksPage]`

**Frontend view mapping:**
- **Today view**: `GET /tasks?due_to={end_of_today}&include_completed=false` + separate section for `has_due_date=false`
- **Week view**: `GET /tasks?due_from={week_start}&due_to={week_end}`
- **Month view**: `GET /tasks?due_from={month_start}&due_to={month_end}`
- **List view**: `GET /tasks?sort_by=due_date&sort_order=asc`
- **Deal page**: `GET /tasks?deal_id={id}`

### 3.3 `POST /tasks` — Create Task

**Request Body:**

```json
{
  "title": "Follow up with procurement team",
  "description": "Verify legal and procurement sign-off before Friday close review.",
  "deal_id": "uuid-or-null",
  "status": "NOT_STARTED",
  "priority": "HIGH",
  "task_type": "TASK",
  "due_date": "2026-03-15T17:00:00Z",
  "assigned_to_user_id": "uuid-or-null",
  "reminder": {
    "enabled": true,
    "trigger_time": "2026-03-15T09:00:00Z",
    "recurrence": "NONE"
  }
}
```

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `title` | string (max 500) | Yes | — | Task subject |
| `description` | string (max 10000) | No | — | Task body |
| `deal_id` | UUID | No | null | If provided, validates deal exists in org |
| `status` | enum | No | `NOT_STARTED` | |
| `priority` | enum | No | `MEDIUM` | |
| `task_type` | enum | No | `TASK` | `TASK` or `TODO` |
| `due_date` | datetime | No | null | |
| `assigned_to_user_id` | UUID | No | current user | Must be a user in the org |
| `reminder` | object | No | null | Nested reminder config |
| `reminder.enabled` | bool | Yes (if reminder) | — | |
| `reminder.trigger_time` | datetime | Yes (if enabled) | — | Must be in the future |
| `reminder.recurrence` | enum | No | `NONE` | NONE, DAILY, WEEKDAYS, WEEKLY, BIWEEKLY, MONTHLY |

**Behavior:**
- Sets `source = MANUAL`
- Sets `integration_id = NULL` (user-created)
- If `deal_id` provided: populates `deal_id`, `deal_external_id`, `related_object_type=DEAL`, `related_local_id`, `related_name`
- If `assigned_to_user_id` provided: resolves user, populates `owner_name`, `owner_email`
- If `reminder.enabled`: creates a `UserReminder` with `reminder_type=STATIC`, `task_id` linkage, message auto-generated from title
- Also sets `reminder_date` on the CRMActivity for display purposes

**Response:** `GenericResponse[TaskDetail]` (201)

### 3.4 `PATCH /tasks/{task_id}` — Update Task

**Request Body:** All fields optional (same as create, minus `source`).

```json
{
  "title": "Updated title",
  "status": "IN_PROGRESS",
  "deal_id": "uuid-or-null",
  "assigned_to_user_id": "uuid-or-null",
  "priority": "HIGH",
  "due_date": "2026-03-20T17:00:00Z",
  "reminder": {
    "enabled": true,
    "trigger_time": "2026-03-20T09:00:00Z",
    "recurrence": "NONE"
  }
}
```

**Special behaviors:**
- **Status → COMPLETED**: auto-sets `completed_date = now()`, auto-cancels linked reminder
- **Status away from COMPLETED**: clears `completed_date`
- **`deal_id` set to null**: removes deal association (clears deal fields + related_* fields)
- **`deal_id` changed**: validates new deal, updates all deal linkage fields
- **`assigned_to_user_id` changed**: resolves new user, updates owner fields
- **`reminder.enabled = false`**: cancels linked UserReminder, clears `reminder_date`
- **`reminder` updated**: updates or creates UserReminder, updates `reminder_date`
- Only user-created tasks (`integration_id IS NULL`) can be updated; CRM-synced tasks are read-only

**Response:** `GenericResponse[TaskDetail]`

### 3.5 `DELETE /tasks/{task_id}` — Delete Task

- Soft delete: sets `is_deleted = True`
- Auto-cancels any linked UserReminder
- Only user-created tasks can be deleted
- **Response:** `GenericResponse[TaskDetail]`

### 3.6 Response Schemas

**TaskListItem** (for list responses):

```json
{
  "task_id": "uuid",
  "title": "Confirm procurement sign-off",
  "description": "Verify legal and procurement...",
  "status": "NOT_STARTED",
  "priority": "HIGH",
  "task_type": "TASK",
  "source": "MANUAL",
  "due_date": "2026-03-15T17:00:00Z",
  "completed_date": null,
  "is_overdue": true,
  "deal_id": "uuid-or-null",
  "deal_name": "Acme Security Rollout",
  "assigned_to_user_id": "uuid",
  "assigned_to_name": "John Doe",
  "assigned_to_email": "john@company.com",
  "has_reminder": true,
  "reminder_date": "2026-03-15T09:00:00Z",
  "created_at": "2026-03-10T10:00:00Z",
  "updated_at": "2026-03-10T10:00:00Z"
}
```

**TaskDetail** (extends TaskListItem):

```json
{
  "...all TaskListItem fields",
  "reminder": {
    "reminder_id": "uuid",
    "status": "ACTIVE",
    "trigger_time": "2026-03-15T09:00:00Z",
    "recurrence": "NONE",
    "recurrence_end_at": null
  },
  "properties": {},
  "integration_id": null,
  "organisation_id": "uuid"
}
```

---

## 4. Task Assignment

### How assignment works:
1. **On create**: `assigned_to_user_id` defaults to the creating user if not specified
2. **On update**: any org member can be assigned via `PATCH /tasks/{id}` with `assigned_to_user_id`
3. **Resolution**: The service resolves `assigned_to_user_id` → user record, populates `owner_name`, `owner_email` for display
4. **Validation**: assigned user must belong to the same organisation
5. **For CRM-synced tasks**: `assigned_to_user_id` is populated from CRM owner mapping during sync; read-only via API

### Who can assign:
- Any authenticated org member can create and assign tasks to any other org member
- No role-based restrictions in V1 (all org members are peers)

---

## 5. Reminder Integration

### Creating a reminder with a task:
1. User enables reminder toggle in Create Task dialog
2. Backend creates `UserReminder` with:
   - `reminder_type = STATIC`
   - `title` = task title
   - `message_content` = "Task reminder: {title}\n{description}"
   - `task_id` = newly created activity_id
   - `status = ACTIVE` (skip PENDING_CONFIRMATION since it's from UI, not agent)
   - `next_trigger_at` = reminder trigger_time
   - `user_id` = assigned user (so THEY get the reminder)
   - `delivery_channel` = configurable (default from user preferences)
3. Existing scheduler picks it up — no new infrastructure needed

### Lifecycle sync:
- **Task completed** → cancel linked reminder
- **Task deleted** → cancel linked reminder
- **Reminder trigger_time updated** → reschedule in Redis ZSET
- **Task reassigned** → update reminder's `user_id` so new assignee gets notified

### Updating a reminder:
- `PATCH /tasks/{id}` with `reminder` object handles create/update/cancel
- No separate reminder API calls needed from the task UI

### Removing a reminder:
- `PATCH /tasks/{id}` with `reminder.enabled = false`
- Cancels the linked UserReminder

---

## 6. Source Tracking

| Source | How it's set | Editable? |
|--------|-------------|-----------|
| `MANUAL` | User creates task via UI/API | Read-only |
| `CRM_SYNC` | HubSpot/Pipedrive sync pipeline | Read-only |
| `AI_MEETING` | Agent extracts from meeting transcript (Phase 2) | Read-only |
| `AI_INSIGHT` | Agent suggests from deal analysis (Phase 2) | Read-only |

**Migration strategy for existing data:**
- Existing tasks with `integration_id IS NOT NULL` → `source = CRM_SYNC`
- Existing tasks with `integration_id IS NULL` → `source = MANUAL`

---

## 7. Frontend View Support

The API supports all four views via query parameters:

### Today View
- **Overdue section**: `GET /tasks?is_overdue=true&include_completed=false`
- **No Due Date section**: `GET /tasks?has_due_date=false&include_completed=false`
- **Show completed toggle**: `GET /tasks?due_to={end_of_today}&include_completed=true&status=COMPLETED`

### Week View
- `GET /tasks?due_from={monday}&due_to={sunday}&sort_by=due_date`
- Frontend groups by day client-side

### Month View
- `GET /tasks?due_from={month_first}&due_to={month_last}&sort_by=due_date`
- Frontend renders calendar grid client-side

### List View
- `GET /tasks?sort_by=due_date&sort_order=asc&include_completed=true`
- Supports column sorting via `sort_by` parameter

### Deal Page
- `GET /tasks?deal_id={id}`
- Shows same sections: Overdue, Active, No Due Date, Completed

---

## 8. What's NOT in Scope (Phase 2 / Future)

- AI task extraction from meetings (`source = AI_MEETING`)
- AI task suggestions from deal analysis (`source = AI_INSIGHT`)
- Task groups / task queues (grouping mechanism)
- Bulk operations (bulk complete, bulk reassign)
- Task comments / activity log
- Email/Slack delivery for reminders (currently Teams-only for proactive delivery)
- Task templates

---

## 9. Migration Plan

### New migration: `017_add_task_source_and_assignment.py`

```sql
-- Add source column with default for existing data
ALTER TABLE crm_activities ADD COLUMN source VARCHAR(50);
UPDATE crm_activities SET source = 'CRM_SYNC' WHERE integration_id IS NOT NULL;
UPDATE crm_activities SET source = 'MANUAL' WHERE integration_id IS NULL;

-- Add assigned_to_user_id
ALTER TABLE crm_activities ADD COLUMN assigned_to_user_id UUID;
-- Populate from owner_id for existing tasks
UPDATE crm_activities SET assigned_to_user_id = owner_id WHERE owner_id IS NOT NULL;

-- Add index
CREATE INDEX ix_crm_activities_assigned_to ON crm_activities (organisation_id, assigned_to_user_id);
CREATE INDEX ix_crm_activities_source ON crm_activities (organisation_id, source);

-- Add task_id to user_reminders
ALTER TABLE user_reminders ADD COLUMN task_id UUID;
CREATE INDEX ix_user_reminders_task ON user_reminders (task_id);
```
