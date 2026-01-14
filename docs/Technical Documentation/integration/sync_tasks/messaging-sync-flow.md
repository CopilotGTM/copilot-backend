# Messaging Sync Tasks: Three-Tier Architecture

**Last Updated**: January 2026
**Status**: Active Implementation Document

## Overview

This document explains the **messaging synchronization** implementation using a **three-tier Celery task architecture**. This pattern ensures each organization gets its own Celery task, enabling better tracking, retry capabilities, and org-level parallelism.

**Supported Providers**:

- Microsoft Teams (channels and chats)
- (Future) Slack

**Sync Types**:

- Channels/Chats sync: Team channels and group/meeting chats
- Deal Messages sync: Messages from deal-linked channels

---

## Architecture Diagram

### Channels/Chats Sync Flow

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CELERY BEAT SCHEDULER                             │
│                       Triggers every 3 hours                                │
│                   Config: src/workers/integrations_worker/config.py         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: sync_messaging_channels_all_orgs_task                              │
│  File: src/workers/integrations_worker/tasks/messaging/channels_all_orgs_task.py
│  Task Name: integrations.sync_messaging_channels_all_orgs                   │
│                                                                             │
│  Actions:                                                                   │
│  1. Query master DB for all APPROVED organizations                          │
│  2. For each org: dispatch sync_messaging_channels_org_task                 │
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
│ sync_messaging_   │  │ sync_messaging_   │  │ sync_messaging_   │
│ channels_org_task │  │ channels_org_task │  │ channels_org_task │
│ (Org A)           │  │ (Org B)           │  │ (Org C)           │
│ task_id: abc-123  │  │ task_id: def-456  │  │ task_id: ghi-789  │
└─────────┬─────────┘  └─────────┬─────────┘  └─────────┬─────────┘
          │                      │                      │
          ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 2: sync_messaging_channels_org_task                                   │
│  File: src/workers/integrations_worker/tasks/messaging/channels_org_task.py │
│  Task Name: integrations.sync_messaging_channels_org                        │
│                                                                             │
│  Actions:                                                                   │
│  1. Query org DB for active messaging integrations (Teams)                  │
│  2. For each integration: dispatch sync_integration_data_task               │
│     - Once for "channels"                                                   │
│     - Once for "chats"                                                      │
│  3. Return org-level summary with task IDs                                  │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=True, max_retries=2, retry_delay=120s                               │
│  • soft_time_limit=900s, time_limit=960s (15+ min for many channels)        │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
              Dispatches sync_integration_data_task for channels AND chats
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        ▼                          ▼                          ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ sync_integration_ │  │ sync_integration_ │  │ sync_integration_ │
│ data_task         │  │ data_task         │  │ data_task         │
│ (channels)        │  │ (chats)           │  │ (channels)        │
└─────────┬─────────┘  └─────────┬─────────┘  └─────────┬─────────┘
          │                      │                      │
          ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  DISPATCHER → MESSAGING ADAPTER → TEAMS PROVIDER                            │
│                                                                             │
│  • Fetches channels/chats from Teams via Nango proxy                        │
│  • Upserts to messaging_channels table                                      │
│  • Updates sync history                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Deal Messages Sync Flow

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CELERY BEAT SCHEDULER                             │
│                       Triggers every hour at :30                            │
│                   Config: src/workers/integrations_worker/config.py         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: sync_deal_messages_all_orgs_task                                   │
│  File: src/workers/integrations_worker/tasks/messaging/                     │
│        deal_messages_all_orgs_task.py                                       │
│  Task Name: integrations.sync_deal_messages_all_orgs                        │
│                                                                             │
│  Actions:                                                                   │
│  1. Query master DB for all APPROVED organizations                          │
│  2. For each org: dispatch sync_deal_messages_org_task via apply_async()    │
│  3. Return summary with task IDs                                            │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
              Dispatches one Tier 2 task per organization (async)
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        ▼                          ▼                          ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ sync_deal_        │  │ sync_deal_        │  │ sync_deal_        │
│ messages_org_task │  │ messages_org_task │  │ messages_org_task │
│ (Org A)           │  │ (Org B)           │  │ (Org C)           │
└─────────┬─────────┘  └─────────┬─────────┘  └─────────┬─────────┘
          │                      │                      │
          ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 2: sync_deal_messages_org_task                                        │
