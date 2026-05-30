# Sync by Provider

## Calendar Sync

### Providers
- **Outlook Calendar**: Microsoft Graph calendarView/delta API
- **Google Calendar**: Google Calendar API with syncToken

### Data Model (Org-Scoped)

```
CalendarEvent (OrgScopedRecordMixin — no integration_id)
  ├── CalendarEventUserState (per-integration visibility)
  ├── CalendarAttendee (unique per event + email)
  └── CalendarEventDealAssociation (event-to-deal matching)
```

CalendarEvent is org-scoped: multiple users' calendars contribute to the same canonical event (matched by `external_id` + `occurrence_anchor`). Per-integration tracking is via `CalendarEventUserState`.

### Sync Flow

1. **Fetch**: Provider returns events (delta or full)
2. **Upsert**: `CalendarSyncService._upsert_event()` — ON CONFLICT on `(external_id, occurrence_anchor)`
3. **Attendees**: Upserted per event, matched by email
4. **User state**: `CalendarEventUserState` tracks which integration synced which event
5. **Deal matching**: Dispatched as a separate task after calendar sync completes

### Outlook-Specific
- **Delta query**: `GET /v1.0/me/calendarView/delta` with date range (90 days back, 1 year forward)
- **Body excluded from delta $select**: Including body prevents Microsoft from returning `@odata.deltaLink`; body is NOT needed for calendar events
- **iCalUId hydration**: Some delta responses omit `iCalUId`; a second API call hydrates these for cross-provider matching

### Google-Specific
- **Sync tokens**: Google Calendar API returns a `syncToken` after full sync; subsequent requests use it for incremental changes
- Uses `call_with_token_retry` for automatic 401 recovery

### Google Calendar Webhooks

Google Calendar uses event channels as low-latency triggers. Webhook payloads
are not persisted directly. After the backend validates the stored channel id,
resource id, and channel token, it schedules the existing single-integration
incremental sync task:

- Google Calendar: `integrations.sync_calendar_integration`

The sync task remains the only writer for provider data and continues to use
the existing Google Calendar sync token metadata.

Calendar subscription metadata is stored in
`connection_metadata.google_webhook_subscriptions.calendar` and renewed by
`integrations.renew_google_subscriptions_all`, which dispatches the
integration-scoped `integrations.ensure_google_subscription` task for each
active Google Calendar integration.

---

## Email Sync

### Providers
- **Outlook Email**: Microsoft Graph messages/delta API
- **Gmail**: Google Gmail API with history.list

### Data Model

```
EmailFolder (per-integration)
  └── Email (per-integration, unique on integration_id + external_message_id)
```

Email is NOT org-scoped — each integration owns its own email records. This is intentional: email content is user-private.

### Sync Flow (EmailDeltaSyncService)

Orchestrated by `EmailDeltaSyncService` using the fresh session pattern:

```
Phase 1: Load integration data → release session
Phase 2: Fetch folders from API (if due — 24h refresh interval) → no session
Phase 3: Write folders + get monitored folders → fresh session
Phase 4: For each monitored folder:
  a. Fetch messages from API → no session
  b. Write messages to DB → fresh session
Phase 5: Persist delta links + update last_synced_at → fresh session
```

### Monitored Folders
Email folder discovery stores provider folder/label metadata first. Message sync then runs only for folders where `email_folders.is_monitored == "true"`.

Default monitored folders:

| Provider | Provider folder/label | Normalized folder type | Message sync default |
| --- | --- | --- | --- |
| Outlook | `Inbox` | `inbox` | Synced |
| Outlook | `Sent Items`, `Sent` | `sent` | Synced |
| Gmail | `INBOX` | `inbox` | Synced |
| Gmail | `SENT` | `sent` | Synced |

Default non-monitored folders:

