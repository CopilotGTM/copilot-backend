# Task: Deal Channel Messages Sync Worker

## Status: TODO

## Problem Statement

Currently, in the deal section, users can select Teams channels and link them to deals. These channel-deal associations are stored in the `messaging_deal_links` table. However, we are NOT fetching the actual messages from these linked channels (including Team channels, group chats, and meeting chats).

Without message fetching, we cannot:

1. Show conversation context for deals
2. Extract insights from deal-related discussions
3. Track communication activity within deal channels

## Proposed Solution

Create a new KeyWorker task that runs **hourly** for each organisation to:

1. Find all channels linked to deals (via `messaging_deal_links`)
2. Fetch messages from those channels using Teams Graph API
3. Store messages in the existing `messaging_messages` table

### Task Structure (Following `trigger_timeline_sync_all_orgs` Pattern)

#### 1. Trigger Task (All Orgs)

```python
@app.task(name="deal.trigger_deal_channel_messages_sync_all_orgs")
def trigger_deal_channel_messages_sync_all_orgs() -> dict[str, int]:
    """Trigger channel message sync for all active organisations."""
```

#### 2. Per-Organisation Task

```python
@app.task(bind=True, name="deal.sync_deal_channel_messages", acks_late=True, max_retries=3)
def sync_deal_channel_messages_task(self: Task, organisation_id: str) -> dict[str, int]:
    """Sync messages for all deal-linked channels in an organisation."""
```

### Database Tables

#### Read From: `messaging_deal_links`

- Query channels linked to deals: `SELECT DISTINCT channel_id FROM messaging_deal_links WHERE channel_id IS NOT NULL`

#### Read From: `messaging_channels`

- Get channel metadata (external_channel_id, channel_type, channel_metadata for team_id)

#### Write To: `messaging_messages` (Already Exists)

| Column | Description |
|--------|-------------|
| message_id | UUID primary key |
| channel_id | FK to messaging_channels |
| external_message_id | Teams message ID |
| participant_id | Author (resolved) |
| content | Message text |
| sent_at | Timestamp |
| thread_id | For threaded replies |
| message_type | TEXT, SYSTEM_EVENT, etc. |
| reactions, attachments, mentions | JSONB metadata |

### Implementation Flow

```
1. Celery Beat (hourly) triggers: deal.trigger_deal_channel_messages_sync_all_orgs
   ↓
2. For each APPROVED organisation:
   - Enqueue: deal.sync_deal_channel_messages(organisation_id)
   ↓
3. Per-org task:
   a. Query messaging_deal_links to get unique channel_ids
   b. Query messaging_channels to get channel details
   c. For each channel:
      - If Team channel: call teams.fetch_messages(team_id, channel_id)
      - If Chat (group/meeting): call teams.fetch_chat_messages(chat_id)
   d. Upsert messages to messaging_messages table
   e. Update channel.last_synced_at
```

## Files to Modify

### 1. Create New Task File

**File:** `src/workers/deal_worker/tasks/deal_channel_messages_tasks.py`

New file containing:

- `trigger_deal_channel_messages_sync_all_orgs()` - Celery Beat trigger
- `sync_deal_channel_messages_task()` - Per-org worker

### 2. Update Worker Config

**File:** `src/workers/deal_worker/config.py`

Add to `task_routes`:

```python
"deal.trigger_deal_channel_messages_sync_all_orgs": {
    "queue": "deal",
    "routing_key": "deal",
},
"deal.sync_deal_channel_messages": {
    "queue": "deal",
    "routing_key": "deal",
},
```

Add to `beat_schedule`:

```python
"sync-deal-channel-messages-all-orgs": {
    "task": "deal.trigger_deal_channel_messages_sync_all_orgs",
    "schedule": crontab(minute=0),  # Run every hour at :00
    "options": {
        "queue": "deal",
        "routing_key": "deal",
    },
},
```

### 3. Create Service (Optional but Recommended)

**File:** `src/service/crm/deal_channel_messages_service.py`

Service class to encapsulate:

- Querying deal-linked channels
- Fetching messages from Teams API
- Storing messages in database

## Reference Files

| Purpose | File Path |
|---------|-----------|
| Task Pattern | `src/workers/deal_worker/tasks/deal_timeline_tasks.py` |
| Worker Config | `src/workers/deal_worker/config.py` |
| Messaging Models | `src/domain/models/organisation_tables/sync/messaging_models.py` |
| Teams Provider | `src/workers/integrations_worker/providers/messaging/teams.py` |
| Message Sync Service | `src/service/integrations/messaging_sync_service.py` |
| Deal Integration Routes | `src/api/routers/integrations/deal_integration_routes.py` |

## Key Teams API Functions (Already Implemented)

From `src/workers/integrations_worker/providers/messaging/teams.py`:

```python
# For Team channels (requires team_id from channel_metadata)
async def fetch_messages(
    nango_client: NangoClient,
    connection_id: str,
    team_id: str,
    channel_id: str,
    time_min: datetime | None = None,
) -> list[dict[str, Any]]

# For group chats and meeting chats
async def fetch_chat_messages(
    nango_client: NangoClient,
    connection_id: str,
    chat_id: str,
    time_min: datetime | None = None,
) -> list[dict[str, Any]]
```

## Schedule

- **Frequency:** Hourly (every hour at :00)
- **Initial sync:** Last 90 days of messages
- **Incremental sync:** Since last_synced_at minus 1-day buffer

## Success Criteria

1. Worker runs hourly for all approved organisations
2. Messages from deal-linked channels are stored in `messaging_messages`
3. Incremental sync works (only fetches new messages)
4. Error handling with exponential backoff retries

Once task is completed, move to review folder.
