# Calendar Events Scheduled Sync - Design Plan

## Status: PLANNING

## Problem Statement

Calendar event syncing currently relies on Nango webhooks. When a webhook is received, we immediately return 200 and process the sync asynchronously. If the sync task fails after all retries, the data is not recorded, but Nango believes the sync was successful.

**The Solution:** Implement scheduled sync tasks (like Teams and Deal Channel Messages) that run periodically to:

1. Catch any events missed due to webhook/task failures
2. Provide reliability through controlled, retryable syncs
3. Maintain real-time sync via webhooks while having scheduled fallback

## Scope

This plan covers **Google Calendar** and **Outlook Calendar** event synchronization:

| Integration Type | Provider | Provider Config Key | Nango Connect Key |
|-----------------|----------|---------------------|-------------------|
| `GOOGLE_CALENDAR` | `google` | `google-calendar` | `google-calendar-getting-started` |
| `OUTLOOK_CALENDAR` | `outlook` | `outlook-bflm` | `outlook-bflm` |

Both use:

- **Category**: `calendar`
- **Sync Name**: `events`

## Existing Architecture

### Current Data Flow (Webhook-Driven)

```
Nango → webhook (sync.success) → Return 200 immediately
                                        ↓
                              Enqueue sync_integration_data_task
                                        ↓
                              Dispatcher fetches via Nango Proxy
                                        ↓
                              CalendarSyncService stores events
                                        ↓
                              If fails after 3 retries → Data lost
```

### Proposed Data Flow (Hybrid)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         REAL-TIME PATH (Webhooks)                            │
│  Nango webhook → sync_integration_data_task → Store events                   │
│  (Immediate, but fire-and-forget acknowledgment)                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    +
┌─────────────────────────────────────────────────────────────────────────────┐
│                      SCHEDULED PATH (Fallback/Catch-up)                      │
│  Celery Beat (every 3h) → calendar_sync_task → Fetch & store events         │
│  (Catches anything missed, idempotent upserts)                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Existing Components to Leverage

### 1. Calendar Providers

**File**: `src/workers/integrations_worker/providers/calendar/`

| Provider | File | Function | Time Window |
|----------|------|----------|-------------|
| Google | `google_calendar.py` | `fetch_events()` | 90 days past → 365 days future |
| Outlook | `outlook_calendar.py` | `fetch_events()` | 90 days past → 365 days future |

Both providers:

- Use Nango Proxy for authenticated API calls
- Handle incremental sync via `last_synced_at` parameter
- Apply 1-day backoff buffer for incremental fetches

### 2. Data Storage

**File**: `src/domain/models/organisation_tables/sync/calendar.py`

| Table | Purpose |
|-------|---------|
| `calendar_events` | Core event storage (title, time, attendees, conference link) |
| `calendar_attendees` | Individual attendee records per event |
| `calendar_event_user_state` | Tracks which integrations synced each event |
| `calendar_event_deal_associations` | Links events to deals (for meeting prep) |

**Key Fields on `calendar_events`**:

- `external_id`: Provider's event ID (unique per org)
- `synced_at`: When last synced
- `source_modified_at`: Provider's modification timestamp
- `status`: null/active/cancelled

### 3. Sync Service

**File**: `src/service/integrations/calendar_sync_service.py`

| Method | Purpose |
|--------|---------|
| `sync_calendar_events()` | Full sync - processes all events |
| `sync_calendar_events_incremental()` | Optimized sync - skips unchanged events |
| `_has_meaningful_event_changes()` | Change detection to avoid unnecessary writes |
| `_create_sync_history()` | Creates `IntegrationSyncHistory` record at sync start |
| `_complete_sync_history()` | Updates sync history with status and counts on completion |

### 4. Integration Sync History (Audit Trail)

**File**: `src/domain/models/organisation_tables/sync/integration_models.py`

**Table**: `integration_sync_history`

| Column | Type | Purpose |
|--------|------|---------|
| `sync_id` | UUID | Primary key |
| `integration_id` | UUID | FK to `knowledge_integrations` |
| `started_at` | datetime | When sync started |
| `completed_at` | datetime | When sync completed (null if in progress) |
| `status` | SyncStatus | QUEUED, IN_PROGRESS, SUCCESS, FAILED, PARTIAL |
| `records_processed` | int | Count of records successfully processed |
| `records_failed` | int | Count of records that failed |
| `error_details` | JSONB | Error information if sync failed |
| `sync_metadata` | JSONB | Additional metadata (sync_type, provider, etc.) |

