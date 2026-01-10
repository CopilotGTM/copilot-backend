# Calendar Sync Tasks: Scheduled Synchronization Flow

**Last Updated**: January 2026
**Status**: Active Implementation Document

## Overview

This document explains the **calendar events synchronization** implementation using Celery Beat scheduled tasks. Calendar sync provides a reliable fallback mechanism for webhook-driven syncs, ensuring calendar events are captured even if webhook processing fails.

**Supported Providers**:
- Google Calendar
- Outlook Calendar

---

## Architecture Diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CELERY BEAT SCHEDULER                             │
│                   Triggers every 15 minutes (configurable)                  │
│                   Config: src/workers/integrations_worker/config.py         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  COORDINATOR TASK: sync_calendar_events_task                                │
│  File: src/workers/integrations_worker/tasks/calendar_sync_tasks.py         │
│                                                                             │
│  Two execution modes:                                                       │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ MODE 1: Scheduled (no args)                                           │  │
│  │ • Query all APPROVED organizations from master DB                     │  │
│  │ • For each org → query active calendar integrations                   │  │
│  │ • Trigger sync for each integration                                   │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ MODE 2: Manual (with org_id/integration_id)                           │  │
│  │ • Sync specific organization or integration                           │  │
│  │ • Used for frontend "Refresh" button                                  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   │ For each integration, triggers:
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  WORKER TASK: sync_integration_data_task                                    │
│  File: src/workers/integrations_worker/tasks/sync_tasks.py                  │
│                                                                             │
│  Parameters passed from calendar sync:                                      │
│  • organisation_id: UUID                                                    │
│  • integration_id: UUID                                                     │
│  • category: "calendar"                                                     │
│  • provider: "google" | "microsoft"                                         │
│  • sync_name: "events"                                                      │
│  • sync_type: "INCREMENTAL"                                                 │
│  • incremental: True                                                        │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  DISPATCHER: execute_sync()                                                 │
│  File: src/workers/integrations_worker/services/dispatcher.py               │
│                                                                             │
│  1. Load integration metadata (connection_id, last_synced_at)               │
│  2. Acquire sync lock (prevent duplicate syncs)                             │
│  3. Get appropriate fetcher from registry                                   │
│  4. Fetch events from provider via Nango proxy                              │
│  5. Route to category handler → _handle_calendar()                          │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CATEGORY HANDLER: _handle_calendar()                                       │
│  File: src/workers/integrations_worker/services/dispatcher.py               │
│                                                                             │
│  Routes to appropriate adapter based on sync_type:                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │ sync_type == "INCREMENTAL"                                              ││
│  │ → upsert_calendar_incremental()                                         ││
│  │ • Skips unchanged events (optimization)                                 ││
│  │ • Handles cancelled events via status field                             ││
│  │ • Tracks action counts for observability                                ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │ sync_type == "INITIAL" or None                                          ││
│  │ → upsert_calendar()                                                     ││
│  │ • Full sync - processes all events                                      ││
│  │ • Used for initial integration setup                                    ││
│  └─────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CALENDAR SYNC SERVICE                                                      │
│  File: src/service/integrations/calendar_sync_service.py                    │
│                                                                             │
│  Tables Modified:                                                           │
│  • calendar_events (primary event storage)                                  │
│  • calendar_event_user_states (tracks which integrations synced each event) │
│  • calendar_attendees (event attendees)                                     │
│  • integration_sync_history (sync tracking)                                 │
│  • knowledge_integrations.last_synced_at (timestamp update)                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Execution Modes

### Mode 1: Scheduled Execution (Celery Beat)

When no arguments are provided, the task runs as a **global coordinator**:

```python
# Triggered by Celery Beat every 15 minutes
sync_calendar_events_task()  # No args
```

**Flow**:
1. Query master database for all organizations with `status == APPROVED`
2. For each organization:
   - Query org database for active calendar integrations
   - Filter: `integration_type IN (GOOGLE_CALENDAR, OUTLOOK_CALENDAR)`
   - Filter: `is_active == True` AND `connection_id IS NOT NULL`
