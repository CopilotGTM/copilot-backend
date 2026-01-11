# Calendar Sync Tasks: Three-Tier Architecture

**Last Updated**: January 2026
**Status**: Active Implementation Document

## Overview

This document explains the **calendar events synchronization** implementation using a **three-tier Celery task architecture**. This pattern ensures each organization gets its own Celery task, enabling better tracking, retry capabilities, and org-level parallelism.

**Supported Providers**:
- Google Calendar
- Outlook Calendar

---

## Architecture Diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CELERY BEAT SCHEDULER                             │
│                       Triggers every 30 minutes                             │
│                   Config: src/workers/integrations_worker/config.py         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: sync_calendar_all_orgs_task                                        │
│  File: src/workers/integrations_worker/tasks/calendar/all_orgs_task.py      │
│  Task Name: integrations.sync_calendar_all_orgs                             │
│                                                                             │
│  Actions:                                                                   │
│  1. Query master DB for all APPROVED organizations                          │
│  2. For each org: dispatch sync_calendar_org_task via apply_async()         │
│  3. Return summary with task IDs                                            │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=False, max_retries=0 (coordinator doesn't retry)                    │
│  • Fire-and-forget dispatch to Tier 2                                       │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
              Dispatches one Tier 2 task per organization (async)
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        ▼                          ▼                          ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ sync_calendar_    │  │ sync_calendar_    │  │ sync_calendar_    │
│ org_task          │  │ org_task          │  │ org_task          │
│ (Org A)           │  │ (Org B)           │  │ (Org C)           │
│ task_id: abc-123  │  │ task_id: def-456  │  │ task_id: ghi-789  │
└─────────┬─────────┘  └─────────┬─────────┘  └─────────┬─────────┘
          │                      │                      │
          ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 2: sync_calendar_org_task                                             │
│  File: src/workers/integrations_worker/tasks/calendar/org_task.py           │
│  Task Name: integrations.sync_calendar_org                                  │
│                                                                             │
│  Actions:                                                                   │
│  1. Query org DB for active calendar integrations (Google/Outlook)          │
│  2. For each integration: dispatch sync_integration_data_task               │
│  3. Return org-level summary with task IDs                                  │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=True, max_retries=2, retry_delay=120s                               │
│  • soft_time_limit=600s, time_limit=660s                                    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
              Dispatches sync_integration_data_task for each integration
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        ▼                          ▼                          ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ sync_integration_ │  │ sync_integration_ │  │ sync_integration_ │
│ data_task         │  │ data_task         │  │ data_task         │
│ (Google Cal)      │  │ (Outlook Cal)     │  │ (Google Cal)      │
└─────────┬─────────┘  └─────────┬─────────┘  └─────────┬─────────┘
          │                      │                      │
          ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  DISPATCHER → CALENDAR ADAPTER → CALENDAR SYNC SERVICE                      │
│                                                                             │
│  • Fetches events from Google/Outlook via Nango proxy                       │
│  • Upserts events to calendar_events table                                  │
│  • Syncs attendees to calendar_attendees table                              │
│  • Updates sync history                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Manual Sync Flow (Tier 3)

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  FRONTEND: User clicks "Refresh" button                                     │
│  OR API: Manual sync endpoint called                                        │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 3: sync_calendar_integration_task                                     │
│  File: src/workers/integrations_worker/tasks/calendar/integration_task.py   │
│  Task Name: integrations.sync_calendar_integration                          │
│                                                                             │
│  Parameters: organisation_id, integration_id                                │
│                                                                             │
│  Actions:                                                                   │
│  1. Validate integration exists and is active                               │
│  2. Dispatch sync_integration_data_task for the specific integration        │
│  3. Return result with task ID                                              │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=True, max_retries=3, retry_delay=60s                                │
│  • soft_time_limit=300s, time_limit=360s                                    │
│  • Routes to integrations_priority queue                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Three-Tier Task Summary

| Tier | Task Name | Triggered By | Purpose |
|------|-----------|--------------|---------|
| **Tier 1** | `sync_calendar_all_orgs_task` | Celery Beat (every 30 min) | Dispatches Tier 2 tasks for each org |
| **Tier 2** | `sync_calendar_org_task` | Tier 1 or API | Syncs all calendar integrations for one org |
| **Tier 3** | `sync_calendar_integration_task` | Frontend/API (manual) | Syncs a single integration on-demand |

### Key Benefits of Three-Tier Architecture

- **Org-level tracking**: Each organization has its own Celery task ID
- **Independent retries**: Failed org syncs can retry without affecting others
- **Parallelism**: Multiple orgs sync concurrently as separate tasks
- **Monitoring**: Can track sync status per organization
- **Isolation**: One org's failure doesn't block others

---

## File Structure

```text
src/workers/integrations_worker/tasks/
├── calendar/                           # Three-tier calendar sync
│   ├── __init__.py                     # Module exports
│   ├── all_orgs_task.py                # Tier 1: Global coordinator
│   ├── org_task.py                     # Tier 2: Org-level processor
│   └── integration_task.py             # Tier 3: Manual trigger
│
├── calendar_sync_tasks.py              # DEPRECATED - see below
└── ...
```

---

## Task Definitions

### Tier 1: `sync_calendar_all_orgs_task`

```python
@app.task(
    name="integrations.sync_calendar_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def sync_calendar_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Sync calendar events for all approved organizations."""
```

### Tier 2: `sync_calendar_org_task`

```python
@app.task(
    name="integrations.sync_calendar_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
)
def sync_calendar_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: Sync all calendar integrations for one organization."""
```

### Tier 3: `sync_calendar_integration_task`

```python
@app.task(
    name="integrations.sync_calendar_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def sync_calendar_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Tier 3: Sync specific calendar integration (manual trigger)."""
```

---

## Celery Configuration

### Task Routes (`config.py`)

```python
self.task_routes = {
    # Calendar sync tasks (three-tier architecture)
    "integrations.sync_calendar_all_orgs": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
    "integrations.sync_calendar_org": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
    "integrations.sync_calendar_integration": {
        "queue": "integrations_priority",  # Priority queue for manual triggers
        "routing_key": "integrations_priority",
    },
}
```

### Beat Schedule (`config.py`)

```python
self.beat_schedule = {
    "sync-calendar-events-all-orgs": {
        "task": "integrations.sync_calendar_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/30"),  # Every 30 minutes
        "kwargs": {},
        "options": {
            "queue": "integrations",
            "routing_key": "integrations",
        },
    },
}
```

---

## Result Formats

### Tier 1 Result

```python
{
    "status": "completed",
    "started_at": "2026-01-10T10:00:00+00:00",
    "completed_at": "2026-01-10T10:00:05+00:00",
    "summary": {
        "total_orgs": 50,
        "orgs_triggered": 48,
        "orgs_skipped": 2
    },
    "org_tasks": [
        {"org_id": "uuid-1", "task_id": "celery-task-id-1", "status": "triggered"},
        {"org_id": "uuid-2", "task_id": "celery-task-id-2", "status": "triggered"},
        {"org_id": "uuid-3", "status": "failed", "error": "..."}
    ]
}
```

### Tier 2 Result

```python
{
    "status": "completed",  # or "partial", "failed"
    "organisation_id": "uuid-1",
    "started_at": "2026-01-10T10:00:06+00:00",
    "completed_at": "2026-01-10T10:00:30+00:00",
    "summary": {
        "total_integrations": 3,
        "syncs_triggered": 3,
        "syncs_failed": 0
    },
    "integration_results": [
        {
            "integration_id": "int-1",
            "integration_type": "google_calendar",
            "user_id": "user-uuid",
            "task_id": "sync-task-id-1",
            "status": "triggered"
        },
        {
            "integration_id": "int-2",
            "integration_type": "outlook_calendar",
            "task_id": "sync-task-id-2",
            "status": "triggered"
        }
    ]
}
```

### Tier 3 Result

```python
{
    "status": "success",  # or "failed"
    "organisation_id": "uuid-1",
    "integration_id": "int-1",
    "integration_type": "google_calendar",
    "started_at": "2026-01-10T10:00:00+00:00",
    "completed_at": "2026-01-10T10:00:15+00:00",
    "task_id": "sync-task-id-1"
}
```

---

## Incremental vs Full Sync

### Incremental Sync (Default)

All tiers use incremental sync by default:
- `sync_type = "INCREMENTAL"`
- `incremental = True`
- Uses `upsert_calendar_incremental()` adapter
- Skips unchanged events for efficiency

### Outlook Delta Link Support

For Outlook Calendar, true incremental sync uses Microsoft's deltaLink:

```text
First Sync:
  → Fetch all events
  → API returns deltaLink token
  → Store deltaLink in integration.connection_metadata

Subsequent Syncs:
  → Pass stored deltaLink to API
  → API returns ONLY changed events
  → Receive new deltaLink for next sync
```

---

## Usage Examples

```python
# Tier 1: Sync all organizations (usually via Beat, but can be manual)
from src.workers.integrations_worker.tasks.calendar import sync_calendar_all_orgs_task
result = sync_calendar_all_orgs_task.delay()

# Tier 2: Sync specific organization
from src.workers.integrations_worker.tasks.calendar import sync_calendar_org_task
result = sync_calendar_org_task.delay(organisation_id="your-org-uuid")

# Tier 3: Sync specific integration (manual refresh)
from src.workers.integrations_worker.tasks.calendar import sync_calendar_integration_task
result = sync_calendar_integration_task.delay(
    organisation_id="your-org-uuid",
    integration_id="your-integration-uuid"
)
```

---

## File Reference

| Component | File Path |
|-----------|-----------|
| **Tier 1 Task** | `src/workers/integrations_worker/tasks/calendar/all_orgs_task.py` |
| **Tier 2 Task** | `src/workers/integrations_worker/tasks/calendar/org_task.py` |
| **Tier 3 Task** | `src/workers/integrations_worker/tasks/calendar/integration_task.py` |
| **Worker Task** | `src/workers/integrations_worker/tasks/sync_tasks.py` |
| **Dispatcher** | `src/workers/integrations_worker/services/dispatcher.py` |
| **Calendar Adapter** | `src/workers/integrations_worker/adapters/calendar_adapter.py` |
| **Calendar Sync Service** | `src/service/integrations/calendar_sync_service.py` |
| **Celery Config** | `src/workers/integrations_worker/config.py` |

---

## Deprecated Files

### `src/workers/integrations_worker/tasks/calendar_sync_tasks.py`

| Field | Value |
|-------|-------|
| **Status** | DEPRECATED |
| **Replacement** | `src/workers/integrations_worker/tasks/calendar/` |
| **Task Name** | `integrations.sync_calendar_events` |
| **Action Required** | Delete in next major release |

**Why deprecated**:
- Uses a single monolithic task that loops through all orgs internally
- No org-level task tracking or retry capability
- Doesn't follow the three-tier architecture pattern

**Before deletion, verify**:
1. No code directly imports `sync_calendar_events_task`
2. No external systems call `integrations.sync_calendar_events`
3. Update any tests that reference the old task
4. Remove the task route from `config.py`

---

## Monitoring & Debugging

### Check Celery Tasks

```bash
# View scheduled tasks
celery -A src.workers.integrations_worker.celery_app inspect scheduled

# View active tasks
celery -A src.workers.integrations_worker.celery_app inspect active
```

### Key Log Messages

- `"Starting calendar sync for all organizations (Tier 1)"` - Tier 1 started
- `"Dispatched calendar sync for org: {uuid}"` - Tier 2 task dispatched
- `"Starting calendar sync for organization (Tier 2): {uuid}"` - Tier 2 started
- `"Dispatching calendar sync: org={} integration={}"` - Integration sync dispatched

---

## Related Documentation

- [Scheduled Sync Architecture Guide](./scheduled-sync-architecture.md) - Three-tier pattern overview
- [Data Sync Flow](./data-sync-flow.md) - General integration sync flow
- [Integration Data Sync Architecture](./Integration-data-sync-architecture.md) - System architecture
