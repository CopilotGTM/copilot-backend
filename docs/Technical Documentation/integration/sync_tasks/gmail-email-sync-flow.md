# Gmail Email Sync Flow

## Overview

Gmail email sync reuses the same three-tier architecture as Outlook email sync. The `EmailDeltaSyncService` routes to the correct provider (Gmail or Outlook) based on `IntegrationType`.

## Architecture

```
Tier 1: all_orgs_task  (org discovery)
  -> Tier 2: org_task  (per org, finds email integrations)
    -> Tier 3: integration_task -> EmailDeltaSyncService.sync_integration()
```

The service handles both providers transparently. Celery tasks are unchanged.

## Gmail API Flow

### Initial Sync
1. `fetch_labels()` - GET /gmail/v1/users/me/labels (+ detail per label for counts)
2. For each monitored label (INBOX, SENT):
   - `messages.list` with `q=after:{90_days_ago_epoch}` to get IDs
   - `messages.get` per ID (concurrent, semaphore=5) to get full message
3. Extract highest `historyId` from fetched messages as cursor

### Incremental Sync
1. `history.list` with `startHistoryId` - returns `messagesAdded` and `messagesDeleted`
2. `messages.get` for added message IDs
3. Deleted messages get `_sync_metadata.sync_action=DELETED` marker
4. New `historyId` from response becomes next cursor

### Expired historyId (404)
Gmail returns 404 when `historyId` is too old (7-30 day retention). On 404, the provider falls back to a full sync automatically (same pattern as Google Calendar `syncToken` expiry).

## Cursor Storage

| Provider | Cursor Type | Metadata Key | Scope |
|----------|------------|--------------|-------|
| Outlook | Delta link (URL) | `connection_metadata.email_folder_delta_links.{folder_id}` | Per-folder |
| Gmail | History ID (integer) | `connection_metadata.gmail_history_id` | Global (shared across labels) |

Gmail's `historyId` is global across the entire mailbox. When syncing multiple labels, the service persists the highest `historyId` from all label syncs.

## Label-to-Folder Mapping

| Gmail Label ID | folder_type | is_monitored |
|---------------|-------------|-------------|
| INBOX | inbox | true |
| SENT | sent | true |
| DRAFT | drafts | false |
| TRASH | trash | false |
| SPAM | junk | false |
| STARRED | starred | false |
| IMPORTANT | important | false |
| CATEGORY_* | category | false |
| User labels | user | false |

## Category Filtering

Gmail category labels (CATEGORY_PROMOTIONS, CATEGORY_SOCIAL, CATEGORY_UPDATES, CATEGORY_FORUMS) are equivalent to Outlook's "Other" inbox classification. Messages with these labels are filtered from INBOX sync to reduce noise. Filtering only applies to INBOX; SENT messages are never filtered.

## Key Files

| File | Purpose |
|------|---------|
| `src/workers/integrations_worker/providers/email/gmail_email.py` | Gmail API calls (labels, messages, history) |
| `src/service/integrations/email_sync_service.py` | Gmail field extraction (label->folder, message->email) |
| `src/service/integrations/email_delta_sync_service.py` | Provider routing, cursor management |
