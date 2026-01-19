# Email Sync Implementation - Three-Tier Architecture

**Created**: January 2026
**Updated**: January 2026
**Status**: Implemented (Phase 1)
**Priority**: High

## Overview

This document outlines the email sync implementation using the **three-tier task architecture**, triggered by Celery Beat (scheduled tasks) instead of Nango webhooks.

**Key Architecture**: Three-tier task structure following the pattern defined in `scheduled-sync-architecture.md`:
- **Tier 1**: Global task (`sync_email_all_orgs_task`) - Triggered by Celery Beat
- **Tier 2**: Organization task (`sync_email_org_task`) - Syncs all integrations for one org
- **Tier 3**: Integration task (`sync_email_integration_task`) - Manual/user-level triggers

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| EmailDeltaSyncService | **IMPLEMENTED** | Orchestration service |
| Tier 1 Task | **IMPLEMENTED** | `tasks/email/all_orgs_task.py` |
| Tier 2 Task | **IMPLEMENTED** | `tasks/email/org_task.py` |
| Tier 3 Task | **IMPLEMENTED** | `tasks/email/integration_task.py` |
| Celery Beat Schedule | **IMPLEMENTED** | Every 30 minutes |
| Task Routes | **IMPLEMENTED** | In `config.py` |
| Delta Query (per-folder) | PLANNED | Future enhancement |
| Pagination Support | PLANNED | Future enhancement |

---

## Architecture Comparison

| Aspect | Old (Webhook) | New (Three-Tier Scheduled) |
|--------|---------------|----------------------------|
| Trigger | Nango webhook | Celery Beat (every 30 min) |
| Entry Point | `nango_webhook_service.py` → dispatcher | `sync_email_all_orgs_task` → `sync_email_org_task` |
| Orchestration | Dispatcher handles all | `EmailDeltaSyncService` |
| API Endpoint | `/v1.0/me/messages` (timestamp filter) | Same (delta query planned for future) |
| Organization Handling | Single org per webhook | All orgs in one scheduled run |
| Manual Trigger | Via webhook simulation | `sync_email_integration_task` (Tier 3) |

---

## Three-Tier Architecture Flow

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TIER 1: GLOBAL TASK                            │
│                         (Triggered by Celery Beat)                          │
│                                                                             │
│  File: tasks/email/all_orgs_task.py                                         │
│  Task: integrations.sync_email_all_orgs                                     │
│  Schedule: Every 30 minutes                                                 │
│                                                                             │
│  Responsibility:                                                            │
│  • Query master DB for all APPROVED organizations                           │
│  • Dispatch Tier 2 task for each organization (async)                       │
│  • Return dispatch summary                                                  │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
            Dispatches sync_email_org_task for each org
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TIER 2: ORGANIZATION TASK                         │
│                      (Triggered by Tier 1 or API)                           │
│                                                                             │
│  File: tasks/email/org_task.py                                              │
│  Task: integrations.sync_email_org                                          │
│                                                                             │
│  Responsibility:                                                            │
│  • Query org DB for active email integrations (OUTLOOK_EMAIL)               │
│  • For each integration: call EmailDeltaSyncService.sync_integration()      │
│  • Combined folders + messages sync per integration                         │
│  • Return org-level aggregated results                                      │
│                                                                             │
│  Retries: max_retries=2, delay=120s                                         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   │ Calls service directly (inline loop)
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     EmailDeltaSyncService                                   │
│                                                                             │
│  File: src/service/integrations/email_delta_sync_service.py                 │
│                                                                             │
│  Methods:                                                                   │
│  • sync_integration(org_id, integration_id) - Public entry point            │
│  • _sync_integration(session, org_id, integration) - Core sync logic        │
│  • _sync_folders(...) - Fetch and sync folders                              │
│  • _sync_folder_messages(...) - Sync messages for one folder                │
│  • _get_folders_to_sync(...) - Filter to inbox/sent + monitored             │
└─────────────────────────────────────────────────────────────────────────────┘

                              ┌────────────────────┐
                              │  TIER 3 (Manual)   │
                              └─────────┬──────────┘
                                        │
┌─────────────────────────────────────────────────────────────────────────────┐
│                          TIER 3: INTEGRATION TASK                           │
│                       (For Manual/User-Level Triggers)                      │
│                                                                             │
│  File: tasks/email/integration_task.py                                      │
│  Task: integrations.sync_email_integration                                  │
│                                                                             │
│  Triggered by:                                                              │
│  • Frontend "Refresh" button                                                │
│  • API endpoint for on-demand sync                                          │
│  • Manual intervention/debugging                                            │
│                                                                             │
│  NOT used by scheduled sync (Tier 1 + Tier 2 handle that)                   │
│  Retries: max_retries=3, delay=60s                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Sync Operations

### What Gets Synced

| Operation | Description | Folders Included |
|-----------|-------------|------------------|
| **Folders** | Email folder structure | All folders |
| **Messages** | Actual emails | Inbox, Sent, + any `is_monitored=True` folders |

### Sync Flow Per Integration

```text
EmailDeltaSyncService._sync_integration()
    │
    ├── 1. _sync_folders()
    │       • Fetch folders from Outlook via Nango
    │       • Sync to email_folders table
    │
    ├── 2. _get_folders_to_sync()
    │       • Filter: folder_type IN ("inbox", "sent")
    │       • OR: is_monitored = True
    │
    └── 3. For each folder: _sync_folder_messages()
            • Fetch messages (time-based incremental)
            • Sync to emails table
            • Update folder.last_synced_at
```

---

## File Structure