3. For each integration, trigger `sync_integration_data_task`

### Mode 2: Manual Execution (Frontend Refresh)

When called with specific IDs, syncs only the requested scope:

```python
# Sync all calendar integrations for one org
sync_calendar_events_task(organisation_id="uuid-here")

# Sync specific integration only
sync_calendar_events_task(
    organisation_id="uuid-here",
    integration_id="integration-uuid"
)
```

**Use Cases**:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

---

## Incremental vs Full Sync

### Incremental Sync (Default for Scheduled Tasks)

**Characteristics**:
- `sync_type = "INCREMENTAL"`
- `incremental = True`
- Uses `upsert_calendar_incremental()` adapter
- Optimized for regular scheduled runs

**Behavior**:
- Fetches events from provider with time-based filtering
- Skips processing unchanged events (based on change detection)
- Handles cancelled/deleted events via status field updates
- Tracks action counts (created, updated, skipped, cancelled)

**Outlook-Specific: Delta Link Support**

For Outlook Calendar, true incremental sync is achieved using Microsoft's deltaLink:

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

This significantly reduces API calls and data transfer for Outlook calendars.

### Full Sync (Initial Setup)

**Characteristics**:
- `sync_type = "INITIAL"` or `None`
- Uses `upsert_calendar()` adapter
- Processes all events without optimization

**When Used**:
- First sync after integration connection
- Webhook-triggered syncs from Nango (INITIAL type)
- Manual full re-sync if needed

---

## File Reference

| Component | File Path |
|-----------|-----------|
| **Coordinator Task** | `src/workers/integrations_worker/tasks/calendar_sync_tasks.py` |
| **Worker Task** | `src/workers/integrations_worker/tasks/sync_tasks.py` |
| **Dispatcher** | `src/workers/integrations_worker/services/dispatcher.py` |
| **Calendar Adapter** | `src/workers/integrations_worker/adapters/calendar_adapter.py` |
| **Calendar Sync Service** | `src/service/integrations/calendar_sync_service.py` |
| **Celery Beat Config** | `src/workers/integrations_worker/config.py` |
| **Sync Models** | `src/workers/integrations_worker/models/sync_models.py` |

---

## Celery Beat Schedule Configuration

```python
# src/workers/integrations_worker/config.py

"sync-calendar-events-all-orgs": {
    "task": "integrations.sync_calendar_events",
    "schedule": crontab(minute="*/15"),  # Every 15 minutes
    "kwargs": {},  # No args = sync all organizations
    "options": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
},
```

**Schedule Recommendations**:
- **Production**: Every 1-2 hours (`crontab(minute=0, hour="*/2")`)
- **Development/Testing**: Every 15 minutes (`crontab(minute="*/15")`)
- Calendar events are time-sensitive but not real-time critical

---

## Data Flow Example

### Scenario: Scheduled sync triggers for Org A with Google Calendar

```text
1. Celery Beat fires at :00 minute
   └── sync_calendar_events_task() triggered (no args)

2. Coordinator queries master DB
   └── Returns: [Org_A, Org_B, Org_C] (approved orgs)

3. For Org_A, query org DB
   └── Returns: Integration_1 (Google Calendar, active)

4. Coordinator triggers worker task:
   └── sync_integration_data_task.apply_async(
         kwargs={
           "organisation_id": "org-a-uuid",
           "integration_id": "int-1-uuid",
           "category": "calendar",
           "provider": "google",
           "sync_name": "events",
           "sync_type": "INCREMENTAL",
           "incremental": True,
         }
       )

5. Worker task executes:
   └── Dispatcher loads integration metadata
   └── Fetcher retrieves events from Google via Nango
   └── _handle_calendar() routes to incremental adapter
   └── CalendarSyncService upserts events to DB

6. Results returned:
   └── {
         "status": "success",
         "items": {
           "events": {"status": "success", "records_synced": 42}
         }
       }
```

