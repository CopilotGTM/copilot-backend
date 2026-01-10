# Email Sync Flow - Outlook

**Last Updated**: January 2026
**Status**: Production Ready

## Overview

Email messages from Outlook/Office 365 are synced via Nango webhooks, processed by the integrations worker, and stored in our database. This document covers the current sync flow for Outlook email (Gmail email is not yet implemented).

---

## Quick Reference

| Aspect           | Outlook Email                                    |
| ---------------- | ------------------------------------------------ |
| API              | Microsoft Graph API                              |
| Folders Endpoint | `/v1.0/me/mailFolders`                           |
| Messages Endpoint| `/v1.0/me/messages`                              |
| Incremental Sync | `receivedDateTime ge <timestamp>` filter         |
| Pagination       | NOT implemented (max 999 items per request)      |
| Delta Tokens     | NOT used (unlike calendar)                       |

---

## Sync Flow Diagram

```text
┌──────────────────┐
│   User Mailbox   │  Email received/updated/deleted
│    (Outlook)     │
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
│  • Verify signature → Derive org → Enqueue task              │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  CELERY TASK: integrations.sync_integration_data             │
│  Queue: integrations                                         │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  DISPATCHER                                                  │
│  src/workers/integrations_worker/services/dispatcher.py      │
│  • Routes to provider fetcher → Stores via adapter           │
└────────┬─────────────────────────────────────────────────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐ ┌─────────┐
│ Folder │ │ Message │
│Fetcher │ │ Fetcher │
└────┬───┘ └────┬────┘
     └────┬─────┘
          ▼
┌──────────────────┐
│  Email Adapter   │  Upserts to email_folders, emails tables
└──────────────────┘
```

---

## Outlook Email Flow

**Provider File**: `src/workers/integrations_worker/providers/email/outlook_email.py`

### Outlook Folder Sync

- Fetches **ALL folders** on every sync (no incremental)
- Endpoint: `GET /v1.0/me/mailFolders`
- Max 250 folders per request
- Fields: `id`, `displayName`, `parentFolderId`, `childFolderCount`, `unreadItemCount`, `totalItemCount`, `isHidden`

### Outlook Message Sync (Initial)

- Fetches messages from **last 90 days**
- Endpoint: `GET /v1.0/me/messages`
- Filter: `receivedDateTime ge <90_days_ago>`
- Ordered by: `receivedDateTime desc`
- Max 999 items per request

### Outlook Message Sync (Incremental)

- Fetches messages since **last_synced_at minus 1 day** buffer
- Same endpoint and ordering as initial
- Uses `receivedDateTime ge <timestamp>` filter
- Does NOT capture deleted messages

### Outlook Pagination

**Current Limitation**: Pagination is NOT implemented.

- Microsoft Graph returns `@odata.nextLink` for additional pages
- Current implementation only fetches first page (max 999 items)
- This is a known limitation for high-volume mailboxes

### Outlook Token Handling

- OAuth tokens managed by Nango
- Nango auto-refreshes expired tokens before proxying requests

### Outlook Current Limitations

| Limitation              | Impact                                          |
| ----------------------- | ----------------------------------------------- |
| No pagination           | Only first 999 messages per sync                |
| No delta tokens         | Re-fetches overlapping data each sync           |
| No deletion tracking    | Deleted emails not detected                     |
| Timestamp-based only    | Less efficient than delta query                 |

---

## Key Differences vs Calendar Sync

| Feature              | Calendar (Outlook)              | Email (Outlook)                |
| -------------------- | ------------------------------- | ------------------------------ |
| Incremental approach | Delta tokens                    | Timestamp filter               |
| State storage        | `delta_link` token              | `last_synced_at` timestamp     |
| API efficiency       | High (only changes)             | Medium (overlapping data)      |
| Deleted items        | `@removed` annotation           | NOT tracked                    |
| Pagination           | Fully implemented               | NOT implemented                |

---

## Database Tables

| Table                    | Purpose                                   |
| ------------------------ | ----------------------------------------- |
| `email_folders`          | Stores folder metadata                    |
| `emails`                 | Stores email messages                     |
| `email_thread_summaries` | Stores conversation thread summaries      |
| `deal_email_associations`| Links emails to deals                     |

---

## Related Files

| Component        | Path                                                                 |
| ---------------- | -------------------------------------------------------------------- |
| Outlook Fetcher  | `src/workers/integrations_worker/providers/email/outlook_email.py`   |
| Dispatcher       | `src/workers/integrations_worker/services/dispatcher.py`             |
| Email Adapter    | `src/workers/integrations_worker/adapters/email_adapter.py`          |
| Sync Task        | `src/workers/integrations_worker/tasks/sync_tasks.py`                |
| Email Models     | `src/domain/models/organisation_tables/sync/email_models.py`         |

---

## Related Documentation

- [Calendar Sync Flow](./calendar-sync-flow.md) - Calendar sync with delta tokens
- [Data Sync Flow](./data-sync-flow.md) - General integration sync flow
- [Email Delta Sync Plan](../../Planning%20Documents/email-delta-sync-plan.md) - Planned delta enhancement
