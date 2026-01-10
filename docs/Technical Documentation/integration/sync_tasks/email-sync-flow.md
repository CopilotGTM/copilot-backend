# Email Sync Tasks: Three-Tier Delta Query Architecture

**Last Updated**: January 2026
**Status**: Active Implementation Document

## Overview

This document explains the **email synchronization** implementation using the **three-tier task architecture** with **Microsoft Graph Delta Query** for efficient incremental sync.

**Key Features**:
- Three-tier task structure (Global → Organization → Integration)
- Delta Query for true incremental sync (only fetches changes)
- Per-folder deltaLink storage
- Full body content included in sync
- "Other" inbox emails filtered out (less important emails)

**Supported Providers**:
- Outlook Email (via Microsoft Graph API)
- Gmail (planned)

---

## Architecture Diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CELERY BEAT SCHEDULER                             │
│                     Triggers every 30 minutes                               │
│                   Config: src/workers/integrations_worker/config.py         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TIER 1: GLOBAL TASK                                 │
│                     sync_email_all_orgs_task                                │
│           File: src/workers/integrations_worker/tasks/email/all_orgs_task.py│
│                                                                             │
│  Responsibility:                                                            │
│  • Query master DB for all APPROVED organizations                           │
│  • Dispatch Tier 2 task for each organization (async)                       │
│  • Return dispatch summary                                                  │
│                                                                             │
│  Config: max_retries=0 (coordinator doesn't retry)                          │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
           Dispatches sync_email_org_task for each org (async)
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      TIER 2: ORGANIZATION TASK                              │
│                        sync_email_org_task                                  │
│            File: src/workers/integrations_worker/tasks/email/org_task.py    │
│                                                                             │
│  Responsibility:                                                            │
│  • Query org DB for active email integrations (OUTLOOK_EMAIL)               │
│  • For each integration: call EmailDeltaSyncService.sync_integration()      │
│  • Combined folders + messages sync per integration                         │
│  • Return org-level aggregated results                                      │
│                                                                             │
│  Config: max_retries=2, retry_delay=120s, soft_time_limit=1800s (30 min)    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                    Calls service directly (inline loop)
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      EmailDeltaSyncService                                  │
│         File: src/service/integrations/email_delta_sync_service.py          │
│                                                                             │
│  Public Methods:                                                            │
│  • sync_integration(org_id, integration_id) - Entry point                   │
│                                                                             │
│  Private Methods:                                                           │
│  • _sync_integration() - Orchestrates folder + message sync                 │
│  • _sync_folders() - Fetch and sync folder structure                        │
│  • _get_folders_to_sync() - Filter to inbox/sent + monitored                │
│  • _sync_folder_messages() - Delta sync messages for one folder             │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                    Fetches data via Nango proxy
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Microsoft Graph API (via Nango)                         │
│       File: src/workers/integrations_worker/providers/email/outlook_email.py│
│                                                                             │
│  Endpoints Used:                                                            │
│  • GET /v1.0/me/mailFolders - Fetch folder structure                        │
│  • GET /v1.0/me/mailFolders/{id}/messages/delta - Delta sync messages       │
│                                                                             │
│  Returns: FetchMessagesResult(messages, delta_link)                         │
└─────────────────────────────────────────────────────────────────────────────┘

                              ┌────────────────────┐
                              │  TIER 3 (Manual)   │
                              └─────────┬──────────┘
                                        │
┌─────────────────────────────────────────────────────────────────────────────┐
│                      TIER 3: INTEGRATION TASK                               │
│                   sync_email_integration_task                               │
│       File: src/workers/integrations_worker/tasks/email/integration_task.py │
│                                                                             │
│  Triggered by:                                                              │
│  • Frontend "Refresh" button                                                │
│  • API endpoint for on-demand sync                                          │
│  • Manual intervention/debugging                                            │
│                                                                             │
│  NOT used by scheduled sync (Tier 1 + Tier 2 handle that)                   │
│  Config: max_retries=3, retry_delay=60s, soft_time_limit=300s               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Delta Query: How It Works

### The Problem with Time-Based Sync

Traditional approach (slow):
```
GET /v1.0/me/messages?$filter=receivedDateTime ge 2024-10-01
```
- Fetches ALL emails since the date
- Even if nothing changed, still fetches everything
- Includes full body content (huge payload)

### Delta Query Solution (fast)

Microsoft Graph Delta Query returns ONLY changes since last sync:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         INITIAL SYNC (First Time)                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Request:                                                                   │
│  GET /v1.0/me/mailFolders/{folder_id}/messages/delta                        │
│      ?$filter=receivedDateTime ge 2024-10-01                                │
│      &$select=id,subject,from,receivedDateTime,...                          │
│                                                                             │
│  Response (paginated):                                                      │
│  {                                                                          │
│    "value": [ /* all emails from last 90 days */ ],                         │
│    "@odata.nextLink": "...?$skiptoken=abc..."  ← More pages                 │
│  }                                                                          │
│                                                                             │
│  Final page response:                                                       │
│  {                                                                          │
│    "value": [ /* last batch */ ],                                           │
│    "@odata.deltaLink": "...?$deltatoken=xyz..."  ← SAVE THIS!               │
│  }                                                                          │
│                                                                             │
│  Action: Store deltaLink in connection_metadata                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                      INCREMENTAL SYNC (Subsequent)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Request:                                                                   │
│  GET {stored_deltaLink}                                                     │
│      (deltaLink contains folder, filters, everything encoded)               │
│                                                                             │
│  Response:                                                                  │
│  {                                                                          │
│    "value": [                                                               │
│      { "id": "msg1", "subject": "New email", ... },      ← NEW              │
│      { "id": "msg2", "@removed": {"reason": "deleted"} } ← DELETED          │
│    ],                                                                       │
│    "@odata.deltaLink": "...?$deltatoken=NEW..."  ← Updated token            │
│  }                                                                          │
│                                                                             │
│  Result: Only 2 items instead of thousands!                                 │
│  Action: Process changes, store new deltaLink                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Performance Comparison

| Scenario | Time-Based | Delta Query |
|----------|------------|-------------|
| Initial sync (90 days, 1000 emails) | ~30 seconds | ~30 seconds |
| Incremental (5 new emails) | ~30 seconds | **~2 seconds** |
| Incremental (no changes) | ~30 seconds | **~0.5 seconds** |

---

## DeltaLink Storage

DeltaLinks are stored **per-folder** in the integration's `connection_metadata`:

```python
# KnowledgeIntegration.connection_metadata structure:
{
    "email_folder_delta_links": {
        "AAMkAGI2TG9hZ...inbox_folder_id": "https://graph.microsoft.com/v1.0/me/mailFolders/.../messages/delta?$deltatoken=aWdub3JlLm...",
        "AAMkAGI2TG9hZ...sent_folder_id": "https://graph.microsoft.com/v1.0/me/mailFolders/.../messages/delta?$deltatoken=c2VudGl0ZW..."
    }
}
```

### DeltaLink Lifecycle

```text
1. First sync (no deltaLink stored)
   └── Fresh delta query with $filter=receivedDateTime ge {90_days_ago}
   └── Paginate through all results
   └── Store returned deltaLink

2. Subsequent syncs (deltaLink exists)
   └── Use stored deltaLink
   └── Get ONLY changes
   └── Store new deltaLink

3. DeltaLink expired (Microsoft returns 410 Gone)
   └── Automatically retry with fresh delta query
   └── Store new deltaLink
```

---

## Folder Sync Frequency

| Sync Type | Current | Recommended | Reason |
|-----------|---------|-------------|--------|
| **Folders** | Every 30 min | **Once per day** | Folders rarely change |
| **Messages** | Every 30 min | Every 30 min | Need near-real-time |

**Current behavior:** Folders sync every time email sync runs (every 30 min).

**Issue:** This is unnecessary - users rarely create/delete folders.

**Planned improvement:** Sync folders only:
- On first sync (initial setup)
- Once per day (daily refresh)
- On manual refresh (Tier 3 trigger)

This can be implemented by checking `integration.last_folder_synced_at` and skipping if < 24 hours old.

---

## Folder Monitoring

All folders are stored in the database, but only **monitored folders** have their messages synced. The `is_monitored` flag controls this behavior.

### Folder Types and Monitoring

| Folder Name | Folder Type | is_monitored | Messages Synced | Description |
|-------------|-------------|--------------|-----------------|-------------|
| **Inbox** | `inbox` | `true` | ✅ Yes | Main mailbox - incoming emails |
| **Sent Items** | `sent` | `true` | ✅ Yes | Sent emails |
| Drafts | `drafts` | `false` | ❌ No | Unsent draft emails |
| Deleted Items | `trash` | `false` | ❌ No | Deleted/trashed emails |
| Junk Email | `junk` | `false` | ❌ No | Spam folder |
| Archive | `archive` | `false` | ❌ No | Archived emails |
| Outbox | `user` | `false` | ❌ No | Temporary folder for emails being sent (moves to Sent Items) |
| Conversation History | `user` | `false` | ❌ No | Skype/Teams chat logs saved to mailbox (not actual emails) |
| Other custom folders | `user` | `false` | ❌ No | User-created folders |

### Default Monitoring Logic

```python
# In email_sync_service.py
MONITORED_FOLDER_TYPES = ("inbox", "sent")

# When syncing folders, is_monitored is set automatically:
is_monitored = "true" if folder_type in MONITORED_FOLDER_TYPES else "false"
```

### Sync Decision Logic

```python
def _get_folders_to_sync(session, integration_id):
    # Only sync folders where is_monitored = "true"
    return await session.execute(
        select(EmailFolder).where(
            EmailFolder.integration_id == integration_id,
            EmailFolder.is_deleted == False,
            EmailFolder.is_monitored == "true",  # Stored as string in DB
        )
    )
```

### User Control

Users can enable/disable monitoring for any folder:

```sql
-- Enable monitoring for a custom folder
UPDATE email_folders SET is_monitored = 'true' WHERE folder_name = 'Important Projects';

-- Disable monitoring for sent items (if not needed)
UPDATE email_folders SET is_monitored = 'false' WHERE folder_type = 'sent';
```

---

## "Other" Inbox Filtering (Focused Inbox)

Outlook's Focused Inbox classifies emails based on importance:

| Classification | Description | Action |
|----------------|-------------|--------|
| `focused` | Important emails from real people | **Synced** |
| `other` | Less important (newsletters, automated, no-reply) | **Skipped** |

### How It Works

Microsoft Graph returns `inferenceClassification` field on each email:

```json
{
    "id": "AAMkAGI2...",
    "subject": "Weekly Newsletter",
    "inferenceClassification": "other",  // ← Less important
    ...
}
```

### Filtering Logic

```python
# In outlook_email.py - _filter_other_inbox_messages()
def _filter_other_inbox_messages(messages):
    filtered = []
    for message in messages:
        # Always keep deleted messages (for proper deletion handling)
        if "@removed" in message:
            filtered.append(message)
            continue

        # Skip "other" inbox messages (less important)
        if message.get("inferenceClassification", "").lower() == "other":
            continue  # Skip newsletters, automated emails

        filtered.append(message)
    return filtered
```

### Benefits

- **Reduces noise**: Skips newsletters, marketing emails, automated notifications
- **Focuses on business emails**: Only syncs emails Outlook considers important
- **Improves performance**: Fewer emails to process and store
- **Maintains consistency**: Deleted emails are always processed regardless of classification

### Key Log Messages

```
"Outlook Email: skipped %d 'Other' inbox messages (less important)"
```

---

## Body Content Handling

### What's Fetched During Sync

```python
# Fields included in delta query:
"$select": "id,conversationId,subject,bodyPreview,body,from,toRecipients,ccRecipients,bccRecipients,replyTo,sentDateTime,receivedDateTime,hasAttachments,importance,isRead,isDraft,internetMessageId,parentFolderId,meetingMessageType,inferenceClassification"
```

### Fields in Database

| Field | Source | Size | Notes |
|-------|--------|------|-------|
| `snippet` | `bodyPreview` | ~255 chars | Short preview |
| `body_text` | `body.content` (text) | 0-100KB | Full content if text |
| `body_html` | `body.content` (html) | 0-100KB | Full content if HTML |

---

## Deleted Email Handling

Delta query returns deleted emails with `@removed` annotation:

```json
{
    "id": "AAMkAGI2...",
    "@removed": {
        "reason": "deleted"
    }
}
```

### Processing Flow

```python
# In outlook_email.py:
if "@removed" in item:
    _mark_as_deleted(item)  # Adds _sync_metadata.sync_action = "DELETED"

# In email_sync_service.py:
if sync_action == SyncAction.DELETED:
    # Mark email as deleted in database
    existing_email.is_deleted = True
```

---

## File Reference

| Component | File Path |
|-----------|-----------|
| **Tier 1 Task** | `src/workers/integrations_worker/tasks/email/all_orgs_task.py` |
| **Tier 2 Task** | `src/workers/integrations_worker/tasks/email/org_task.py` |
| **Tier 3 Task** | `src/workers/integrations_worker/tasks/email/integration_task.py` |
| **Email Module Init** | `src/workers/integrations_worker/tasks/email/__init__.py` |
| **Delta Sync Service** | `src/service/integrations/email_delta_sync_service.py` |
| **Email Sync Service** | `src/service/integrations/email_sync_service.py` |
| **Outlook Provider** | `src/workers/integrations_worker/providers/email/outlook_email.py` |
| **Celery Config** | `src/workers/integrations_worker/config.py` |
| **Celery App** | `src/workers/integrations_worker/celery_app.py` |

---

## Celery Configuration

### Task Routes

```python
# config.py
"integrations.sync_email_all_orgs": {
    "queue": "integrations",
    "routing_key": "integrations",
},
"integrations.sync_email_org": {
    "queue": "integrations",
    "routing_key": "integrations",
},
"integrations.sync_email_integration": {
    "queue": "integrations_priority",  # Priority queue for manual triggers
    "routing_key": "integrations_priority",
},
```

### Beat Schedule

```python
# config.py
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

### Time Limits

| Task | Soft Limit | Hard Limit | Rationale |
|------|------------|------------|-----------|
| Tier 1 (all_orgs) | 240s (default) | 300s | Just dispatches, completes quickly |
| Tier 2 (org) | 1800s (30 min) | 1860s | May have multiple integrations with full body |
| Tier 3 (integration) | 300s (5 min) | 360s | Single integration sync |

---

## Data Flow Example

### Scenario: Scheduled sync for Organization with 1 Outlook Email integration

```text
1. Celery Beat fires at :00 or :30 minute
   └── sync_email_all_orgs_task() triggered

2. Tier 1 queries master DB
   └── Returns: [Org_A, Org_B] (approved orgs)

3. Tier 1 dispatches Tier 2 for each org:
   └── sync_email_org_task.apply_async(organisation_id="org-a-uuid")
   └── sync_email_org_task.apply_async(organisation_id="org-b-uuid")

4. Tier 2 for Org_A executes:
   └── Query: active email integrations for Org_A
   └── Returns: [Integration_1 (outlook_email)]

5. For Integration_1, service orchestrates:
   └── EmailDeltaSyncService.sync_integration(org_id, integration_id)

   5a. Sync folders:
       └── GET /v1.0/me/mailFolders
       └── Upsert to email_folders table

   5b. Get folders to sync messages:
       └── Filter: folder_type IN ("inbox", "sent") OR is_monitored=True
       └── Returns: [Inbox, Sent Items]

   5c. For each folder, delta sync messages:
       └── Check: deltaLink stored for this folder?

       If NO deltaLink (first sync):
         └── GET /v1.0/me/mailFolders/{id}/messages/delta?$filter=receivedDateTime ge {90_days_ago}
         └── Paginate through all results (follow @odata.nextLink)
         └── Store @odata.deltaLink in connection_metadata

       If YES deltaLink (incremental):
         └── GET {stored_deltaLink}
         └── Returns: only new/deleted emails
         └── Store new deltaLink

       └── Upsert messages to emails table
       └── Handle @removed items (mark deleted)

6. Results aggregated and returned:
   └── {
         "status": "completed",
         "organisation_id": "org-a-uuid",
         "summary": {
           "total_integrations": 1,
           "successful": 1,
           "total_folders_synced": 5,
           "total_messages_synced": 3
         }
       }
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
        {"folder_name": "Inbox", "folder_type": "inbox", "messages_synced": 100, "is_initial": false},
        {"folder_name": "Sent Items", "folder_type": "sent", "messages_synced": 50, "is_initial": false}
    ]
}
```

---

## Error Handling

### DeltaLink Expiration

Microsoft deltaLinks can expire. The system handles this automatically:

```python
# outlook_email.py
try:
    resp = await nango.proxy_request(endpoint, params)