│  File: src/workers/integrations_worker/tasks/messaging/                     │
│        deal_messages_org_task.py                                            │
│  Task Name: integrations.sync_deal_messages_org                             │
│                                                                             │
│  Actions:                                                                   │
│  1. Call DealChannelMessagesService.sync_all_deal_channels()                │
│  2. Service queries MessagingDealLink for deal-linked channels              │
│  3. Groups by external_channel_id to avoid duplicate fetches                │
│  4. Randomly selects integration for load distribution                      │
│  5. Fetches messages via Teams Graph API (Nango proxy)                      │
│  6. Stores in messaging_messages table                                      │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=True, max_retries=3, retry_delay=60s (exponential backoff)          │
│  • soft_time_limit=600s, time_limit=660s                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Channels/Chats Manual Sync Flow (Tier 3)

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  FRONTEND: User clicks "Refresh" button                                     │
│  OR API: Manual sync endpoint called                                        │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 3: sync_messaging_channels_integration_task                           │
│  File: src/workers/integrations_worker/tasks/messaging/channels_integration_task.py
│  Task Name: integrations.sync_messaging_channels_integration                │
│                                                                             │
│  Parameters: organisation_id, integration_id                                │
│                                                                             │
│  Actions:                                                                   │
│  1. Validate integration exists and is active                               │
│  2. Dispatch sync_integration_data_task for channels                        │
│  3. Dispatch sync_integration_data_task for chats                           │
│  4. Return result with task IDs                                             │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=True, max_retries=3, retry_delay=60s                                │
│  • soft_time_limit=300s, time_limit=360s                                    │
│  • Routes to integrations_priority queue                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Three-Tier Task Summary

### Sync Flow Overview

```
AUTOMATIC (Celery Beat):
    Tier 1 (all_orgs) → Tier 2 (org)

MANUAL (Frontend refresh button):
    Tier 3 (integration) directly
```

### Channels/Chats Sync Tasks

| Tier | Task Name | Triggered By | Purpose |
|------|-----------|--------------|---------|
| **Tier 1** | `sync_messaging_channels_all_orgs_task` | Celery Beat (every 3h) | Dispatches Tier 2 for each org |
| **Tier 2** | `sync_messaging_channels_org_task` | Tier 1 | Syncs all integrations for one org |
| **Tier 3** | `sync_messaging_channels_integration_task` | Frontend (manual) | Syncs a single integration on-demand |

### Deal Messages Sync Tasks

