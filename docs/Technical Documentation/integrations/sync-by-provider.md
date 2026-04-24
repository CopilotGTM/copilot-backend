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
Only `inbox` and `sent` folders are synced by default (`is_monitored = "true"`). Users can enable/disable monitoring for any folder.

### Outlook-Specific
- **Delta query**: `GET /v1.0/me/mailFolders/{id}/messages/delta`
- **Body backfill**: Body is excluded from `$select` in delta query (same reason as calendar). After pagination, bodies are backfilled in a second pass via individual message GET calls.
- **"Other" filtering**: Outlook's `inferenceClassification == "other"` messages (newsletters, etc.) are filtered out of inbox sync.

### Gmail-Specific
- **History API**: `GET /gmail/v1/users/me/messages` for initial; `history.list` with `historyId` for incremental
- **History expiry**: If `historyId` is too old (400/404), falls back to full fetch
- **Label-based**: Messages are fetched per-label (INBOX, SENT); label ID is injected into message payload as `_gmail_label_id`

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

- **Custom OAuth**: Supported via the current first-party provider architecture
- **Deal schema sync**: Supported
- **Deal record sync**: Deferred
- **Current behavior after connect**: Integration is stored as active; schema sync is implemented as the first CRM sync slice, but is not auto-dispatched from the OAuth callback flow yet
- **Registry storage**: Provider-backed deal schema rows are mirrored into `crm_custom_field_definitions` with `field_source=provider` and scoped by `integration_id`
- **Current placeholder metadata**: HubSpot integrations still receive `initial_sync_status = not_implemented` during post-connect state handling because the callback flow does not auto-dispatch schema sync yet

## Salesforce CRM

- **Custom OAuth**: Supported via the current first-party provider architecture
- **Data sync**: Deferred
- **Current behavior after connect**: Integration is stored as active and marked with an initial-sync placeholder in `connection_metadata`
- **Current placeholder metadata**:
  - `initial_sync_status = not_implemented`
  - `initial_sync_placeholder = Salesforce custom auth connected successfully; CRM sync dispatch will be added later.`