**Indexes**:

- `ix_sync_history_integration_started`: For querying sync history by integration
- `ix_sync_history_status`: For filtering by status

**How It's Updated**:

The `CalendarSyncService` automatically manages sync history:

```python
# At sync start (in sync_calendar_events / sync_calendar_events_incremental)
sync_history = await self._create_sync_history(integration.integration_id)
# Creates record with status=QUEUED, started_at=now()

# On successful completion
await self._complete_sync_history(
    sync_history,
    status=SyncStatus.SUCCESS,
    records_processed=created_count + updated_count,
    records_failed=failed_count,
    metadata={"sync_type": "INCREMENTAL", "provider": provider, ...}
)

# On failure
await self._complete_sync_history(
    sync_history,
    status=SyncStatus.FAILED,
    records_processed=0,
    records_failed=0,
    error_details={"error": str(e), "traceback": ...}
)
```

**Data Flow with Sync History**:

```
sync_calendar_events_task (Scheduled)
    ↓
sync_integration_data_task.apply_async()
    ↓
Dispatcher.execute_sync()
    ↓
_handle_calendar()
    ↓
upsert_calendar_incremental()
    ↓
CalendarSyncService.sync_calendar_events_incremental()
    ├── _create_sync_history() ← INSERT into integration_sync_history
    ├── Process events...
    └── _complete_sync_history() ← UPDATE integration_sync_history
```

**No Additional Work Required**: The sync history is automatically tracked by `CalendarSyncService`. The scheduled task reuses the same service layer, so sync history will be recorded for both webhook-driven and scheduled syncs.

### 5. Dispatcher Handler

**File**: `src/workers/integrations_worker/services/dispatcher.py`

The `_handle_calendar()` function (lines 662-711) routes to:

- `sync_calendar_events_incremental()` for `sync_type == "INCREMENTAL"`
- `sync_calendar_events()` for full sync

### 6. Provider Registry

**File**: `src/workers/integrations_worker/providers/registry.py`

```python
_REGISTRY[("calendar", "google")] = {"events": google_calendar_fetch}
_REGISTRY[("calendar", "outlook")] = {"events": outlook_calendar_fetch}
```

### 7. Integration Model

**File**: `src/domain/models/organisation_tables/sync/integration_models.py`

Key fields on `KnowledgeIntegration`:

- `integration_type`: `GOOGLE_CALENDAR` or `OUTLOOK_CALENDAR`
- `connection_id`: Nango connection identifier
- `is_active`: Whether integration is enabled
- `last_synced_at`: Timestamp of last successful sync
- `last_sync_status`: SUCCESS/FAILED
- `auto_sync_enabled`: Whether scheduled syncs should run

## Implementation Plan

### File Structure Decision

**Recommendation: Single file** (following existing patterns)

```
src/workers/integrations_worker/tasks/
├── sync_tasks.py                    # Webhook-driven (existing)
├── teams_sync_tasks.py              # Scheduled Teams sync (existing)
├── deal_channel_messages_tasks.py   # Scheduled deal messages (existing)
└── calendar_sync_tasks.py           # NEW: Scheduled calendar sync
```

**Rationale**:

