# Deal Activities Guide

## Purpose

- Represent the product activity feed projection for CRM-side events.
- Support linking projected activities to deals, contacts, and accounts by local IDs.
- Keep source-of-truth tasks and notes in their canonical tables while projecting feed rows into `crm_activities`.

## Projection Model

**Model:** `CRMActivity`

**Location:** `src/domain/models/organisation_tables/sync/crm/activities.py`

**Table:** `crm_activities`

### Key Columns

| Concept | Column(s) | Notes |
| :--- | :--- | :--- |
| Identity | `activity_id` | Internal UUID used by API and services |
| Source | `source_type`, `source_record_id` | Points back to canonical source records such as `crm_tasks` and `crm_notes` |
| Type | `activity_type`, `activity_subtype` | Canonical type plus provider subtype |
| Display | `title`, `summary`, `preview`, `display_payload` | Feed-ready presentation fields |
| Actor | `actor_user_id`, `actor_name`, `actor_email` | Who performed or owns the activity when known |
| Timing | `occurred_at` | Feed ordering timestamp |
| Soft delete | `is_deleted` | Reflects provider deletion state |
| Metadata | `properties` | Flattened and derived metadata; provider payloads live in source mappings |

### Link Table

Object membership lives in `crm_activity_links`. Read APIs query
`crm_activity_links -> crm_activities` so the same activity can appear on a
deal, contact, and account without duplicating source data.

Supported object types include `DEAL`, `CONTACT`, and `COMPANY`.

## Tasks API (Tasks-Only View)

The Tasks API remains task-shaped and is backed by `crm_tasks`. Task rows can be
projected into `crm_activities` for timeline/feed display, but task readers and
agent tools should use task services or `CRMTask`.

**Handler:** `src/api/routers/crm/tasks_handler.py`

**Service:** `src/service/crm/tasks_access_service.py`

### Endpoints

- `GET /deals/{deal_id}/tasks`
  - Returns canonical task rows linked to the deal.
  - Optional filters: `statuses`, `priorities`
  - `task_types` is accepted for backwards compatibility, but non-task rows will not be returned.

- `GET /tasks/{task_id}`
  - Returns a single task by `task_id`
  - Does not return calls/meetings/emails/notes

### Overdue Flag

API responses compute `is_overdue` as:

- `due_date < now(UTC)` and `status != COMPLETED`

## Notes

- Notes are stored in `crm_notes` and can be projected into `crm_activities` for feed display.
- Activity-feed APIs are available for deals, contacts, and accounts at `/{object_id}/activity-feed` on their existing routers.
- `integration_id` on projected `CRMActivity` rows can be `null` for internally created records.
