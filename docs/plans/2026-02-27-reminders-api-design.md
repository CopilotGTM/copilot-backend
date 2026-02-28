# Reminders API Design

**Date:** 2026-02-27
**Status:** Approved

## Overview

Add frontend-facing REST API endpoints for managing user reminders. The existing `UserReminder` model, Redis ZSET scheduling, and Celery execution pipeline are reused — this is a thin API layer on top of production-ready infrastructure.

## Decisions

- **Approach:** Thin API layer over existing infrastructure (no new models, no new workers)
- **Scope:** User-level reminders at `/users/reminders` (deal-level is future work)
- **Confirmation:** API-created reminders go directly to ACTIVE (no PENDING_CONFIRMATION)
- **Channels:** Single channel per reminder (email, teams, slack)
- **Editing:** All fields editable on ACTIVE/PAUSED reminders; service handles re-scheduling
- **Stop/Resume:** New PAUSED status distinct from CANCELLED

## State Machine

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
          [Cancel from any non-terminal state] ──→ CANCELLED
          [3 consecutive failures] ──→ FAILED
```

**Status transitions:**
- `pause`: ACTIVE → PAUSED (unschedule from Redis)
- `resume`: PAUSED → ACTIVE (re-schedule in Redis)
- `cancel`: ACTIVE|PAUSED|TRIGGERED|PENDING_CONFIRMATION → CANCELLED (unschedule)
- `update`: valid from ACTIVE|PAUSED only

## API Endpoints

All under prefix `/users/reminders`:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Create reminder (ACTIVE immediately) |
| `GET` | `/` | List user's reminders (paginated, filterable) |
| `GET` | `/{reminder_id}` | Get reminder detail |
| `PATCH` | `/{reminder_id}` | Update reminder fields |
| `POST` | `/{reminder_id}/pause` | Pause recurring reminder |
| `POST` | `/{reminder_id}/resume` | Resume paused reminder |
| `POST` | `/{reminder_id}/cancel` | Cancel reminder (terminal) |
| `DELETE` | `/{reminder_id}` | Hard delete reminder |

## Schemas

### ReminderCreateRequest
```python
title: str                          # max 255
reminder_type: ReminderType         # "static" | "agentic"
message_content: str | None         # required for static
agent_prompt: str | None            # required for agentic
deal_id: UUID | None                # optional, for agentic deal-scoped
trigger_time: datetime              # ISO 8601 with timezone
timezone: str                       # IANA timezone
recurrence: ReminderRecurrence      # default "none"
recurrence_end_at: datetime | None  # optional
delivery_channel: str               # "email" | "teams" | "slack"
```

### ReminderUpdateRequest (all fields optional)
```python
title: str | None
message_content: str | None
agent_prompt: str | None
deal_id: UUID | None
trigger_time: datetime | None
timezone: str | None
recurrence: ReminderRecurrence | None
recurrence_end_at: datetime | None
delivery_channel: str | None
```

### ReminderListQueryParams
```python
limit: int = 20       # 1-100
page: int = 1
status: str | None    # comma-separated filter
reminder_type: str | None
sort: str = "desc"    # by next_trigger_at
```

### ReminderDetail (response)
```python
reminder_id: UUID
title: str
reminder_type: ReminderType
status: ReminderStatus
message_content: str | None
agent_prompt: str | None
deal_id: UUID | None
next_trigger_at: datetime
timezone: str
recurrence: ReminderRecurrence
recurrence_end_at: datetime | None
delivery_channel: str
last_triggered_at: datetime | None
trigger_count: int
consecutive_failures: int
last_error: str | None
created_at: datetime
updated_at: datetime
```

### ReminderListResponse
```python
items: list[ReminderListItem]
page: int
limit: int
total_count: int
total_pages: int
```

## Service Layer

`ReminderService(session, organisation_id, user_id)` with standard dependency pattern.

**Methods:**
- `create()` — validate type/content, create ACTIVE, schedule in Redis
- `list()` — paginated, filterable, sorted by next_trigger_at
- `get()` — fetch by ID, verify ownership
- `update()` — apply changes, reschedule if time/recurrence changed
- `pause()` — ACTIVE → PAUSED, unschedule
- `resume()` — PAUSED → ACTIVE, reschedule
- `cancel()` — non-terminal → CANCELLED, unschedule
- `delete()` — hard delete + unschedule

**Validation:**
- Static reminders require `message_content`
- Agentic reminders require `agent_prompt`
- trigger_time must be in the future
- timezone must be valid IANA timezone
- Delivery channel availability validated (future — for now accept any)

## File Structure

```
src/
├── api/routers/reminders/
│   ├── __init__.py
│   └── reminders_handler.py
├── api/schemas/
│   └── reminder_models.py
├── service/reminders/
│   ├── __init__.py
│   └── reminder_service.py
└── domain/models/organisation_tables/
    └── reminder_models.py  (existing — add PAUSED to ReminderStatus)
```

No database migration required — status column is String(20), not a PG enum.

## Reused Infrastructure

- `UserReminder` model (existing)
- `schedule_reminder()` / `unschedule_reminder()` from `src/infra/messaging/reminder_schedule.py`
- Redis ZSET `reminders:due` (existing)
- Celery `poll_due_reminders_task` + `execute_reminder_task` (existing)
- `ReminderExecutionService` (existing)