---

## Return Value Structure

### Coordinator Task Result

```python
{
    "status": "completed",
    "organizations_processed": 15,
    "organizations_with_integrations": 12,
    "total_syncs_triggered": 24,
    "results": [
        {
            "org_id": "uuid-1",
            "integrations_count": 2,
            "syncs_triggered": 2,
            "results": [
                {
                    "integration_id": "int-1",
                    "integration_type": "google_calendar",
                    "user_id": "user-uuid",
                    "sync": {
                        "type": "events",
                        "task_id": "celery-task-id",
                        "status": "submitted",
                        "provider": "google"
                    }
                }
            ]
        },
        {
            "org_id": "uuid-2",
            "status": "failed",
            "error": "Database connection error"
        }
    ]
}
```

### Worker Task Result

```python
{
    "status": "success",  # or "partial", "failed"
    "items": {
        "events": {
            "status": "success",
            "records_synced": 42,
            "reason": None  # or "no_records", "locked"
        }
    },
    "extra": {
        # Additional metadata if any
    }
}
```

---

## Error Handling & Retry Strategy

### Coordinator Task

```python
@app.task(
    name="integrations.sync_calendar_events",
    bind=False,
    max_retries=0,  # Coordinator doesn't retry
    acks_late=True,
)
```

- **No retries**: Coordinator errors don't retry (next scheduled run handles it)
- Individual org/integration failures are logged but don't stop other syncs
- Failed orgs are included in results with error details

### Worker Task

```python
@app.task(
    name="integrations.sync_integration_data",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 60 seconds between retries
    acks_late=True,
    reject_on_worker_lost=True,
)
```

- **3 retries**: Worker retries on transient failures
- **60s delay**: Between retry attempts
- **Non-retryable errors**: `integration_not_found`, `no_fetcher`, etc. don't retry

---

## Downstream Operations

After successful calendar sync, the system can trigger downstream operations:

| Trigger Condition | Downstream Task |
|-------------------|-----------------|
| Initial calendar sync (`sync_type == "INITIAL"`) | Deal-meeting matching (`process_deal_meetings_task`) |

```python
# From dispatcher.py - triggered after initial sync
if category == "calendar" and sync_name == "events":
    process_deal_meetings_task.apply_async(args=[org_id_str])
```

**Note**: Downstream tasks are only triggered for INITIAL sync, not incremental.

---

## Monitoring & Debugging

### Check Sync Status

```python
# View recent sync history in database
SELECT * FROM integration_sync_history
WHERE integration_id = 'uuid'
ORDER BY started_at DESC
LIMIT 10;
```

### Celery Task Monitoring

```bash
# View scheduled tasks
celery -A src.workers.integrations_worker.celery_app inspect scheduled

# View active tasks
celery -A src.workers.integrations_worker.celery_app inspect active
```

### Logging

Key log messages to search for:
- `"Starting scheduled calendar events sync for all organizations"` - Coordinator started
- `"Calendar sync triggered: org=%s integration=%s"` - Worker task submitted
- `"Calendar sync completed for data_type=%s"` - Individual sync finished
- `"sync_calendar_events started"` - CalendarSyncService processing

---

## Related Documentation

- [Scheduled Sync Architecture Guide](./scheduled-sync-architecture.md) - Architecture decisions and three-tier task structure
- [Data Sync Flow](./data-sync-flow.md) - General integration sync flow
- [Integration Data Sync Architecture](./Integration-data-sync-architecture.md) - System architecture

---

## Summary

The calendar sync task system provides:

1. **Reliability**: Scheduled sync catches any events missed by webhooks
2. **Flexibility**: Can be triggered manually for specific orgs/integrations
3. **Efficiency**: Incremental sync with Outlook deltaLink support
4. **Observability**: Comprehensive result tracking and logging
5. **Scalability**: Processes all organizations in parallel via async task submission