```text
src/
├── service/integrations/
│   ├── email_sync_service.py           # EXISTING: DB operations (unchanged)
│   └── email_delta_sync_service.py     # NEW: Orchestration service
│
└── workers/integrations_worker/
    ├── config.py                        # MODIFIED: Added routes & schedule
    ├── celery_app.py                    # MODIFIED: Import email module
    │
    ├── tasks/
    │   ├── email/                       # NEW: Three-tier task folder
    │   │   ├── __init__.py              # Exports all email tasks
    │   │   ├── all_orgs_task.py         # Tier 1: sync_email_all_orgs_task
    │   │   ├── org_task.py              # Tier 2: sync_email_org_task
    │   │   └── integration_task.py      # Tier 3: sync_email_integration_task
    │   │
    │   └── sync_tasks.py                # EXISTING: Webhook-driven (unchanged)
    │
    └── providers/email/
        └── outlook_email.py             # EXISTING: fetch_folders, fetch_messages
```

---

## Configuration

### Task Routes (config.py)

```python
"integrations.sync_email_all_orgs": {
    "queue": "integrations",
    "routing_key": "integrations",
},
"integrations.sync_email_org": {
    "queue": "integrations",
    "routing_key": "integrations",
},
"integrations.sync_email_integration": {
    "queue": "integrations_priority",  # Priority for manual triggers
    "routing_key": "integrations_priority",
},
```

### Beat Schedule (config.py)

```python
"sync-email-all-orgs": {
    "task": "integrations.sync_email_all_orgs",
    "schedule": crontab(minute="*/30"),  # Every 30 minutes
    "kwargs": {},
    "options": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
},
```

---

## Result Formats

### Tier 1 Result (All Orgs)

```python
{
    "status": "completed",
    "started_at": "2026-01-10T10:00:00Z",
    "completed_at": "2026-01-10T10:00:05Z",
    "summary": {
        "total_orgs": 50,
        "orgs_triggered": 48,
        "orgs_skipped": 2
    },
    "org_tasks": [
        {"org_id": "uuid-1", "task_id": "celery-task-id-1", "status": "triggered"},
        {"org_id": "uuid-2", "task_id": "celery-task-id-2", "status": "triggered"},
    ]
}
```

### Tier 2 Result (Organization)

```python
{
    "status": "completed",
    "organisation_id": "uuid-1",
    "started_at": "2026-01-10T10:00:06Z",
    "completed_at": "2026-01-10T10:01:30Z",
    "summary": {
        "total_integrations": 2,
        "successful": 2,
        "failed": 0,
        "total_folders_synced": 10,
        "total_messages_synced": 250
    },
    "integration_results": [
        {
            "integration_id": "int-1",
            "integration_type": "outlook_email",
            "status": "success",
            "folders_synced": 5,
            "messages_synced": 150
        }
    ]
}
```

### Tier 3 Result (Integration)

```python
{
    "status": "success",
    "organisation_id": "uuid-1",
    "integration_id": "int-1",
    "integration_type": "outlook_email",
    "started_at": "2026-01-10T10:00:00Z",
    "completed_at": "2026-01-10T10:00:45Z",
    "folders_synced": 5,
    "messages_synced": 150,
    "folder_results": [
        {"folder_name": "Inbox", "folder_type": "inbox", "messages_synced": 100},
        {"folder_name": "Sent Items", "folder_type": "sent", "messages_synced": 50}
    ]
}
```

---

## Future Enhancements (Planned)

### Phase 2: Delta Query Support

Currently using time-based incremental sync. Future enhancement to use Microsoft Graph Delta Query:

```text
Current:
  /v1.0/me/mailFolders/{id}/messages?$filter=receivedDateTime ge ...

Future (per-folder delta):
  /v1.0/me/mailFolders/{id}/messages/delta
```

**Storage**: Delta links in `KnowledgeIntegration.connection_metadata`:

```python
{
    "email_folder_delta_links": {
        "AAMkAGI2...inbox_id": "https://graph.microsoft.com/...?$deltatoken=xxx",
        "AAMkAGI2...sent_id": "https://graph.microsoft.com/...?$deltatoken=yyy"
    }
}
```

### Phase 3: Pagination Support

Handle high-volume mailboxes with full pagination of message results.

### Phase 4: Gmail Support

Extend to support Gmail provider (IntegrationType.GMAIL).

---

## Testing

### Manual Task Invocation

```bash
# Test Tier 1 (all orgs)
celery -A src.workers.integrations_worker.celery_app call integrations.sync_email_all_orgs

# Test Tier 2 (specific org)
celery -A src.workers.integrations_worker.celery_app call integrations.sync_email_org \
  --kwargs='{"organisation_id": "uuid-here"}'

# Test Tier 3 (specific integration)
celery -A src.workers.integrations_worker.celery_app call integrations.sync_email_integration \
  --kwargs='{"organisation_id": "org-uuid", "integration_id": "int-uuid"}'
```

### Verification

1. Check Celery task status in logs
2. Verify `email_folders` and `emails` tables have data
3. Check `integration_sync_history` for sync records
4. Verify `knowledge_integrations.last_synced_at` is updated

---

## References

- [Scheduled Sync Architecture Guide](../Technical%20Documentation/integration/sync_tasks/scheduled-sync-architecture.md)
- [Calendar Sync Flow](../Technical%20Documentation/integration/sync_tasks/calendar-sync-flow.md)
- [Microsoft Graph Messages API](https://learn.microsoft.com/en-us/graph/api/user-list-messages)
- [Microsoft Graph Delta Query](https://learn.microsoft.com/en-us/graph/delta-query-messages)
