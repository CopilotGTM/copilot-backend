# Three-Tier Sync Architecture

## Overview

All scheduled sync tasks follow a three-tier Celery architecture:

This is the default scheduled-sync model for calendar, email, messaging, and token refresh.
HubSpot and Salesforce CRM post-connect initial sync are the main exception: they run as provider-specific Tier 3 chains rather than the generic Tier 1 -> Tier 2 -> Tier 3 flow.

```
Tier 1: all_orgs_task (coordinator)
  - Queries approved orgs from master DB
  - Dispatches one Tier 2 task per org
  - Soft limit: 120s

Tier 2: org_task (per-org worker)
  - Queries active integrations for the org
  - Syncs each integration inline (not dispatched)
  - Records sync health (success/failure/circuit breaker)
  - Soft limit: varies by type (600s-1800s)

Tier 3: integration_task (manual/on-demand)
  - Triggered by frontend "Refresh" or OAuth callback
  - Syncs a single integration
  - Soft limit: 900s (allows initial sync)
```

## Fixed Schedule

Configured in `src/workers/integrations_worker/scheduled_jobs.py` and dispatched by `src/workers/integrations_worker/cron_dispatch.py`:

| Task | Schedule | Tier |
|------|----------|------|
| Token refresh | Every 4 hours | Tier 1 |
| Calendar sync | Every 15 minutes | Tier 1 |
| Email sync | Every 30 minutes | Tier 1 |
| Deal messages sync | Every 30 minutes | Tier 1 |
| Messaging channels sync | Once daily at 6 AM UTC | Tier 1 |

## Task Time Limits

| Tier | Soft Limit | Hard Limit | Retries |
|------|-----------|-----------|---------|
| **Tier 1** (all_orgs) | 120s | 180s | 0 |
| **Tier 2** (org) - Calendar | 600s | 660s | 2 |
| **Tier 2** (org) - Email | 1800s | 1860s | 2 |
| **Tier 2** (org) - Messaging | 900s | 960s | 2 |
| **Tier 3** (integration) - All | 900s | 960s | 3 |

## Session Management (Fresh Session Pattern)

Sync tasks never hold a database session during external API calls:

```
Phase 1: Load integration data
  async for session in acquire_org_async_session(org_id):
      extract needed data into dataclass
  # Session released

Phase 2: Fetch from provider API (NO session held)
  data = await fetch_from_provider(token_info, ...)

Phase 3: Write to database
  async for session in acquire_org_async_session(org_id):
      service = SyncService(session, org_id)
      await service.sync(integration, data)
  # Session released

Phase 4: Update integration metadata
  async for session in acquire_org_async_session(org_id):
      integration.last_synced_at = now
      await session.commit()
  # Session released
```

**Why**: Database connections are a limited resource. Provider API calls can take seconds to minutes (pagination, backfill). Holding a session during these calls would exhaust the connection pool.

## Sync History Tracking

Every sync run creates an `IntegrationSyncHistory` record via `SyncHistoryManager`:

```python
sync_history = await SyncHistoryManager.start(integration_id)
# ... do sync ...
await SyncHistoryManager.complete(
    sync_history,
    status=SyncStatus.SUCCESS,
    records_processed=150,
    records_failed=2,
    metadata={"created_count": 100, "updated": 50, "provider": "outlook"},
)
```

Fields tracked:
- `status`: SUCCESS, FAILED, PARTIAL
- `records_processed`, `records_failed`
- `started_at`, `completed_at`, `duration_ms`
- `sync_metadata`: JSONB with provider-specific details
- `error_details`: JSONB on failure

## Full vs Incremental Sync

The decision is made per-folder/per-channel based on whether a sync cursor exists:

| Provider | Cursor | Initial (no cursor) | Incremental (has cursor) |
|----------|--------|---------------------|--------------------------|
| Outlook Email | deltaLink | Fetch all messages via delta query | Use stored deltaLink |
| Outlook Calendar | deltaLink | calendarView/delta (90 days back, 1 year forward) | Use stored deltaLink |
| Gmail | historyId | Fetch all messages | Use history.list with historyId |
| Google Calendar | syncToken | Full calendar list | Use syncToken |
| Teams Channels | deltaLink | messages/delta for each channel | Use stored deltaLink |
| Teams Chats | N/A (no delta) | Pagination with date cutoff | Same, narrower cutoff |

**Both paths use ON CONFLICT DO UPDATE** — neither deletes existing data. Full sync upserts every record. Incremental sync only upserts changed records (with change detection to skip unchanged ones).

### Cursor Storage

- **Outlook delta links**: `KnowledgeIntegration.connection_metadata["email_folder_delta_links"][folder_id]`
- **Gmail history ID**: `KnowledgeIntegration.connection_metadata["gmail_history_id"]`
- **Calendar delta links**: `KnowledgeIntegration.connection_metadata["calendar_delta_link"]`
- **Teams channel delta links**: `MessagingChannel.sync_cursor`

### Cursor Expiry

When a stored cursor expires (provider returns 410 Gone or equivalent):
- **Outlook**: `DeltaExpiredError` caught in fetcher, retries with `delta_link=None` (fresh query)
- **Gmail**: 400/404 on history.list, falls back to full fetch
- Centralized in `_delta_sync.py` for Microsoft providers