- Consistent with existing patterns (`teams_sync_tasks.py` handles multiple sync types in one file)
- Both Google and Outlook follow identical logic, just different integration types
- Single file is easier to maintain and understand

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Celery Beat (every 3h)                             │
│                                                                              │
│  Schedule: crontab(minute=0, hour="*/3")                                     │
│  Task: integrations.sync_calendar_events                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     sync_calendar_events_task()                              │
│                                                                              │
│  Parameters:                                                                 │
│    - organisation_id: str | None  (None = all orgs)                          │
│    - integration_id: str | None   (None = all integrations in org)           │
│                                                                              │
│  Logic:                                                                      │
│    1. If no org_id → _sync_all_orgs()                                        │
│    2. If org_id → _sync_organization(org_id, integration_id)                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           _sync_all_orgs()                                   │
│                                                                              │
│  1. Query master DB for APPROVED organizations                               │
│  2. For each org:                                                            │
│     - Call _sync_organization(org_id, None)                                  │
│     - Catch errors per-org (don't stop on failure)                           │
│  3. Return summary with results                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    _sync_organization(org_id, integration_id)                │
│                                                                              │
│  1. Query org DB for active calendar integrations:                           │
│     - IntegrationType IN (GOOGLE_CALENDAR, OUTLOOK_CALENDAR)                 │
│     - is_active = True                                                       │
│     - connection_id IS NOT NULL                                              │
│     - (Optional) auto_sync_enabled = True                                    │
│                                                                              │
│  2. For each integration:                                                    │
│     - Determine provider: 'google' or 'outlook'                              │
│     - Determine provider_config_key from mapping                             │
│     - Enqueue sync_integration_data_task with:                               │
│       - category: "calendar"                                                 │
│       - provider: "google" | "outlook"                                       │
│       - sync_name: "events"                                                  │
│       - sync_type: "INCREMENTAL"                                             │
│                                                                              │
│  3. Return per-org results                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    sync_integration_data_task (existing)                     │
│                                                                              │
│  Dispatcher → Fetch via Provider → CalendarSyncService → Store              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Phase 1: Create Task File

**File**: `src/workers/integrations_worker/tasks/calendar_sync_tasks.py`

```python
"""
Calendar-specific synchronization tasks.

Handles automatic and on-demand calendar event synchronization:
- Google Calendar events sync
- Outlook Calendar events sync
- Scheduled sync for all organizations (via Celery Beat every 3 hours)
- Manual sync for specific integration (triggered from frontend)

This provides a fallback mechanism for webhook-driven syncs, ensuring
events are captured even if webhook processing fails.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

logger = get_logger(__name__)

# Calendar integration types to sync
CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


@app.task(
    name="integrations.sync_calendar_events",
    bind=False,
    max_retries=0,  # Coordinator doesn't retry; workers retry independently
    acks_late=True,
)
def sync_calendar_events_task(
    organisation_id: str | None = None,
    integration_id: str | None = None,
) -> dict[str, Any]:
    """
    Sync calendar events for all organizations or specific org/integration.

    This task syncs events from:
    - Google Calendar
    - Outlook Calendar

    The task can be:
    1. Scheduled via Celery Beat to run every 3 hours for all orgs (no args)
    2. Manually triggered from frontend (refresh button) for specific integration
    3. Triggered via API endpoint for specific org

    Args:
        organisation_id: Optional - sync only this organization
        integration_id: Optional - sync only this specific integration (requires organisation_id)

    Returns:
        Dictionary with sync summary and results
    """
    # ... implementation follows teams_sync_tasks.py pattern
```

### Phase 2: Implementation Details

#### 2.1 Query Active Calendar Integrations

```python
async def _get_calendar_integrations(
    org_session: AsyncSession,
    specific_integration_id: UUID | None = None,
) -> list[KnowledgeIntegration]:
    """Get active calendar integrations for an organization."""

    stmt = select(KnowledgeIntegration).where(
        KnowledgeIntegration.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
        KnowledgeIntegration.is_active == True,  # noqa: E712
        KnowledgeIntegration.connection_id.isnot(None),
    )

    # Filter to specific integration if provided
    if specific_integration_id:
        stmt = stmt.where(KnowledgeIntegration.integration_id == specific_integration_id)

    result = await org_session.execute(stmt)
    return list(result.scalars().all())
```

#### 2.2 Determine Provider Details

```python
def _get_provider_details(integration: KnowledgeIntegration) -> dict[str, str]:
    """Get provider category and config key for an integration."""

    category, provider = categorize_integration_type(integration.integration_type)
    provider_config_key = to_nango_provider_key(integration.integration_type)

    return {
        "category": category,           # "calendar"
        "provider": provider,           # "google" or "outlook"
        "provider_config_key": provider_config_key,  # "google-calendar" or "outlook-bflm"
    }
```

#### 2.3 Trigger Sync Task

```python
async def _trigger_events_sync(
    org_id: UUID,
    integration: KnowledgeIntegration,
) -> dict[str, Any]:
    """Trigger calendar events sync for a single integration."""

    provider_details = _get_provider_details(integration)

    try:
        task = sync_integration_data_task.apply_async(
            kwargs={
                "organisation_id": str(org_id),
                "integration_id": str(integration.integration_id),
                "category": provider_details["category"],
                "provider": provider_details["provider"],
                "provider_config_key": provider_details["provider_config_key"],
                "sync_name": "events",
                "sync_type": "INCREMENTAL",
                "sync_variant": "base",
                "incremental": True,
            },
            headers=correlation_headers(),
        )

        return {
            "type": "events",
            "task_id": task.id,
            "status": "submitted",
            "provider": provider_details["provider"],
        }
    except Exception as e:
        logger.error(
            "Failed to trigger calendar sync: org=%s integration=%s error=%s",
            org_id,
            integration.integration_id,
            str(e),
            exc_info=True,
        )
        return {
            "type": "events",
            "status": "failed",
            "provider": provider_details["provider"],
            "error": str(e),
        }
```

### Phase 3: Config Updates

**File**: `src/workers/integrations_worker/config.py`

#### 3.1 Add Task Route

```python
self.task_routes = {
    # ... existing routes ...

    # Calendar events periodic sync
    "integrations.sync_calendar_events": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
}
```

#### 3.2 Add Beat Schedule

```python
self.beat_schedule = {
    # ... existing schedules ...

    "sync-calendar-events-all-orgs": {
        "task": "integrations.sync_calendar_events",
        "schedule": crontab(minute=0, hour="*/3"),  # Every 3 hours at :00
        "kwargs": {},  # No args = sync all organizations
        "options": {
            "queue": "integrations",
            "routing_key": "integrations",
        },
    },
}
```

### Phase 4: Task Registration

Ensure the new task file is discovered by Celery.

**File**: `src/workers/integrations_worker/tasks/__init__.py`

```python
# Import all task modules for Celery auto-discovery
from src.workers.integrations_worker.tasks.calendar_sync_tasks import *  # noqa: F401, F403
from src.workers.integrations_worker.tasks.deal_channel_messages_tasks import *  # noqa: F401, F403
from src.workers.integrations_worker.tasks.sync_tasks import *  # noqa: F401, F403
from src.workers.integrations_worker.tasks.teams_sync_tasks import *  # noqa: F401, F403
from src.workers.integrations_worker.tasks.token_refresh import *  # noqa: F401, F403
```

## Idempotency Considerations

The system already handles idempotency through:

1. **Unique Constraint**: `calendar_events` has `UNIQUE(organisation_id, external_id)`
2. **Upsert Logic**: `CalendarSyncService._upsert_event()` uses `ON CONFLICT DO UPDATE`
3. **Change Detection**: `_has_meaningful_event_changes()` skips unchanged events
4. **Sync Metadata**: Tracks `ADDED/UPDATED/DELETED/UNCHANGED` actions

**No additional idempotency work required** - the existing sync service handles this.

## Deduplication with Webhook Sync

The scheduled task and webhook-driven task both call `sync_integration_data_task`, which:

1. Uses the same dispatcher and sync service
2. Applies the same upsert logic
3. Updates `last_synced_at` on completion

**Existing Redis lock** (`sync:lock:{integration_id}:{data_type}`) prevents concurrent syncs for the same integration.

## Time Windows

| Sync Type | Past Window | Future Window |
|-----------|-------------|---------------|
| Initial | 90 days | 365 days |
| Incremental | last_synced_at - 1 day buffer | 365 days |

This is already implemented in the calendar providers.

## Error Handling

### Per-Organization Errors

```python
for org_id in org_ids:
    try:
        org_result = await _sync_organization(org_id, None)
        results.append(org_result)
    except Exception as e:
        logger.error("Failed to process organization %s: %s", org_id, str(e))
        results.append({
            "org_id": str(org_id),
            "status": "failed",
            "error": str(e),
        })
        # Continue to next org - don't stop entire job
```

### Per-Integration Errors

The `sync_integration_data_task` has its own retry logic:

- **Max Retries**: 3
- **Retry Delay**: 60 seconds base, exponential backoff
- **Non-Retryable**: `integration_not_found`, `no_fetcher`, `unsupported_category`

## Logging

```python
# Start of scheduled job
logger.info("Starting scheduled calendar events sync for all organizations")

# Per-org processing
logger.info("Processing calendar events for organization: %s", org_id)
logger.info("Found %d active calendar integration(s) for org: %s", len(integrations), org_id)

# Per-integration sync
logger.info(
    "Calendar sync triggered: org=%s integration=%s provider=%s task=%s",
    org_id,
    integration.integration_id,
    provider,
    task.id,
)

# Completion
logger.info(
    "Completed calendar sync: orgs=%d integrations=%d syncs=%d",
    len(org_ids),
    total_integrations,
    total_syncs,
)
```

## Return Value Structure

```python
{
    "status": "completed",
    "organizations_processed": 10,
    "organizations_with_integrations": 7,
    "total_syncs_triggered": 15,  # Both Google and Outlook
    "results": [
        {
            "org_id": "uuid-1",
            "integrations_count": 3,
            "syncs_triggered": 3,
            "results": [
                {
                    "integration_id": "uuid-a",
                    "integration_type": "google_calendar",
                    "user_id": "uuid-user",
                    "sync": {
                        "type": "events",
                        "task_id": "celery-task-id",
                        "status": "submitted",
                        "provider": "google",
                    },
                },
                # ... more integrations
            ],
        },
        # ... more orgs
    ],
}
```

## Testing Plan

### 1. Unit Tests

**File**: `tests/workers/integrations_worker/tasks/test_calendar_sync_tasks.py`

- Test `_get_calendar_integrations()` returns correct types
- Test `_get_provider_details()` mapping
- Test `_sync_organization()` with mocked integrations
- Test `_sync_all_orgs()` with mocked organizations

### 2. Integration Tests

- Test full flow with test Google Calendar integration
- Test full flow with test Outlook Calendar integration
- Test idempotency (run twice, verify no duplicates)
- Test error handling (invalid connection, token expired)

### 3. Manual Testing

```bash
# Trigger for all orgs
python -c "
from src.workers.integrations_worker.tasks.calendar_sync_tasks import sync_calendar_events_task
result = sync_calendar_events_task.delay()
print(result.get())
"

# Trigger for specific org
python -c "
from src.workers.integrations_worker.tasks.calendar_sync_tasks import sync_calendar_events_task
result = sync_calendar_events_task.delay(organisation_id='your-org-uuid')
print(result.get())
"

# Trigger for specific integration
python -c "
from src.workers.integrations_worker.tasks.calendar_sync_tasks import sync_calendar_events_task
result = sync_calendar_events_task.delay(
    organisation_id='your-org-uuid',
    integration_id='your-integration-uuid'
)
print(result.get())
"
```

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/workers/integrations_worker/tasks/calendar_sync_tasks.py` | **CREATE** | New scheduled task |
| `src/workers/integrations_worker/tasks/__init__.py` | MODIFY | Import new task |
| `src/workers/integrations_worker/config.py` | MODIFY | Add route + beat schedule |
| `tests/workers/integrations_worker/tasks/test_calendar_sync_tasks.py` | CREATE | Unit tests |

## Schedule Recommendation

| Task | Current Schedule | Recommendation |
|------|------------------|----------------|
| Teams Channels + Chats | Every 3 hours | Keep |
| Deal Channel Messages | Every hour at :30 | Keep |
| **Calendar Events** | N/A (new) | **Every 3 hours at :00** |

**Reasoning**:

- Calendar events change less frequently than chat messages
- 3-hour interval balances freshness with API quota
- Aligns with Teams sync schedule for operational simplicity
- Webhooks still provide real-time updates

## Dependencies

- Celery Beat running (`integrations-beat` service)
- Redis available for task queue
- Active calendar integrations in database
- Valid Nango connections for integrations

## Post-Implementation Verification

1. Check Celery Beat logs for scheduled task execution
2. Verify `calendar_events` table is being updated
3. Monitor for errors in worker logs
4. Confirm `last_synced_at` is updating on integrations
5. Test manual trigger from frontend (if applicable)

## Acceptance Criteria

- [ ] Task triggers every 3 hours for all approved organizations
- [ ] Syncs both Google Calendar and Outlook Calendar integrations
- [ ] Uses existing `sync_integration_data_task` for actual sync work
- [ ] Handles per-org errors without stopping entire job
- [ ] Logs include integration type and provider details
- [ ] Returns structured summary with sync counts
- [ ] Manual trigger works with org_id and integration_id parameters
- [ ] Idempotent - running twice doesn't create duplicate events
- [ ] Works alongside webhook-driven syncs (hybrid model)

## Future Enhancements

1. **Calendar Sync (Calendar List)**: Currently only syncing events. Could add sync for calendar list (`sync_name: "calendars"`) if needed.

2. **Selective Sync**: Add `auto_sync_enabled` check to skip integrations where user disabled auto-sync.

3. **Sync History Tracking**: Create `IntegrationSyncHistory` records for audit trail.

4. **Rate Limit Awareness**: If hitting Google/Microsoft API limits, implement per-provider throttling.

## Related Documents

- [deal-channel-messages-sync-plan.md](./deal-channel-messages-sync-plan.md) - Similar scheduled sync pattern
- [Integration-data-sync-architecture.md](../Technical%20Documentation/integration/Integration-data-sync-architecture.md) - Overall sync architecture
- [data-sync-flow.md](../Technical%20Documentation/integration/data-sync-flow.md) - Data flow documentation
