# Scheduled Sync Architecture Guide

**Last Updated**: January 2026
**Status**: Architecture & Implementation Guide

## Overview

This document explains the architectural decision to transition from **Nango webhook-driven synchronization** to **Celery Beat scheduled synchronization** for integration data syncing, and provides the **proposed task structure** for implementing this pattern consistently across all integration types.

---

## Table of Contents

1. [Current Architecture: Webhook-Driven Sync](#current-architecture-webhook-driven-sync)
2. [Problems with Webhook-Driven Approach](#problems-with-webhook-driven-approach)
3. [Problems with Current Code Structure](#problems-with-current-code-structure)
4. [New Architecture: Scheduled Task Sync](#new-architecture-scheduled-task-sync)
5. [Proposed Three-Tier Task Architecture](#proposed-three-tier-task-architecture)
6. [Proposed Folder Structure](#proposed-folder-structure)
7. [Comparison: Webhook vs Scheduled](#comparison-webhook-vs-scheduled)
8. [Benefits of Scheduled Approach](#benefits-of-scheduled-approach)
9. [Trade-offs and Considerations](#trade-offs-and-considerations)
10. [Migration Strategy](#migration-strategy)
11. [Implementation Checklist](#implementation-checklist)
12. [Delta Link Support](#delta-link-support)

---

## Current Architecture: Webhook-Driven Sync

### How It Works

```text
┌──────────────────┐
│  External API    │  Data changes (create/update/delete)
│ (Google/Outlook) │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Nango Platform  │  Detects change, sends webhook
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  WEBHOOK HANDLER (FastAPI)                                   │
│  src/service/integrations/nango_webhook_service.py           │
│  • Immediately responds 200 OK to Nango                      │
│  • Enqueues Celery task asynchronously                       │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  CELERY TASK: integrations.sync_integration_data             │
│  src/workers/integrations_worker/tasks/sync_tasks.py         │
│  • Fetches data from provider via Nango proxy                │
│  • Stores in database via adapters                           │
└──────────────────────────────────────────────────────────────┘
```

### Implementation Reference

**File**: `src/workers/integrations_worker/tasks/sync_tasks.py`

```python
@app.task(
    name="integrations.sync_integration_data",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_integration_data_task(self: Task, *, organisation_id, integration_id, ...):
    # Task triggered by webhook, processes sync request
```

---

## Problems with Webhook-Driven Approach

### 1. Fire-and-Forget Reliability Issue

When Nango sends a webhook, our API **immediately responds with 200 OK** before the actual sync completes:

```text
Nango Webhook → API receives → Returns 200 OK → Triggers background task
                                    ↑
                         Success reported HERE
                         (but sync hasn't happened yet!)
```

**Problem**: If the background task fails after we've acknowledged the webhook:
- Nango believes the sync was successful
- No automatic retry from Nango
- Data loss occurs silently
- No record of what was supposed to sync

### 2. No Centralized Failure Tracking

- Failed syncs are logged but not systematically tracked
- No easy way to identify which integrations are failing
- No mechanism to retry failed syncs across all users
- Debugging requires searching through distributed logs

### 3. Cascading Operations Not Triggered

When data syncs, dependent operations should occur:

| Data Type | Required Downstream Operations |
|-----------|-------------------------------|
| Calendar Events | Deal enrichment, Meeting analysis |
| Contacts | Stakeholder announcement updates |
| Emails | Thread analysis, Deal association |
| Deals | Pipeline updates, Forecasting |

**Problem**: Webhook-driven sync only handles the immediate sync. Triggering downstream operations requires:
- Additional task chaining (complex)
- Manual coordination
- Risk of missed triggers

### 4. Difficult to Manage at Scale

- Each webhook is isolated
- No global view of sync status
- Hard to implement features like:
  - Pause/resume syncing for an org
  - Priority queuing
  - Rate limiting across all integrations
  - Sync health monitoring

### 5. Webhook Delivery Not Guaranteed

External factors can cause webhook loss:
- Network issues between Nango and our API
- API downtime or restarts
- Load balancer issues
- Webhook timeout (Nango may timeout before we respond)

---

## Problems with Current Code Structure

### 1. Inconsistent Task Patterns

Current implementations have **inconsistent patterns** across different integration types:

```text
calendar_sync_tasks.py   → Single task handles both global + org-level
teams_sync_tasks.py      → Different pattern, mixed responsibilities
sync_tasks.py            → Webhook-oriented, integration-level only
```

**Issues**:
- No standard pattern for developers to follow
- Each new integration type reinvents the wheel
- Difficult to maintain consistency across the codebase

### 2. Monolithic Task Functions

Current tasks try to do everything in one function:

```python
# Current pattern - one function doing too much
def sync_calendar_events_task(organisation_id=None, integration_id=None):
    if organisation_id is None:
        # Sync ALL orgs (scheduled mode)
        for org in get_all_approved_orgs():
            for integration in get_integrations(org):
                sync_integration_data_task.apply_async(...)
    elif integration_id is None:
        # Sync one org (all integrations)
        for integration in get_integrations(organisation_id):
            sync_integration_data_task.apply_async(...)
    else:
        # Sync specific integration
        sync_integration_data_task.apply_async(...)
```

**Issues**:
- Single function with multiple responsibilities
- Hard to test individual behaviors
- Difficult to monitor at each level
- No clear separation of concerns

### 3. Missing Organization-Level Task Layer

Current structure jumps directly from "all orgs" to "integration-level":

```text
Current Flow:
  Global Task → [loop inside task] → Integration Task

Missing Layer:
  Global Task → Organization Task → Integration Task
```

**Why this matters**:
- Can't retry failed orgs independently
- Can't track org-level sync status
- Can't implement org-specific rate limiting
- Can't pause/resume specific orgs

### 4. Tight Coupling with Webhook Structure

The `sync_integration_data_task` was designed for webhooks:

```python
# Webhook-oriented parameters
sync_integration_data_task(
    organisation_id=...,
    integration_id=...,
    provider_config_key=...,  # From Nango webhook
    sync_name=...,            # From Nango webhook
    sync_type=...,            # From Nango webhook (INITIAL/INCREMENTAL)
    response_results=...,     # From Nango webhook
)
```

**Issues**:
- Parameters assume webhook context
- Scheduled tasks must "fake" webhook parameters
- No clean way to express "sync everything for this integration"

### 5. No Standardized Result Aggregation

Each task has its own result format:

```python
# calendar_sync_tasks.py returns:
{"status": "completed", "organizations_processed": 15, "results": [...]}

# teams_sync_tasks.py returns:
{"status": "success", "teams_synced": 5, "channels_synced": 20}

# sync_tasks.py returns:
{"status": "success", "items": {"events": {...}}}
```

**Issues**:
- Inconsistent monitoring and alerting
- Hard to build unified dashboards
- Different error handling patterns

---

## New Architecture: Scheduled Task Sync

### How It Works

```text
┌──────────────────────────────────────────────────────────────┐
│  CELERY BEAT SCHEDULER                                       │
│  Triggers tasks on configurable intervals (e.g., every 3h)  │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  SCHEDULED SYNC TASK                                         │
│  e.g., sync_teams_channels_task                              │
│  1. Query all approved organizations                         │
│  2. Query all active integrations per org                    │
│  3. Trigger individual sync tasks for each                   │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  CELERY TASK: integrations.sync_integration_data             │
│  • Fetches data from provider                                │
│  • Stores in database                                        │
│  • Can trigger downstream operations                         │
└──────────────────────────────────────────────────────────────┘
```

---

## Proposed Three-Tier Task Architecture

To solve the structural problems, we propose a **three-tier task architecture**:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TIER 1: GLOBAL TASK                            │
│                         (Triggered by Celery Beat)                          │
│                                                                             │
│  Task Name: sync_{category}_all_orgs_task                                   │
│  Example:   sync_calendar_all_orgs_task                                     │
│                                                                             │
│  Responsibility:                                                            │
│  • Query master DB for all APPROVED organizations                           │
│  • Trigger Tier 2 task for each organization                                │
│  • Aggregate and return global results                                      │
│  • NO business logic - pure orchestration                                   │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                    Triggers one task per organization
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TIER 2: ORGANIZATION TASK                         │
│                    (Triggered by Global Task or API)                        │
│                                                                             │
│  Task Name: sync_{category}_org_task                                        │
│  Example:   sync_calendar_org_task                                          │
│                                                                             │
│  Parameters: organisation_id (required)                                     │
│                                                                             │
│  Responsibility:                                                            │
│  • Query org DB for all active integrations of this category                │
│  • Loop through integrations and perform sync operations                    │
│  • Handle org-level error recovery                                          │
│  • Return org-level results                                                 │
│  • Contains the actual sync logic for this category                         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                    (For manual/user-level triggers only)
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          TIER 3: INTEGRATION TASK                           │
│                       (For Manual/User-Level Triggers)                      │
│                                                                             │
│  Task Name: sync_{category}_integration_task                                │
│  Example:   sync_calendar_integration_task                                  │
│                                                                             │
│  Parameters: organisation_id (required), integration_id (required)          │
│                                                                             │
│  Responsibility:                                                            │
│  • Sync specific integration (user clicked "Refresh" button)                │
│  • Used for manual intervention and debugging                               │
│  • NOT used by scheduled sync (scheduler uses Tier 1 + Tier 2)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Detailed Flow Diagram

```text
                    ┌─────────────────────────────────┐
                    │        CELERY BEAT              │
                    │   (Every N hours/minutes)       │
                    └───────────────┬─────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: sync_calendar_all_orgs_task()                                        │
│                                                                               │
│  async def _execute():                                                        │
│      orgs = await get_approved_organisations()  # From master DB              │
│      results = []                                                             │
│      for org_id in orgs:                                                      │
│          task = sync_calendar_org_task.apply_async(                           │
│              kwargs={"organisation_id": str(org_id)}                          │
│          )                                                                    │
│          results.append({"org_id": org_id, "task_id": task.id})               │
│      return {"orgs_triggered": len(orgs), "results": results}                 │
└───────────────────────────────────┬───────────────────────────────────────────┘
                                    │
            Triggers one Tier 2 task per org (async, parallel)
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
┌───────────────────┐   ┌───────────────────┐   ┌───────────────────┐
│ sync_calendar_    │   │ sync_calendar_    │   │ sync_calendar_    │
│ org_task          │   │ org_task          │   │ org_task          │
│ (Org A)           │   │ (Org B)           │   │ (Org C)           │
└─────────┬─────────┘   └─────────┬─────────┘   └─────────┬─────────┘
          │                       │                       │
          ▼                       ▼                       ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  TIER 2: sync_calendar_org_task(organisation_id)                              │
│                                                                               │
│  async def _execute():                                                        │
│      integrations = await get_active_calendar_integrations(org_id)            │
│      results = []                                                             │
│      for integration in integrations:                                         │
│          try:                                                                 │
│              result = await sync_calendar_for_integration(                    │
│                  org_id, integration                                          │
│              )                                                                │
│              results.append({"integration_id": ..., "status": "success"})     │
│          except Exception as e:                                               │
│              results.append({"integration_id": ..., "status": "failed"})      │
│      return {"integrations_synced": len(results), "results": results}         │
└───────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ (Tier 3 NOT used by scheduler)
                                    │
┌───────────────────────────────────────────────────────────────────────────────┐
│  TIER 3: sync_calendar_integration_task(organisation_id, integration_id)      │
│                                                                               │
│  USE CASES:                                                                   │
│  • User clicks "Refresh" button on frontend                                   │
│  • Admin manually triggers sync for debugging                                 │
│  • API endpoint for on-demand sync                                            │
│                                                                               │
│  NOTE: This task is NOT called by Tier 1 or Tier 2.                           │
│  Tier 2 performs the sync inline (for loop), not via async task dispatch.     │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Why Tier 2 Uses a For Loop (Not Async Task Dispatch)

```text
APPROACH A (Current/Proposed): Inline for loop in Tier 2
├── Tier 1 triggers Tier 2 tasks (one per org) ✓
└── Tier 2 loops through integrations synchronously
    ├── Pro: Simpler error handling per org
    ├── Pro: Results available immediately
    ├── Pro: Easier to implement org-level transactions
    └── Con: Sequential processing within org

APPROACH B (Alternative): Tier 2 triggers Tier 3 tasks
├── Tier 1 triggers Tier 2 tasks (one per org)
└── Tier 2 triggers Tier 3 tasks (one per integration)
    ├── Pro: Maximum parallelism
    ├── Con: Complex result aggregation
    ├── Con: Harder to track org-level status
    └── Con: More tasks in queue = more overhead

DECISION: Use Approach A
- Parallelism is achieved at org level (Tier 1 → Tier 2)
- Within an org, integrations are processed sequentially (simpler)
- Tier 3 exists only for manual/user-level triggers
```

### Task Naming Convention

```text
Pattern: sync_{category}_{scope}_task

Tier 1 (Global):       sync_{category}_all_orgs_task
Tier 2 (Organization): sync_{category}_org_task
Tier 3 (Integration):  sync_{category}_integration_task

Examples:
├── Calendar:
│   ├── sync_calendar_all_orgs_task
│   ├── sync_calendar_org_task
│   └── sync_calendar_integration_task
│
├── Email:
│   ├── sync_email_all_orgs_task
│   ├── sync_email_org_task
│   └── sync_email_integration_task
│
├── CRM:
│   ├── sync_crm_all_orgs_task
│   ├── sync_crm_org_task
│   └── sync_crm_integration_task
│
└── Messaging Channels (Teams/Slack):
    ├── sync_messaging_channels_all_orgs_task
    ├── sync_messaging_channels_org_task
    └── sync_messaging_channels_integration_task
```

### Standardized Result Format

```python
# Tier 1 Result (Global)
{
    "status": "completed",
    "started_at": "2026-01-10T10:00:00Z",
    "completed_at": "2026-01-10T10:05:00Z",
    "summary": {
        "total_orgs": 50,
        "orgs_triggered": 48,
        "orgs_skipped": 2,  # No active integrations
    },
    "org_results": [
        {"org_id": "uuid-1", "task_id": "celery-task-id-1", "status": "triggered"},
        {"org_id": "uuid-2", "task_id": "celery-task-id-2", "status": "triggered"},
        {"org_id": "uuid-3", "status": "skipped", "reason": "no_active_integrations"},
    ]
}

# Tier 2 Result (Organization)
{
    "status": "completed",  # or "partial", "failed"
    "organisation_id": "uuid-1",
    "started_at": "2026-01-10T10:00:05Z",
    "completed_at": "2026-01-10T10:01:30Z",
    "summary": {
        "total_integrations": 3,
        "successful": 2,
        "failed": 1,
        "total_records_synced": 150,
    },
    "integration_results": [
        {
            "integration_id": "int-1",
            "integration_type": "google_calendar",
            "status": "success",
            "records_synced": 75,
        },
        {
            "integration_id": "int-2",
            "integration_type": "outlook_calendar",
            "status": "success",
            "records_synced": 75,
        },
        {
            "integration_id": "int-3",
            "integration_type": "google_calendar",
            "status": "failed",
            "error": "Connection expired",
        },
    ]
}

# Tier 3 Result (Integration - Manual Trigger)
{
    "status": "success",
    "organisation_id": "uuid-1",
    "integration_id": "int-1",
    "integration_type": "google_calendar",
    "started_at": "2026-01-10T10:00:00Z",
    "completed_at": "2026-01-10T10:00:45Z",
    "records_synced": 75,
    "sync_type": "INCREMENTAL",
}
```

---

## Proposed Folder Structure

### Current Structure (Inconsistent)

```text
src/workers/integrations_worker/
├── tasks/
│   ├── __init__.py
│   ├── sync_tasks.py              # Webhook-driven sync (integration level)
│   ├── calendar_sync_tasks.py     # Calendar - mixed responsibilities
│   ├── teams_sync_tasks.py        # Teams - different pattern
│   ├── deal_channel_messages_tasks.py
│   └── token_refresh.py
├── adapters/
├── providers/
├── services/
└── ...
```

### Proposed Structure (Consistent Three-Tier)

```text
src/workers/integrations_worker/
├── tasks/
│   ├── __init__.py
│   │
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── # SHARED/BASE TASKS
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── sync_tasks.py                    # Base sync_integration_data_task
│   │                                    # (Used by all categories)
│   ├── token_refresh.py                 # OAuth token refresh tasks
│   │
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── # CALENDAR SYNC TASKS
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── calendar/
│   │   ├── __init__.py
│   │   ├── all_orgs_task.py             # Tier 1: sync_calendar_all_orgs_task
│   │   ├── org_task.py                  # Tier 2: sync_calendar_org_task
│   │   └── integration_task.py          # Tier 3: sync_calendar_integration_task
│   │
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── # EMAIL SYNC TASKS
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── email/
│   │   ├── __init__.py
│   │   ├── all_orgs_task.py             # Tier 1: sync_email_all_orgs_task
│   │   ├── org_task.py                  # Tier 2: sync_email_org_task
│   │   └── integration_task.py          # Tier 3: sync_email_integration_task
│   │
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── # CRM SYNC TASKS
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── crm/
│   │   ├── __init__.py
│   │   ├── all_orgs_task.py             # Tier 1: sync_crm_all_orgs_task
│   │   ├── org_task.py                  # Tier 2: sync_crm_org_task
│   │   └── integration_task.py          # Tier 3: sync_crm_integration_task
│   │
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── # MESSAGING SYNC TASKS (Teams, Slack)
│   ├── # ═══════════════════════════════════════════════════════════
│   ├── messaging/
│   │   ├── __init__.py
│   │   ├── all_orgs_task.py              # Tier 1: sync_messaging_channels_all_orgs_task
│   │   ├── org_task.py                   # Tier 2: sync_messaging_channels_org_task
│   │   ├── integration_task.py           # Tier 3: sync_messaging_channels_integration_task
│   │   ├── deal_messages_all_orgs_task.py  # Tier 1: sync_deal_messages_all_orgs_task
│   │   └── deal_messages_org_task.py     # Tier 2: sync_deal_messages_org_task
│   │
│   └── # ═══════════════════════════════════════════════════════════
│       # LEGACY (To be migrated)
│       # ═══════════════════════════════════════════════════════════
│       ├── calendar_sync_tasks.py       # DEPRECATED - migrate to calendar/
│       ├── teams_sync_tasks.py          # DEPRECATED - migrate to messaging/
│       └── deal_channel_messages_tasks.py
│
├── adapters/
│   ├── calendar_adapter.py
│   ├── email_adapter.py
│   ├── crm_adapter.py
│   └── ...
│
├── providers/
│   ├── calendar/
│   │   ├── google_calendar.py
│   │   └── outlook_calendar.py
│   ├── email/
│   │   ├── gmail.py
│   │   └── outlook_mail.py
│   └── ...
│
├── services/
│   └── dispatcher.py
│
└── config.py                            # Celery Beat schedules
```

### Task File Templates

**Tier 1: `all_orgs_task.py`**

```python
"""
Tier 1: Global calendar sync task.

Triggered by Celery Beat to sync calendar events for ALL approved organizations.
"""

from typing import Any
from src.workers.integrations_worker.celery_app import app
from .org_task import sync_calendar_org_task

@app.task(
    name="integrations.sync_calendar_all_orgs",
    bind=False,
    max_retries=0,  # Coordinator doesn't retry
    acks_late=True,
)
def sync_calendar_all_orgs_task() -> dict[str, Any]:
    """
    Tier 1: Sync calendar events for all approved organizations.

    Triggered by: Celery Beat (scheduled)
    Triggers: sync_calendar_org_task for each organization
    """
    # Implementation...
```

**Tier 2: `org_task.py`**

```python
"""
Tier 2: Organization-level calendar sync task.

Syncs all calendar integrations for a specific organization.
"""

from typing import Any
from src.workers.integrations_worker.celery_app import app

@app.task(
    name="integrations.sync_calendar_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
)
def sync_calendar_org_task(
    self,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync all calendar integrations for one organization.

    Triggered by: sync_calendar_all_orgs_task (Tier 1) or API
    Contains: For loop over integrations, performs sync inline
    """
    # Implementation...
```

**Tier 3: `integration_task.py`**

```python
"""
Tier 3: Integration-level calendar sync task.

For manual/user-level triggers only. NOT used by scheduled sync.
"""

from typing import Any
from src.workers.integrations_worker.celery_app import app

@app.task(
    name="integrations.sync_calendar_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def sync_calendar_integration_task(
    self,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """
    Tier 3: Sync specific calendar integration.

    Triggered by: Frontend refresh button, API, manual intervention
    NOT triggered by: Scheduled sync (Tier 1/2)
    """
    # Implementation...
```

### Updated Celery Beat Configuration

```python
# src/workers/integrations_worker/config.py

self.beat_schedule = {
    # ═══════════════════════════════════════════════════════════
    # MESSAGING CHANNELS SYNC - Every 3 hours
    # ═══════════════════════════════════════════════════════════
    "sync-messaging-channels-all-orgs": {
        "task": "integrations.sync_messaging_channels_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/180"),
        "kwargs": {},
        "options": {"queue": "integrations"},
    },

    # ═══════════════════════════════════════════════════════════
    # DEAL MESSAGES SYNC - Every 3 hours
    # ═══════════════════════════════════════════════════════════
    "sync-deal-messages-all-orgs": {
        "task": "integrations.sync_deal_messages_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/180"),
        "kwargs": {},
        "options": {"queue": "integrations"},
    },

    # ═══════════════════════════════════════════════════════════
    # CALENDAR SYNC - Every 30 minutes
    # ═══════════════════════════════════════════════════════════
    "sync-calendar-events-all-orgs": {
        "task": "integrations.sync_calendar_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/30"),
        "kwargs": {},
        "options": {"queue": "integrations"},
    },

    # ═══════════════════════════════════════════════════════════
    # EMAIL SYNC - Every 60 minutes
    # ═══════════════════════════════════════════════════════════
    "sync-email-all-orgs": {
        "task": "integrations.sync_email_all_orgs",  # Tier 1
        "schedule": crontab(minute="*/60"),
        "kwargs": {},
        "options": {"queue": "integrations"},
    },

    # NOTE: CRM sync tasks are not yet implemented
}
```

---

## Comparison: Webhook vs Scheduled

| Aspect | Webhook-Driven | Scheduled Tasks |
|--------|---------------|-----------------|
| **Data Freshness** | Near real-time | Delayed (interval-based) |
| **Reliability** | Depends on webhook delivery | Guaranteed execution |
| **Failure Recovery** | Lost if task fails after ACK | Automatic on next schedule |
| **Downstream Operations** | Manual coordination | Can be orchestrated |
| **Scalability** | Hard to manage globally | Centralized control |
| **Monitoring** | Distributed logs | Centralized status |
| **API Load** | On-demand (varies) | Predictable (scheduled) |
| **Implementation** | Simple (reactive) | More complex (proactive) |

---

## Benefits of Scheduled Approach

### 1. Guaranteed Data Consistency

```text
Schedule runs → Query ALL integrations → Sync each one → Verify success

No integration is "forgotten" - every active integration syncs on schedule
```

### 2. Centralized Failure Handling

```python
# The scheduled task returns comprehensive results
return {
    "status": "completed",
    "organizations_processed": 15,
    "organizations_with_integrations": 12,
    "total_syncs_triggered": 24,
    "results": [
        {"org_id": "...", "status": "success", "syncs_triggered": 2},
        {"org_id": "...", "status": "failed", "error": "..."},
    ]
}
```

- Failed syncs are tracked
- Easy to identify problematic integrations
- Automatic retry on next schedule

### 3. Orchestrated Downstream Operations

```text
Calendar Sync Task
    │
    ├── Sync calendar events
    │       │
    │       ▼
    ├── Trigger deal enrichment task
    │       │
    │       ▼
    ├── Trigger meeting analysis task
    │       │
    │       ▼
    └── Update stakeholder data
```

All operations can be orchestrated in a single scheduled run.

### 4. Better Control and Monitoring

- Pause syncing for maintenance
- Adjust sync frequency per integration type
- Monitor sync health across all orgs
- Implement backpressure and rate limiting

### 5. Predictable Resource Usage

- API calls happen at known intervals
- Can schedule during low-traffic periods
- Easier capacity planning

---

## Trade-offs and Considerations

### When Webhook Approach is Better

1. **Real-time requirements**: If data must be fresh within seconds
2. **Low volume**: Few integrations with infrequent changes
3. **Simple operations**: No downstream processing needed
4. **Provider support**: When provider has reliable webhook infrastructure

### When Scheduled Approach is Better

1. **Data consistency is critical**: Can't afford to lose syncs
2. **Complex workflows**: Multiple downstream operations per sync
3. **Scale**: Many organizations and integrations
4. **Monitoring required**: Need visibility into sync health
5. **Audit requirements**: Need to track all sync operations

### Hybrid Approach (Recommended)

For optimal results, consider a **hybrid approach**:

```text
┌─────────────────────────────────────────────────────────────────┐
│  WEBHOOKS: Handle real-time updates when available              │
│  • Quick propagation of changes                                 │
│  • Best-effort delivery                                         │
└─────────────────────────────────────────────────────────────────┘
                              +
┌─────────────────────────────────────────────────────────────────┐
│  SCHEDULED TASKS: Catch anything webhooks missed               │
│  • Run every few hours as safety net                            │
│  • Full reconciliation ensures no data loss                     │
│  • Trigger all downstream operations                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Migration Strategy

### Phase 1: Implement Three-Tier Task Structure
- Create folder structure for each category
- Implement Tier 1, 2, 3 tasks following the template
- Run alongside existing tasks for validation

### Phase 2: Add Downstream Operations
- Implement task orchestration for dependent operations
- Deal enrichment, stakeholder updates, etc.
- Ensure idempotency for operations that may run multiple times

### Phase 3: Migrate Existing Tasks
- Migrate `calendar_sync_tasks.py` → `tasks/calendar/`
- Migrate `teams_sync_tasks.py` → `tasks/messaging/`
- Update Celery Beat configuration
- Deprecate old task files

### Phase 4: Evaluate Webhook Necessity
- Analyze if real-time updates are actually needed
- Consider keeping webhooks for critical real-time use cases
- Remove webhook handlers where scheduled sync is sufficient

### Phase 5: Optimize Sync Frequency
- Adjust intervals based on data freshness requirements
- Different frequencies for different integration types:
  - Calendar: Every 1-2 hours (meetings are time-sensitive)
  - CRM: Every 3-6 hours (less time-critical)
  - Email: Every 1 hour (conversations move fast)

---

## Implementation Checklist

### Structure Setup
- [x] Create `tasks/calendar/` folder with all three tiers
- [x] Create `tasks/email/` folder with all three tiers
- [ ] Create `tasks/crm/` folder with all three tiers
- [x] Create `tasks/messaging/` folder with all three tiers
- [x] Update `tasks/__init__.py` to import new modules

### Task Implementation
- [x] Implement Tier 1 tasks (all_orgs) for calendar, email, messaging
- [x] Implement Tier 2 tasks (org) for calendar, email, messaging
- [x] Implement Tier 3 tasks (integration) for calendar, email, messaging
- [x] Implement deal_messages sync (Tier 1 + Tier 2) in messaging/
- [x] Ensure standardized result format across all tasks
- [x] Implement Delta Query support for Team channel messages
- [x] Implement client-side filtering for chat messages (no Delta Query support)

### Configuration
- [x] Update Celery Beat schedules in `config.py`
- [x] Configure task routes for new task names
- [x] Set appropriate retry policies per tier
- [x] Remove orphaned `sync-all-integrations` placeholder task

### Testing & Validation
- [ ] Unit tests for each tier
- [ ] Integration tests for full flow
- [ ] Test failure scenarios and recovery
- [ ] Validate result aggregation

### Migration
- [x] Deprecate old task files (calendar_sync_tasks.py, teams_sync_tasks.py, deal_channel_messages_tasks.py)
- [ ] Update any API endpoints calling old tasks
- [x] Update documentation references
- [ ] Monitor for issues post-migration

### Monitoring
- [ ] Set up monitoring and alerting
- [ ] Create dashboards for sync health
- [ ] Document sync frequencies and rationale

---

## Related Files

### Three-Tier Task Implementations

| Category | Tier 1 (all_orgs) | Tier 2 (org) | Tier 3 (integration) |
|----------|-------------------|--------------|----------------------|
| Calendar | `tasks/calendar/all_orgs_task.py` | `tasks/calendar/org_task.py` | `tasks/calendar/integration_task.py` |
| Email | `tasks/email/all_orgs_task.py` | `tasks/email/org_task.py` | `tasks/email/integration_task.py` |
| Messaging Channels | `tasks/messaging/all_orgs_task.py` | `tasks/messaging/org_task.py` | `tasks/messaging/integration_task.py` |
| Deal Messages | `tasks/messaging/deal_messages_all_orgs_task.py` | `tasks/messaging/deal_messages_org_task.py` | N/A (use org-level) |

### Core Files

| Component | Path |
|-----------|------|
| Webhook-Driven Sync Task | `src/workers/integrations_worker/tasks/sync_tasks.py` |
| Celery Beat Config | `src/workers/integrations_worker/config.py` |
| Celery App | `src/workers/integrations_worker/celery_app.py` |
| Webhook Handler | `src/service/integrations/nango_webhook_service.py` |

### Deprecated Files (To Be Removed)

| Component | Path | Replacement |
|-----------|------|-------------|
| Calendar Sync (Legacy) | `tasks/calendar_sync_tasks.py` | `tasks/calendar/` |
| Teams Sync (Legacy) | `tasks/teams_sync_tasks.py` | `tasks/messaging/` |
| Deal Messages (Legacy) | `tasks/deal_channel_messages_tasks.py` | `tasks/messaging/deal_messages_*.py` |

---

## Delta Link Support

### Overview

Microsoft Graph APIs support **Delta Query** for efficient incremental synchronization. Instead of fetching all data each time, Delta Query returns only the changes (created, updated, deleted) since the last sync.

### How Delta Link Works

```text
INITIAL SYNC:
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. First sync: GET /messages/delta                                         │
│     • Returns ALL messages                                                  │
│     • Response includes @odata.deltaLink (token for next sync)              │
│                                                                             │
│  2. Store deltaLink in database for this channel/folder                     │
└─────────────────────────────────────────────────────────────────────────────┘

INCREMENTAL SYNC:
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. Use stored deltaLink: GET {deltaLink}                                   │
│     • Returns ONLY messages changed since deltaLink was issued              │
│     • Created messages: Full message object                                 │
│     • Updated messages: Full message object with changes                    │
│     • Deleted messages: @removed annotation with message ID                 │
│                                                                             │
│  2. Process changes and store new deltaLink for next sync                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Delta Link by Integration Type

| Integration | Delta Support | Initial Fetch | Storage Location | API Endpoint |
|-------------|---------------|---------------|------------------|--------------|
| **Outlook Calendar** | Yes | Events (last 6 months) | `KnowledgeIntegration.connection_metadata` | `/calendarView/delta` |
| **Outlook Email** | Yes | Messages (last 90 days) | `KnowledgeIntegration.connection_metadata` | `/mailFolders/{id}/messages/delta` |
| **Teams Channel Messages** | Yes | All messages | `MessagingChannel.last_sync_cursor` | `/teams/{id}/channels/{id}/messages/delta` |
| **Teams Chat Messages** | **No*** | All messages | `MessagingChannel.last_synced_at` | `/chats/{id}/messages` |
| **Google Calendar** | Yes | Events (last 6 months) | `KnowledgeIntegration.connection_metadata` | Uses `syncToken` |
| **CRM (HubSpot/Salesforce)** | No** | Modified since last sync | N/A | Uses `modifiedAfter` timestamp |

*Teams chat messages do NOT support Delta Query. Microsoft Graph returns error: "Change tracking is not supported against 'microsoft.graph.chatMessage'". We use client-side filtering with `last_synced_at` instead.

**CRM providers don't support delta query - use timestamp-based incremental sync instead.

### Client-Side Filtering for Chat Messages

Since Microsoft Graph doesn't support Delta Query or `$filter` on `createdDateTime` for chat messages, we implement **client-side filtering with early termination**:

```text
INCREMENTAL SYNC FOR CHATS:
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. Fetch messages ordered by createdDateTime DESC (newest first)           │
│     GET /chats/{id}/messages?$orderby=createdDateTime desc                  │
│                                                                             │
│  2. Compare each message's createdDateTime to last_synced_at                │
│     • If message.createdDateTime > last_synced_at → keep (new message)      │
│     • If message.createdDateTime <= last_synced_at → STOP (reached old)     │
│                                                                             │
│  3. Early termination prevents fetching the entire chat history             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Implementation** (`src/workers/integrations_worker/providers/messaging/teams.py`):

```python
# Fetch newest messages first - enables early termination
initial_params = {
    "$top": "50",
    "$orderby": "createdDateTime desc",
}

# Stop when we hit messages older than our cutoff
for item in resp["value"]:
    created_at = datetime.fromisoformat(item["createdDateTime"])
    if cutoff_date and created_at <= cutoff_date:
        reached_cutoff = True
        break  # Early termination - no need to fetch more pages
    all_messages.append(item)
```

### Handling Deleted Items

When Delta Query returns a deleted item, it appears with `@removed` annotation:

```json
{
  "id": "1234567890",
  "@removed": {
    "reason": "deleted"
  }
}
```

Our providers translate this to `_sync_metadata` for consistent handling:

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

Services then mark these items as `is_deleted = True` in the database.

### Handling Expired Delta Links

Delta links can expire (typically after 30-45 days of inactivity). When this happens:

1. Microsoft returns error 410 Gone or `syncStateNotFound`
2. Our providers detect this and automatically retry with fresh delta query
3. A full re-sync occurs, and a new delta link is stored

```python
# Automatic recovery in teams.py
if "410" in error_msg or "syncStateNotFound" in error_code:
    # Retry without delta_link - forces fresh delta query
    return await fetch_messages(delta_link=None, ...)
```

### Delta Link Storage Strategy

**For Email (per folder):**
```python
# Stored in KnowledgeIntegration.connection_metadata
{
  "email_folder_delta_links": {
    "inbox-folder-id": "https://graph.microsoft.com/.../delta?$skiptoken=...",
    "sent-folder-id": "https://graph.microsoft.com/.../delta?$skiptoken=..."
  }
}
```

**For Teams Messages (per channel):**
```python
# Stored in MessagingChannel.last_sync_cursor
# Each channel has its own delta link
channel.last_sync_cursor = "https://graph.microsoft.com/.../delta?$skiptoken=..."
```

**For Calendar (per integration):**
```python
# Stored in KnowledgeIntegration.connection_metadata
{
  "calendar_delta_link": "https://graph.microsoft.com/.../delta?$skiptoken=..."
}
```

---

## Related Documentation

- [Calendar Sync Flow](./calendar-sync-flow.md) - Calendar-specific sync details
- [Messaging Sync Flow](./messaging-sync-flow.md) - Messaging-specific sync details
- [Data Sync Flow](./data-sync-flow.md) - General integration sync flow
- [Integration Data Sync Architecture](./Integration-data-sync-architecture.md) - System architecture