| Tier | Task Name | Triggered By | Purpose |
|------|-----------|--------------|---------|
| **Tier 1** | `sync_deal_messages_all_orgs_task` | Celery Beat (every 3h) | Dispatches Tier 2 for each org |
| **Tier 2** | `sync_deal_messages_org_task` | Tier 1 | Syncs deal-linked messages for one org |
| **Tier 3** | `sync_deal_messages_integration_task` | Frontend (manual) | Syncs deal messages for one integration |

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
├── messaging/                              # Three-tier messaging sync
│   ├── __init__.py                         # Module exports
│   ├── channels_all_orgs_task.py           # Tier 1: Global coordinator (channels/chats)
│   ├── channels_org_task.py                # Tier 2: Org-level processor (channels/chats)
│   ├── channels_integration_task.py        # Tier 3: Manual trigger (channels/chats)
│   ├── deal_messages_all_orgs_task.py      # Tier 1: Deal messages coordinator
│   ├── deal_messages_org_task.py           # Tier 2: Deal messages per org
│   └── deal_messages_integration_task.py   # Tier 3: Manual trigger (deal messages)
│
├── teams_sync_tasks.py                     # DEPRECATED - see below
├── deal_channel_messages_tasks.py          # DEPRECATED - see below
└── ...
```

---

## Task Definitions

### Tier 1: `sync_messaging_channels_all_orgs_task`

```python
@app.task(
    name="integrations.sync_messaging_channels_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def sync_messaging_channels_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Sync messaging channels/chats for all approved organizations."""
```

### Tier 2: `sync_messaging_channels_org_task`

```python
@app.task(
    name="integrations.sync_messaging_channels_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
    soft_time_limit=900,
    time_limit=960,
)
def sync_messaging_channels_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: Sync all messaging integrations for one organization."""
```

### Tier 3: `sync_messaging_channels_integration_task`

```python
@app.task(
    name="integrations.sync_messaging_channels_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def sync_messaging_channels_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Tier 3: Sync specific messaging integration (manual trigger)."""
```

### Deal Messages Tasks

```python
@app.task(
    name="integrations.sync_deal_messages_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def sync_deal_messages_all_orgs_task() -> dict[str, Any]:
    """Tier 1: Trigger deal message sync for all organizations."""

@app.task(
    name="integrations.sync_deal_messages_org",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=600,
    time_limit=660,
)
def sync_deal_messages_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Tier 2: Sync deal-linked channel messages for one org."""

@app.task(
    name="integrations.sync_deal_messages_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def sync_deal_messages_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Tier 3: Sync deal messages for specific integration (manual trigger)."""
```

---

## Celery Configuration

### Task Routes (`config.py`)

```python
self.task_routes = {
    # Messaging channels sync tasks (three-tier architecture)
    "integrations.sync_messaging_channels_all_orgs": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
    "integrations.sync_messaging_channels_org": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
    "integrations.sync_messaging_channels_integration": {
        "queue": "integrations_priority",  # Priority queue for manual triggers
        "routing_key": "integrations_priority",
    },
    # Deal messages sync tasks (three-tier architecture)
    "integrations.sync_deal_messages_all_orgs": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
    "integrations.sync_deal_messages_org": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
    "integrations.sync_deal_messages_integration": {
        "queue": "integrations_priority",  # Priority queue for manual triggers
        "routing_key": "integrations_priority",
    },
}
```

### Beat Schedule (`config.py`)

```python
self.beat_schedule = {
    "sync-messaging-channels-all-orgs": {
        "task": "integrations.sync_messaging_channels_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/180"),  # Every 3 hours
        "kwargs": {},
        "options": {
            "queue": "integrations",
            "routing_key": "integrations",
        },
    },
    "sync-deal-messages-all-orgs": {
        "task": "integrations.sync_deal_messages_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/180"),  # Every 3 hours
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

### Tier 1 Result (Messaging)

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

### Tier 2 Result (Messaging)

```python
{
    "status": "completed",  # or "partial", "failed"
    "organisation_id": "uuid-1",
    "started_at": "2026-01-10T10:00:06+00:00",
    "completed_at": "2026-01-10T10:00:30+00:00",
    "summary": {
        "total_integrations": 2,
        "syncs_triggered": 4,  # 2 per integration (channels + chats)
        "syncs_failed": 0
    },
    "integration_results": [
        {
            "integration_id": "int-1",
            "integration_type": "teams",
            "user_id": "user-uuid",
            "syncs": [
                {"sync_type": "channels", "task_id": "...", "status": "triggered"},
                {"sync_type": "chats", "task_id": "...", "status": "triggered"}
            ]
        }
    ]
}
```

### Tier 3 Result (Manual Trigger)

```python
{
    "status": "success",  # or "partial", "failed"
    "organisation_id": "uuid-1",
    "integration_id": "int-1",
    "integration_type": "teams",
    "started_at": "2026-01-10T10:00:00+00:00",
    "completed_at": "2026-01-10T10:00:15+00:00",
    "syncs": [
        {"sync_type": "channels", "task_id": "...", "status": "triggered"},
        {"sync_type": "chats", "task_id": "...", "status": "triggered"}
    ]
}
```

### Deal Messages Tier 2 Result

```python
{
    "status": "completed",  # or "partial", "failed"
    "organisation_id": "uuid-1",
    "started_at": "2026-01-10T10:30:00+00:00",
    "completed_at": "2026-01-10T10:31:30+00:00",
    "summary": {
        "channels_synced": 5,
        "channels_failed": 1,
        "messages_synced": 150
    },
    "errors": [
        {
            "channel_id": "...",
            "channel_name": "Sales Team",
            "error": "Rate limit exceeded"
        }
    ]
}
```

---

## Usage Examples

```python
# Tier 1: Sync all organizations (usually via Beat, but can be manual)
from src.workers.integrations_worker.tasks.messaging import sync_messaging_channels_all_orgs_task
result = sync_messaging_channels_all_orgs_task.delay()

# Tier 2: Sync specific organization
from src.workers.integrations_worker.tasks.messaging import sync_messaging_channels_org_task
result = sync_messaging_channels_org_task.delay(organisation_id="your-org-uuid")

# Tier 3: Sync specific integration (manual refresh)
from src.workers.integrations_worker.tasks.messaging import sync_messaging_channels_integration_task
result = sync_messaging_channels_integration_task.delay(
    organisation_id="your-org-uuid",
    integration_id="your-integration-uuid"
)

# Deal messages sync for all orgs
from src.workers.integrations_worker.tasks.messaging import sync_deal_messages_all_orgs_task
result = sync_deal_messages_all_orgs_task.delay()

# Deal messages sync for specific org
from src.workers.integrations_worker.tasks.messaging import sync_deal_messages_org_task
result = sync_deal_messages_org_task.delay(organisation_id="your-org-uuid")
```

---

## File Reference

| Component | File Path |
|-----------|-----------|
| **Tier 1 Task (Channels)** | `src/workers/integrations_worker/tasks/messaging/channels_all_orgs_task.py` |
| **Tier 2 Task (Channels)** | `src/workers/integrations_worker/tasks/messaging/channels_org_task.py` |
| **Tier 3 Task (Channels)** | `src/workers/integrations_worker/tasks/messaging/channels_integration_task.py` |
| **Tier 1 Task (Deal Messages)** | `src/workers/integrations_worker/tasks/messaging/deal_messages_all_orgs_task.py` |
| **Tier 2 Task (Deal Messages)** | `src/workers/integrations_worker/tasks/messaging/deal_messages_org_task.py` |
| **Tier 3 Task (Deal Messages)** | `src/workers/integrations_worker/tasks/messaging/deal_messages_integration_task.py` |
| **Worker Task** | `src/workers/integrations_worker/tasks/sync_tasks.py` |
| **Dispatcher** | `src/workers/integrations_worker/services/dispatcher.py` |
| **Messaging Adapter** | `src/workers/integrations_worker/adapters/messaging_adapter.py` |
| **Teams Provider** | `src/workers/integrations_worker/providers/messaging/teams.py` |
| **Deal Messages Service** | `src/service/integrations/deal_channel_messages_service.py` |
| **Celery Config** | `src/workers/integrations_worker/config.py` |

---

## Deprecated Files

### `src/workers/integrations_worker/tasks/teams_sync_tasks.py`

| Field | Value |
|-------|-------|
| **Status** | DEPRECATED |
| **Replacement** | `src/workers/integrations_worker/tasks/messaging/` |
| **Task Name** | `integrations.sync_teams_channels` |
| **Action Required** | Delete in next major release |

**Why deprecated**:

- Uses a single monolithic task that loops through all orgs internally
- No org-level task tracking or retry capability
- Doesn't follow the three-tier architecture pattern

### `src/workers/integrations_worker/tasks/deal_channel_messages_tasks.py`

| Field | Value |
|-------|-------|
| **Status** | DEPRECATED |
| **Replacement** | `src/workers/integrations_worker/tasks/messaging/deal_messages_*.py` |
| **Task Names** | `integrations.trigger_deal_channel_messages_sync_all_orgs`, `integrations.sync_deal_channel_messages` |
| **Action Required** | Delete in next major release |

**Why deprecated**:

- Should be consolidated with messaging sync in a single module
- Task naming doesn't follow the standard pattern

**Before deletion, verify**:

1. No code directly imports old task functions
2. No external systems call old task names
3. Update any tests that reference the old tasks
4. Remove the task routes from `config.py`

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

- `"Starting messaging sync for all organizations (Tier 1)"` - Tier 1 started
- `"Dispatched messaging sync for org: {uuid}"` - Tier 2 task dispatched
- `"Starting messaging sync for organization (Tier 2): {uuid}"` - Tier 2 started
- `"Dispatching messaging sync: org={} integration={}"` - Integration sync dispatched
- `"Starting deal messages sync for organization (Tier 2): {uuid}"` - Deal messages Tier 2 started

---

## Teams Delta Query Integration

### Overview

Microsoft Teams **channel messages** support **Delta Query** for efficient incremental synchronization. This allows fetching only new, updated, or deleted messages since the last sync.

> **Important**: Teams **chat messages** (group chats, meeting chats) do **NOT** support Delta Query. Microsoft Graph returns error: "Change tracking is not supported against 'microsoft.graph.chatMessage'". For chats, we use client-side filtering instead (see [Chat Messages Sync](#chat-messages-sync-no-delta-support) below).

### How It Works

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  INITIAL SYNC (first time for a channel/chat)                               │
│                                                                             │
│  1. Call: GET /teams/{id}/channels/{id}/messages/delta                      │
│  2. API returns ALL messages + @odata.deltaLink                             │
│  3. Store deltaLink in MessagingChannel.last_sync_cursor                    │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  INCREMENTAL SYNC (subsequent syncs)                                        │
│                                                                             │
│  1. Call: GET {stored_deltaLink}                                            │
│  2. API returns ONLY changes since deltaLink was issued:                    │
│     • Created messages: Full message object                                 │
│     • Updated messages: Full message object                                 │
│     • Deleted messages: @removed annotation                                 │
│  3. Store new deltaLink for next sync                                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Delta Link Endpoints

| Message Type | Endpoint | Delta Support |
|--------------|----------|---------------|
| **Channel Messages** | `/v1.0/teams/{team-id}/channels/{channel-id}/messages/delta` | ✅ Yes |
| **Chat Messages** | `/v1.0/chats/{chat-id}/messages` | ❌ No (uses client-side filtering) |

### Delta Link Storage

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  MessagingChannel Table                                                     │
│                                                                             │
│  ┌─────────────────┬────────────────────────────────────────────────────┐  │
│  │ Field           │ Purpose                                             │  │
│  ├─────────────────┼────────────────────────────────────────────────────┤  │
│  │ channel_id      │ Primary key (UUID)                                  │  │
│  │ external_channel_id │ Teams channel/chat ID                          │  │
│  │ last_sync_cursor│ Stores deltaLink URL (channel messages only)       │  │
│  │ last_synced_at  │ Timestamp of last sync (used for chat filtering)   │  │
│  └─────────────────┴────────────────────────────────────────────────────┘  │
│                                                                             │
│  Channel Messages: Use last_sync_cursor (deltaLink) for incremental sync   │
│  Chat Messages: Use last_synced_at for client-side filtering               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Handling Deleted Messages

When a message is deleted, Delta Query returns:

```json
{
  "id": "1234567890",
  "@removed": {
    "reason": "deleted"
  }
}
```

The provider marks this with sync metadata:

```python
{
  "id": "1234567890",
  "_sync_metadata": {
    "sync_action": "DELETED",
    "sync_type": "INCREMENTAL",
    "source": "delta_removed"
  }
}
```

The service then marks the message as `is_deleted = True` in the database.

### Expired Delta Link Recovery

Delta links can expire after extended inactivity. When this happens:

1. Microsoft returns 410 Gone or `syncStateNotFound` error
2. Provider automatically retries with fresh delta query
3. Full re-sync occurs, new delta link is stored

```python
# Automatic recovery in teams.py
if "410" in error_msg or "syncStateNotFound" in error_code:
    logger.warning("deltaLink expired, retrying with fresh delta query")
    return await fetch_messages(delta_link=None, ...)  # Force fresh sync
```

### Benefits of Delta Query

| Aspect | Without Delta | With Delta |
|--------|---------------|------------|
| **API Calls** | Fetch ALL messages every sync | Fetch ONLY changes |
| **Data Transfer** | High (all messages) | Minimal (changed only) |
| **Sync Speed** | Slow (pagination through all) | Fast (changes only) |
| **Deleted Detection** | Must compare before/after | Automatic via @removed |
| **Rate Limits** | Higher risk of hitting limits | Lower API usage |

---

## Chat Messages Sync (No Delta Support)

### Why Client-Side Filtering?

Microsoft Graph API has two key limitations for chat messages:

1. **No Delta Query**: `GET /chats/{id}/messages/delta` returns error "Change tracking is not supported against 'microsoft.graph.chatMessage'"

2. **No `$filter` on `createdDateTime`**: `GET /chats/{id}/messages?$filter=createdDateTime gt {date}` returns error "The entity property 'createdDateTime' and operationKind 'GreaterThan' is not allowed in $filter query"

### How It Works

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  INITIAL SYNC (first time for a chat)                                       │
│                                                                             │
│  1. Call: GET /chats/{id}/messages?$orderby=createdDateTime desc            │
│  2. API returns all messages (newest first)                                 │
│  3. Store all messages + update last_synced_at                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  INCREMENTAL SYNC (subsequent syncs)                                        │
│                                                                             │
│  1. Call: GET /chats/{id}/messages?$orderby=createdDateTime desc            │
│  2. For each message in response:                                           │
│     • If message.createdDateTime > last_synced_at → keep (new message)      │
│     • If message.createdDateTime <= last_synced_at → STOP (reached old)     │
│  3. Early termination prevents fetching entire chat history                 │
│  4. Store new messages + update last_synced_at                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Implementation

```python
# providers/messaging/teams.py - fetch_chat_messages()

async def fetch_chat_messages(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    chat_id: str,
    ...
) -> FetchMessagesResult:
    """Fetch chat messages with client-side incremental filtering."""

    # Order by newest first - enables early termination
    initial_params = {
        "$top": "50",
        "$orderby": "createdDateTime desc",
    }

    cutoff_date = None if is_initial else last_synced_at
    all_messages = []
    reached_cutoff = False

    while next_link and not reached_cutoff:
        resp = await nango.proxy_get(endpoint, params)

        for item in resp["value"]:
            created_at = datetime.fromisoformat(item["createdDateTime"])

            # Stop when we hit messages older than our cutoff
            if cutoff_date and created_at <= cutoff_date:
                reached_cutoff = True
                break

            all_messages.append(item)

        next_link = resp.get("@odata.nextLink")

    # No deltaLink for chats - return None
    return FetchMessagesResult(messages=all_messages, delta_link=None)
```

### Determining Initial vs Incremental

For chat messages, we determine if it's an initial sync by checking `last_synced_at`:

```python
# deal_channel_messages_service.py

async def _fetch_messages_for_channel(self, channel_info, integration):
    if channel_type in (ChannelType.GROUP_MESSAGE, ChannelType.MEETING):
        # For chats: is_initial based on last_synced_at (no delta support)
        is_initial = channel_info.last_synced_at is None

        return await fetch_chat_messages(
            is_initial=is_initial,
            last_synced_at=channel_info.last_synced_at,
            ...
        )
```

### Trade-offs

| Aspect | Channel Messages (Delta) | Chat Messages (Client-Side) |
|--------|--------------------------|----------------------------|
| **API Efficiency** | Optimal (only changes) | Sub-optimal (fetches until cutoff) |
| **Deleted Detection** | Automatic via @removed | Not supported* |
| **Implementation** | Simple (use deltaLink) | More complex (manual filtering) |
| **Storage** | Requires deltaLink storage | Uses timestamp only |

*Chat message deletions are not detected. If a chat message is deleted after sync, it remains in our database.

---

### Provider Implementation

```python
# providers/messaging/teams.py

@dataclass
class FetchMessagesResult:
    """Result containing messages and optional delta token."""
    messages: list[dict[str, Any]]
    delta_link: str | None = None


async def fetch_messages(
    *,
    nango: NangoClient,
    connection_id: str,
    team_id: str,
    channel_id: str,
    delta_link: str | None = None,  # Pass stored deltaLink
    ...
) -> FetchMessagesResult:
    """
    Fetch Teams messages using Delta Query.

    Returns FetchMessagesResult with messages and new deltaLink for storage.
    """
```

### Service Integration

```python
# deal_channel_messages_service.py

async def sync_channel_messages(self, channel_info):
    # Get stored deltaLink
    delta_link = channel_info.last_sync_cursor

    # Fetch with delta
    fetch_result = await self._fetch_messages(
        channel_info, integration, delta_link=delta_link
    )

    # Store new deltaLink for next sync
    await self._update_channel_sync_state(
        channel_info.channel_id,
        delta_link=fetch_result.delta_link,
    )
```

---

## Related Documentation

- [Scheduled Sync Architecture Guide](./scheduled-sync-architecture.md) - Three-tier pattern overview and Delta Link details
- [Calendar Sync Flow](./calendar-sync-flow.md) - Calendar sync implementation
- [Data Sync Flow](./data-sync-flow.md) - General integration sync flow
- [Integration Data Sync Architecture](./Integration-data-sync-architecture.md) - System architecture

---

## Migration Completed

The migration from monolithic webhook-driven tasks to the three-tier scheduled sync architecture has been completed.

### Files Removed

The following legacy files have been deleted:

| File | Replaced By |
|------|-------------|
| `tasks/teams_sync_tasks.py` | `tasks/messaging/channels_*.py` |
| `tasks/deal_channel_messages_tasks.py` | `tasks/messaging/deal_messages_*.py` |
| `tasks/calendar_sync_tasks.py` | `tasks/calendar/*.py` |

### Completed Migration Steps

1. ✅ Removed deprecated imports from `tasks/__init__.py`
2. ✅ Removed deprecated imports from `celery_app.py`
3. ✅ Removed deprecated task routes from `config.py`
4. ✅ Deleted deprecated task files
5. ✅ Deleted deprecated test file (`test_calendar_sync_tasks.py`)
6. ✅ Updated documentation references

### Task Name Mapping (Historical Reference)

| Old Task Name | New Task Name | Notes |
|--------------|---------------|-------|
| `integrations.sync_teams_channels` | `integrations.sync_messaging_channels_all_orgs` | Tier 1 global coordinator |
| `integrations.trigger_deal_channel_messages_sync_all_orgs` | `integrations.sync_deal_messages_all_orgs` | Tier 1 global coordinator |
| `integrations.sync_deal_channel_messages` | `integrations.sync_deal_messages_org` | Tier 2 org-level processor |
| `integrations.sync_calendar_events` | `integrations.sync_calendar_all_orgs` | Tier 1 global coordinator |
