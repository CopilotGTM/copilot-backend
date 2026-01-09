# Calendar Sync Flow - Google & Outlook

**Last Updated**: January 2026
**Status**: Production Ready

## Overview

Calendar events from Google and Outlook are synced via Nango webhooks, processed by the integrations worker, and stored in our database. This document covers the sync flow for both providers.

---

## Quick Reference

| Aspect           | Google Calendar                         | Outlook Calendar              |
| ---------------- | --------------------------------------- | ----------------------------- |
| API              | Google Calendar API v3                  | Microsoft Graph API           |
| Endpoint         | `/calendar/v3/calendars/primary/events` | `/v1.0/me/calendarView/delta` |
| Incremental Sync | `updatedMin` timestamp                  | Delta tokens                  |
| Deleted Events   | `showDeleted=true`                      | `@removed` annotation         |

---

## Sync Flow Diagram

```text
┌──────────────────┐
│  User Calendar   │  Event created/updated/deleted
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
│ Google │ │ Outlook │
│Fetcher │ │ Fetcher │
└────┬───┘ └────┬────┘
     └────┬─────┘
          ▼
┌──────────────────┐
│ Calendar Adapter │  Upserts to calendar_events table
└──────────────────┘
```

---

## Google Calendar Flow

**Provider File**: `src/workers/integrations_worker/providers/calendar/google_calendar.py`

### Google Initial Sync

- Fetches events from **90 days ago** to **365 days ahead**
- Parameters: `timeMin`, `timeMax`, `singleEvents=true`, `orderBy=startTime`
- Max 2500 results per page

### Google Incremental Sync

- Uses `updatedMin` parameter (last_synced_at minus 1 hour buffer)
- `showDeleted=true` to capture cancelled events
- Orders by `updated` timestamp

### Google Pagination

- Uses `nextPageToken` from response
- Continues until no more pages (max 50 pages safety limit)

### Google Token Handling

- OAuth tokens managed by Nango
- Nango auto-refreshes expired tokens before proxying requests

### Google Deleted Events

- Google returns cancelled events with `status: "cancelled"`
- Captured when `showDeleted=true` is set

---

## Outlook Calendar Flow

**Provider File**: `src/workers/integrations_worker/providers/calendar/outlook_calendar.py`

### Outlook Initial Sync (Fresh Delta Query)

- Uses `/v1.0/me/calendarView/delta` endpoint
- Date range: **90 days ago** to **365 days ahead**
- Returns full event properties (organizer, onlineMeeting, attendees)

### Outlook Incremental Sync (With Delta Link)

- Uses stored `deltaLink` from previous sync
- Returns **only changed events** since last sync
- Much more efficient than timestamp-based approach

### Outlook Pagination

- Uses `@odata.nextLink` for pagination
- Final page returns `@odata.deltaLink` for next sync

### Outlook Token Handling

- OAuth tokens managed by Nango
- Delta links can expire - system auto-retries with fresh query on:
  - HTTP 410 Gone
  - Error codes: `syncStateNotFound`, `resyncRequired`

### Outlook Deleted Events

- Microsoft returns `@removed` annotation for deleted events
- System transforms to `isCancelled=true` with sync metadata

### Outlook Old Event Filtering

- Incremental syncs filter events older than 90 days
- Deleted events are always kept regardless of age

---

## Key Differences

| Feature              | Google                          | Outlook                     |
| -------------------- | ------------------------------- | --------------------------- |
| Incremental approach | Re-fetches all modified events  | Only returns changed events |
| State storage        | `last_synced_at` timestamp      | `delta_link` token          |
| API efficiency       | Medium (timestamp filter)       | High (delta tokens)         |
| Deleted event format | `status: "cancelled"`           | `@removed` annotation       |

---

## Related Files

| Component        | Path                                                                     |
| ---------------- | ------------------------------------------------------------------------ |
| Google Fetcher   | `src/workers/integrations_worker/providers/calendar/google_calendar.py`  |
| Outlook Fetcher  | `src/workers/integrations_worker/providers/calendar/outlook_calendar.py` |
| Dispatcher       | `src/workers/integrations_worker/services/dispatcher.py`                 |
| Calendar Adapter | `src/workers/integrations_worker/adapters/calendar_adapter.py`           |
| Sync Task        | `src/workers/integrations_worker/tasks/sync_tasks.py`                    |

---

## Related Documentation

- [Data Sync Flow](./data-sync-flow.md) - General integration sync flow
- [Integration Data Sync Architecture](./Integration-data-sync-architecture.md) - System architecture
