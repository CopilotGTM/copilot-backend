# Deal Activities Guide

## Purpose

- Represent CRM-side activities (tasks, calls, meetings, emails, notes) in a single canonical table.
- Support linking activities to deals (and other CRM objects) while keeping provider-specific payloads.
- Replace the legacy deal task storage (`crm_deal_tasks`) with the generalized activities table (`crm_activities`).

## Canonical Model

**Model:** `CRMActivity`

**Location:** `src/domain/models/organisation_tables/sync/crm/activities.py`

**Table:** `crm_activities`

### Key Columns

| Concept | Column(s) | Notes |
| :--- | :--- | :--- |
| Identity | `activity_id`, `external_id` | `activity_id` is internal UUID; `external_id` is provider identifier |
| Deal linkage | `deal_id`, `deal_external_id` | Backwards compatible deal-focused query path |
| Generic linkage | `related_object_type`, `related_external_id`, `related_local_id` | Polymorphic link to DEAL/CONTACT/COMPANY/etc |
| Type | `activity_type`, `activity_subtype` | Canonical type plus provider subtype |
| Status/Priority | `status`, `priority` | Canonicalized across CRMs |
| Owner | `owner_external_id`, `owner_name`, `owner_email`, `owner_id` | Assignment/ownership |
| Scheduling | `due_date`, `end_date`, `completed_date`, `reminder_date` | Time fields are timezone-aware |
| Soft delete | `is_deleted` | Reflects provider deletion state |
| Metadata | `properties`, `metrics`, `data_payload` | Flattened, derived, and raw payloads |

### Backwards-Compatible Aliases

For existing code that still uses task-oriented names, `CRMActivity` exposes aliases:

- `task_id` → `activity_id`
- `task_type` → `activity_type`
- `is_deleted_remote` → `is_deleted`

## Tasks API (Tasks-Only View)

The Tasks API remains task-shaped but is backed by `crm_activities`.

**Handler:** `src/api/routers/crm/tasks_handler.py`

**Service:** `src/service/crm/tasks_access_service.py`

### Endpoints

- `GET /deals/{deal_id}/tasks`
  - Returns only task-like activities: `activity_type in {TASK, TODO}`
  - Optional filters: `statuses`, `priorities`
  - `task_types` is accepted for backwards compatibility, but non-task activity types will not be returned.

- `GET /tasks/{task_id}`
  - Returns a single task-like activity by `activity_id`
  - Does not return calls/meetings/emails/notes

### Overdue Flag

API responses compute `is_overdue` as:

- `due_date < now(UTC)` and `status != COMPLETED`

## Notes

- `integration_id` on `CRMActivity` can be `null` (user-created activities). API schemas should treat it as optional.
- If you need non-task activities, use an Activities-focused endpoint/service (not the Tasks API).