| Provider | Provider folder/label | Normalized folder type | Default behavior |
| --- | --- | --- | --- |
| Outlook | `Drafts` | `drafts` | Folder row stored, messages not synced |
| Outlook | `Deleted Items`, `Trash` | `trash` | Folder row stored, messages not synced |
| Outlook | `Junk Email`, `Junk`, `Spam` | `junk` | Folder row stored, messages not synced |
| Outlook | `Archive` | `archive` | Folder row stored, messages not synced |
| Outlook | Custom folders | `user` | Folder row stored, messages not synced |
| Gmail | `DRAFT` | `drafts` | Label row stored, messages not synced |
| Gmail | `TRASH` | `trash` | Label row stored, messages not synced |
| Gmail | `SPAM` | `junk` | Label row stored, messages not synced |
| Gmail | `STARRED` | `starred` | Label row stored, messages not synced |
| Gmail | `IMPORTANT` | `important` | Label row stored, messages not synced |
| Gmail | `CATEGORY_*` labels | `category` | Label row stored, messages not synced |
| Gmail | Custom labels | `user` | Label row stored, messages not synced |

Users can enable/disable monitoring for any stored folder by changing `is_monitored`. The default scope is intentionally not a full mailbox mirror.

### Outlook-Specific
- **Delta query**: `GET /v1.0/me/mailFolders/{id}/messages/delta`
- **Body backfill**: Body is excluded from `$select` in delta query (same reason as calendar). After pagination, bodies are backfilled in a second pass via individual message GET calls.
- **"Other" filtering**: Outlook's `inferenceClassification == "other"` messages (newsletters, automated mail, etc.) are filtered out of message sync. Deleted markers are preserved so the local state can still process removals.

### Microsoft Graph Webhooks

Outlook Calendar and Outlook Email use Microsoft Graph change notifications as
low-latency triggers. Webhook payloads are not persisted directly. After the
backend validates the stored `clientState` and subscription id, it schedules
the existing single-integration incremental sync task:

- Outlook Calendar: `integrations.sync_calendar_integration`
- Outlook Email: `integrations.sync_email_integration`

The sync task remains the only writer for provider data. Calendar continues to
use `connection_metadata.calendar_delta_link`; email continues to use
`connection_metadata.email_folder_delta_links`.

Subscriptions are stored in
`connection_metadata.microsoft_graph_subscriptions` and renewed by
`integrations.renew_microsoft_graph_subscriptions_all`, which dispatches the
integration-scoped `integrations.ensure_microsoft_graph_subscription` task for
each active Outlook Calendar or Outlook Email integration.

Scheduled all-org calendar/email polling is disabled by default for Outlook.
After the first sync stores the Microsoft delta cursors, Graph webhooks are the
low-latency trigger that schedules the integration-level delta sync. Manual or
API-triggered all-org sync remains available for repair/backfill.

### Gmail-Specific
- **History API**: `GET /gmail/v1/users/me/messages` for initial; `history.list` with `historyId` for incremental
- **History expiry**: If `historyId` is too old (400/404), falls back to full fetch
- **Label-based**: Messages are fetched per-label (INBOX, SENT); label ID is injected into message payload as `_gmail_label_id`
- **Category filtering**: Gmail inbox sync excludes `CATEGORY_PROMOTIONS`, `CATEGORY_SOCIAL`, `CATEGORY_UPDATES`, and `CATEGORY_FORUMS` using the Gmail query where possible, with a post-fetch safety filter for any category messages that still appear.

### Gmail Webhooks

Gmail uses Cloud Pub/Sub push notifications as low-latency triggers. Webhook
payloads are not persisted directly. Because Pub/Sub push endpoints are static,
the backend routes Gmail notifications by decoded account `emailAddress` and
matches active Gmail integrations by `integration_email`. It then schedules the
existing single-integration incremental sync task:

- Gmail: `integrations.sync_email_integration`

The sync task remains the only writer for provider data and continues to use
the existing Gmail history cursor metadata.

The Pub/Sub push endpoint must include the configured
`GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN`, and the received Pub/Sub envelope must match
`GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION`, before any integration lookup or sync
enqueue occurs.

Gmail watch metadata is stored in
`connection_metadata.google_webhook_subscriptions.gmail` and renewed by
`integrations.renew_google_subscriptions_all`, which dispatches the
integration-scoped `integrations.ensure_google_subscription` task for each
active Gmail integration.

### Change Detection (Incremental)
`sync_messages_incremental` batch-loads existing messages by external_id, then compares key fields (subject, from_email, is_read, labels, parent folder) to skip unchanged records.

---

## Messaging Sync (Teams + Slack)

### Providers
- **Microsoft Teams**: Microsoft Graph API (channels, chats, messages)
- **Slack**: Slack Web API (`conversations.*`, `users.list`, `auth.test`, `team.info`)