except Exception as e:
    if "410" in str(e) or "sync state" in str(e):
        # DeltaLink expired - retry with fresh delta query
        return await fetch_messages(
            ...,
            delta_link=None,  # Force fresh query
        )
```

### Retry Strategy

| Task | Max Retries | Delay | Behavior |
|------|-------------|-------|----------|
| Tier 1 | 0 | N/A | No retry (next scheduled run handles) |
| Tier 2 | 2 | 120s | Retries org-level failures |
| Tier 3 | 3 | 60s | Retries integration-level failures |

---

## Monitoring & Debugging

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

### Check Sync Status

```sql
-- View recent sync history
SELECT * FROM integration_sync_history
WHERE integration_id = 'uuid'
ORDER BY started_at DESC
LIMIT 10;

-- Check deltaLinks stored
SELECT
    integration_id,
    connection_metadata->'email_folder_delta_links' as delta_links
FROM knowledge_integrations
WHERE integration_type = 'outlook_email';

-- View synced emails
SELECT folder_id, COUNT(*) as email_count
FROM emails
WHERE integration_id = 'uuid'
GROUP BY folder_id;
```

### Key Log Messages

```
# Tier 1
"Starting email sync for all organizations (Tier 1)"
"Found %d approved organization(s) for email sync"
"Dispatched email sync for org: %s (task_id=%s)"