### Data Model (Org-Scoped)

```
MessagingChannel (OrgScopedRecordMixin — no integration_id)
  ├── MessagingChannelMember (direct identity: external_user_id, email, display_name)
  ├── MessagingMessage (direct identity: author_email, author_display_name)
  └── MessagingChannelIntegrationState (per-integration visibility)
```

Messaging is org-scoped: the same Slack/Teams channel appears once regardless of how many
users have connected that provider. Per-integration tracking is via
`MessagingChannelIntegrationState`.

Slack integrations are still **user-scoped**. Canonical channels/messages stay org-scoped,
while `MessagingChannelIntegrationState` records which user integration can access each channel.

### Two Sync Phases

**Phase 1: Channel/Chat sync** (Tier 2: `channels_org_task`)
- Fetch provider channels/chats for every active user integration
- Upsert via `MessagingSyncService.sync_channels_incremental()`

**Phase 2: Deal message sync** (Tier 2: `deal_messages_org_task`)
- For each deal with linked messaging channels
- Fetch messages from those channels
- Upsert via `DealChannelMessagesService`

### Slack Channels and Chats
- **Channel discovery**: `conversations.list` for `public_channel` and `private_channel`
- **Group chats**: `conversations.list` for `mpim`
- **Workspace namespacing**: canonical Slack channel IDs are stored as `{team_id}:{channel_id}`
  so the same raw channel ID from different workspaces cannot collide
- **Members**: `conversations.members` + `users.list`
- 1:1 DMs are not synced into deal-channel workflows

### Teams Channels
- **Delta query**: `GET /v1.0/teams/{teamId}/channels/{channelId}/messages/delta`
- Each channel stores its own `sync_cursor` (delta link)
- Delta expiry handled via `DeltaExpiredError` → fresh sync

### Teams Chats
- **No delta support**: Microsoft Graph does NOT support delta queries for chat messages
- Uses standard pagination (`GET /v1.0/me/chats`) ordered by `lastMessagePreview/createdDateTime desc`
- Client-side date cutoff stops pagination when chats are older than the lookback window
- Lookback: 90 days for initial sync, 7 days for recurring sync
- 1:1 chats (`oneOnOne`) are filtered out

### Slack Messages
- **History API**: `conversations.history`
- **Thread replies**: `conversations.replies`
- **Incremental cutoff**: last synced timestamp with a configurable safety buffer
- Lookback: 90 days for initial sync by default, configurable via environment variables
- Messages are normalized into the same org-scoped `messaging_messages` table as Teams

### Member Sync
Members are batch-loaded per channel (one SELECT per channel, not per member) to avoid N+1 queries. Upserted by `(channel_id, external_user_id)`.

---

## HubSpot CRM

- **Custom OAuth**: Supported.
- **Live chain**: `schema -> pipelines -> users -> companies -> contacts -> deals -> tasks -> notes -> finalize`
- **Schema refresh**:
  - `integrations.sync_hubspot_deal_schema`
  - `integrations.sync_hubspot_deal_schema_all_orgs`
- **Current capture**:
  - schema registry for deal fields
  - pipelines + stages
  - users
  - companies
  - contacts
  - deals
  - deal stage history
  - deal participants
  - object associations
  - `crm_activities` for tasks and notes
- **Direction**: strictly HubSpot -> CopilotGTM.
- **Reference**: see [crm-initial-sync.md](crm-initial-sync.md) for metadata and captured tables.

## Salesforce CRM

- **Custom OAuth**: Supported.
- **Live chain**: `schema -> pipelines -> users -> companies -> contacts -> deals -> tasks -> notes -> finalize`
- **Pipeline model**:
  - pipeline = `Opportunity RecordType`
  - stage = record-type-scoped `StageName`
- **Current capture boundary**:
  - Opportunity schema
  - pipelines + stages
  - users
  - companies
  - contacts
  - deals
  - deal stage history
  - deal participants
  - deal object associations
  - crm activities for tasks and notes
- **Deferred**: broader Salesforce association slices beyond direct deal-company/contact edges.
- **Direction**: strictly Salesforce -> CopilotGTM.
- **Reference**: see [crm-initial-sync.md](crm-initial-sync.md) for metadata and captured tables.