# Tier 2
"Starting email sync for organization (Tier 2): %s"
"Found %d active email integration(s) for org: %s"
"Email sync complete for org %s: successful=%d failed=%d"

# Delta Query
"Outlook Email: fresh delta query for folder=%s (since=%s)"
"Outlook Email: using stored deltaLink for folder (true incremental)"
"Delta sync complete after %d pages, new deltaLink received"
"Outlook Email fetch complete: %d messages (%d after filtering, initial=%s, used_delta_link=%s)"

# Filtering
"Outlook Email: skipped %d 'Other' inbox messages (less important)"

# Deletions
"Delta sync: detected deleted message id=%s reason=%s"
```

---

## Comparison: Email vs Calendar Sync

| Aspect | Email Sync | Calendar Sync |
|--------|------------|---------------|
| **Architecture** | Three-tier (dedicated tasks) | Single coordinator + dispatcher |
| **Service** | Dedicated EmailDeltaSyncService | Uses generic sync_integration_data_task |
| **Delta Query** | Per-folder deltaLinks | Single deltaLink per integration |
| **Folders** | Syncs inbox + sent (configurable) | N/A (single calendar) |
| **Body Content** | Excluded (on-demand) | Full event details included |
| **Schedule** | Every 30 minutes | Every 15 minutes |

---

## Related Documentation

- [Scheduled Sync Architecture Guide](./scheduled-sync-architecture.md) - Three-tier architecture overview
- [Calendar Sync Flow](./calendar-sync-flow.md) - Calendar implementation for comparison
- [Email Delta Sync Plan](../../Planning%20Documents/email-delta-sync-plan.md) - Original planning document
- [Microsoft Graph Delta Query](https://learn.microsoft.com/en-us/graph/delta-query-messages) - API documentation

---

## Planned Improvements

| Improvement | Status | Description |
|-------------|--------|-------------|
| Folder sync once/day | Planned | Skip folder sync if < 24 hours old |
| Filter meeting invites | Consider | Skip emails with `meetingMessageType` (already in calendar) |
| Filter no-reply emails | Consider | Skip automated emails from noreply@ addresses |
| "Other" inbox filtering | **Done** | Skip `inferenceClassification = "other"` emails |
| Full body content | **Done** | Body included in sync |
| Folder monitoring flag | **Done** | `is_monitored` controls which folders sync messages |

---

## Summary

The email sync system provides:

1. **Efficiency**: Delta Query fetches only changes (near-instant incremental syncs)
2. **Scalability**: Three-tier architecture isolates org/integration failures
3. **Reliability**: Per-folder deltaLinks survive partial failures
4. **Focused Content**: "Other" inbox emails filtered out (newsletters, automated)
5. **Folder Control**: `is_monitored` flag controls which folders sync (inbox/sent by default)
6. **Complete Data**: Full email body content included in sync
7. **Flexibility**: Tier 3 available for manual/on-demand sync
8. **Observability**: Comprehensive logging and result tracking
