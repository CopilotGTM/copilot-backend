# Deal Channel Messages Sync - Design Plan

## Status: PLANNING

## Problem Statement

Deal-linked Teams channels need their messages synced to enable conversation insights and deal context. Users manually select specific channels to link to deals via the UI, which creates `MessagingDealLink` records.

**Key Consideration:** The same Teams channel can be linked via multiple user integrations.

```
Scenario: "Sales - Acme Corp" Teams channel

User A connects Teams → Integration AAA → MessagingChannel row #1 → Links to Deal X
  integration_id: AAA
  external_channel_id: "19:abc123@thread.tacv2"

User B connects Teams → Integration BBB → MessagingChannel row #2 → Links to Deal X
  integration_id: BBB
  external_channel_id: "19:abc123@thread.tacv2"  ← SAME Teams channel!
```

We need to:
1. **Only sync channels that users have explicitly linked to deals** (via `MessagingDealLink`)
2. **Avoid duplicate fetches** for the same `external_channel_id`
3. **Distribute load across integrations** to avoid rate limits on a single user's token

## Existing Data Model

### Tables Involved

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `messaging_channels` | Stores channel metadata | `channel_id`, `integration_id`, `external_channel_id` |
| `messaging_deal_links` | **User-selected** channel-to-deal links | `channel_id`, `deal_id`, `integration_id`, `detection_method` |
| `messaging_messages` | Stores fetched messages | `message_id`, `channel_id`, `integration_id`, `external_message_id` |
| `knowledge_integrations` | User's Teams connections | `integration_id`, `connection_id`, `is_active` |

### How Channel-Deal Links Work

1. User goes to Deal page → clicks "Link Channel"
2. Frontend shows channels from user's Teams integration
3. User selects channel → `link_channel_to_deal()` creates:
   - `MessagingChannel` record (if not exists for that integration)
   - `MessagingDealLink` record linking channel to deal

**Important:** `detection_method = "manual"` for user-selected channels.

### Unique Constraints

| Table | Unique Constraint | Implication |
|-------|-------------------|-------------|
| `messaging_channels` | `(integration_id, external_channel_id)` | Same Teams channel = multiple rows per integration |
| `messaging_messages` | `(integration_id, external_message_id)` | Same message = multiple rows if fetched via different integrations |
| `messaging_deal_links` | None (just PK) | Links are per-integration |

### Key Insight

Messages are currently **scoped to integration_id**, meaning:
- Fetching via Integration A → stores with `integration_id: AAA`
- Fetching via Integration B → stores with `integration_id: BBB`
- **Duplicate storage for the same actual message**

## Design Options

### Option A: Single-Fetch per Unique Channel (Recommended)

**Approach:** Group channels by `external_channel_id`, pick ONE integration per unique channel, fetch once.

```python
# Pseudocode
unique_channels = (
    SELECT external_channel_id, 
           MIN(channel_id) as canonical_channel_id
    FROM messaging_channels c
    JOIN messaging_deal_links dl ON c.channel_id = dl.channel_id
    WHERE dl.is_deleted = false
    GROUP BY external_channel_id
)

for (external_channel_id, canonical_channel_id) in unique_channels:
    channel = get_channel(canonical_channel_id)
    integration = get_active_integration(channel.integration_id)
    
    # Fetch once via canonical integration
    messages = fetch_messages(integration, channel)
    
    # Store with canonical integration_id
    store_messages(messages, channel.integration_id)
```

**Pros:**
- No duplicate messages
- Efficient API usage
- Works with existing schema

**Cons:**
- If canonical integration's token expires, sync fails
- Other integration's deal links may not "see" the messages

**Mitigation:** Select integration with most recent activity/valid token

### Option B: Shared Messages with Multiple Integration References

**Approach:** Store messages once, track which integrations can access them.

```sql
-- New table to track message-integration visibility
CREATE TABLE messaging_message_integrations (
    message_id UUID REFERENCES messaging_messages(message_id),
    integration_id UUID REFERENCES knowledge_integrations(integration_id),
    PRIMARY KEY (message_id, integration_id)
);

-- Update messaging_messages unique constraint
ALTER TABLE messaging_messages 
DROP CONSTRAINT uq_msg_external;

ALTER TABLE messaging_messages 
ADD CONSTRAINT uq_msg_external_org UNIQUE (organisation_id, external_message_id);
```

**Pros:**
- True deduplication
- All integrations can "claim" access
- More accurate data model

**Cons:**
- Schema migration required
- More complex queries
- Breaking change to existing constraint

### Option C: Org-Level Messages (No Integration Scope)

**Approach:** Make `integration_id` nullable on messages, store at org level.

```python
# Messages are org-level, not integration-level
class MessagingMessage:
    integration_id: UUID | None  # Nullable - org-level data
    fetched_via_integration_id: UUID  # Track which integration fetched it
```

**Unique constraint:** `(organisation_id, external_message_id)`

**Pros:**
- Simplest deduplication
- Messages are truly shared

**Cons:**
- Major schema change
- Breaks `SyncedRecordMixin` pattern
- May affect existing queries

## Recommended Approach: Single-Fetch with Random Integration Selection

Use **Option A (Single-Fetch)** with **random integration selection** for load distribution:

### Why Random Selection?

1. **Rate Limit Distribution** - Microsoft Graph has per-user rate limits. Random selection spreads API calls across multiple user tokens.
2. **Token Resilience** - If one user's token expires, next sync picks a different integration.
3. **Fairness** - No single user bears all the API quota usage.

### Integration Selection Strategy

When multiple integrations have the same `external_channel_id`:

```python
import random

async def get_random_integration_for_channel(
    external_channel_id: str,
    session: AsyncSession,
) -> tuple[MessagingChannel, KnowledgeIntegration] | None:
    """
    Get a random valid integration for fetching messages from a channel.
    
    Randomization distributes load across users to avoid rate limiting.
    """
    
    stmt = (
        select(MessagingChannel, KnowledgeIntegration)
        .join(KnowledgeIntegration)
        .where(
            MessagingChannel.external_channel_id == external_channel_id,
            MessagingChannel.is_deleted == False,
            KnowledgeIntegration.is_active == True,
            KnowledgeIntegration.connection_id.isnot(None),
        )
    )
    
    result = await session.execute(stmt)
    valid_options = result.all()
    
    if not valid_options:
        return None
    
    # Random selection for load distribution
    return random.choice(valid_options)
```

### Handling Multiple Deal Links

After fetching messages via the canonical integration, link them to ALL deals that reference the same `external_channel_id`:

```python
# 1. Fetch messages for canonical channel
messages = await fetch_messages(canonical_channel)

# 2. Store messages with canonical integration_id
stored_messages = await store_messages(messages, canonical_channel.integration_id)

# 3. Get ALL deal links for this external_channel_id (any integration)
all_deal_links = await get_deal_links_by_external_channel_id(
    external_channel_id=canonical_channel.external_channel_id
)

# 4. For each deal link, ensure they can query the messages
# This works because channel_id is in messaging_deal_links,
# and messages are in messaging_messages with same channel_id
```

## Implementation Plan

### Architecture Overview

Follow the existing codebase patterns:
- **Tasks** (Celery) → Thin orchestration layer, delegates to services
- **Services** (`src/service/integrations/`) → Business logic, database operations
- **Providers** (`src/workers/integrations_worker/providers/`) → External API calls

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Celery Task                               │
│         deal_channel_messages_tasks.py (thin orchestrator)          │
│                                                                     │
│  - Triggers from Beat schedule                                      │
│  - Enqueues per-org tasks                                           │
│  - Delegates to service for actual work                             │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           Service Layer                             │
│         deal_channel_messages_service.py (business logic)           │
│                                                                     │
│  - get_unique_deal_linked_channels() - Query with deduplication     │
│  - sync_channel_messages() - Orchestrate fetch + store              │
│  - store_messages() - Database operations                           │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Provider Layer                             │
│      providers/messaging/teams.py (external API calls)              │
│                                                                     │
│  - fetch_messages() - Teams channel messages                        │
│  - fetch_chat_messages() - Teams chat messages                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Phase 1: Create the Service

**File:** `src/service/integrations/deal_channel_messages_service.py`

```python
class DealChannelMessagesService:
    """Service for syncing messages from deal-linked Teams channels."""
    
    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id
        self.nango = NangoClient()
    
    async def get_unique_deal_linked_channels(self) -> list[DealLinkedChannelInfo]:
        """Get unique deal-linked channels with random integration selection."""
        
    async def sync_channel_messages(
        self,
        channel_info: DealLinkedChannelInfo,
    ) -> ChannelSyncResult:
        """Fetch and store messages for a single channel."""
        
    async def sync_all_deal_channels(self) -> OrgSyncResult:
        """Sync all deal-linked channels for the organization."""
        
    async def _fetch_messages(
        self,
        channel_info: DealLinkedChannelInfo,
        integration: KnowledgeIntegration,
    ) -> list[dict[str, Any]]:
        """Fetch messages from Teams API via provider layer."""
        
    async def _store_messages(
        self,
        channel_id: UUID,
        integration_id: UUID,
        messages: list[dict[str, Any]],
    ) -> int:
        """Store fetched messages in database."""
```

### Phase 2: Update the Task (Thin Orchestrator)

**File:** `src/workers/integrations_worker/tasks/deal_channel_messages_tasks.py`

```python
@app.task(name="integrations.trigger_deal_channel_messages_sync_all_orgs")
def trigger_deal_channel_messages_sync_all_orgs() -> dict[str, int]:
    """Trigger deal channel message sync for all approved organisations."""
    # Same as before - enqueue per-org tasks
    
@app.task(bind=True, name="integrations.sync_deal_channel_messages")
def sync_deal_channel_messages_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Sync messages for deal-linked channels in an organisation."""
    
    async def _sync() -> dict[str, Any]:
        async for session in acquire_org_async_session(UUID(organisation_id)):
            service = DealChannelMessagesService(session, UUID(organisation_id))
            return await service.sync_all_deal_channels()
    
    return run_async(_sync())
```

### Phase 3: Worker Config
    async def get_unique_deal_channels(self) -> list[UniqueChannelInfo]:
        """Get distinct external_channel_ids with canonical integration."""
        
    async def sync_channel_messages(
        self,
        channel: MessagingChannel,
        integration: KnowledgeIntegration,
    ) -> SyncResult:
        """Fetch and store messages for a single channel."""
```

### Phase 3: Worker Config

**File:** `src/workers/integrations_worker/config.py`

```python
# Add to task_routes
"integrations.trigger_deal_channel_messages_sync_all_orgs": {
    "queue": "integrations",
    "routing_key": "integrations",
},
"integrations.sync_deal_channel_messages": {
    "queue": "integrations", 
    "routing_key": "integrations",
},

# Add to beat_schedule
"sync-deal-channel-messages-all-orgs": {
    "task": "integrations.trigger_deal_channel_messages_sync_all_orgs",
    "schedule": crontab(minute=30),  # Every hour at :30
    "options": {"queue": "integrations"},
},
```

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Celery Beat (hourly)                         │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│          trigger_deal_channel_messages_sync_all_orgs                │
│                                                                     │
│  1. Query master DB for APPROVED organisations                      │
│  2. For each org, enqueue sync_deal_channel_messages_task           │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼ (per org)
┌─────────────────────────────────────────────────────────────────────┐
│               sync_deal_channel_messages_task(org_id)               │
│                                                                     │
│  1. Query messaging_deal_links for USER-SELECTED channels           │
│     (detection_method = "manual" or any linked channel)             │
│  2. GROUP BY external_channel_id (deduplicate)                      │
│  3. For each unique channel:                                        │
│     a. RANDOMLY select integration (load distribution)              │
│     b. Get Nango connection for selected integration                │
│     c. Determine channel type:                                      │
│        - Team channel → fetch_messages(team_id, channel_id)         │
│        - Chat → fetch_chat_messages(chat_id)                        │
│     d. Upsert to messaging_messages                                 │
│     e. Update channel.last_synced_at                                │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        messaging_messages                           │
│                                                                     │
│  • Messages stored with randomly selected integration_id            │
│  • No duplicates (same external_message_id per org)                 │
│  • Accessible via messaging_deal_links → channel_id                 │
│  • Load distributed across multiple user tokens                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Why Only Deal-Linked Channels?

**We do NOT sync all channels from all integrations.** Only channels that users have explicitly selected and linked to deals via `MessagingDealLink`.

This is important because:
1. **Privacy** - Users choose what to share with the deal context
2. **Efficiency** - Don't waste API calls on irrelevant channels
3. **Relevance** - Only sync conversations that matter for deal insights

The `MessagingDealLink.detection_method = "manual"` indicates user-selected channels.

## Query: Get Unique Deal-Linked Channels

```python
import random

async def get_unique_deal_linked_channels(
    session: AsyncSession,
    organisation_id: UUID,
) -> list[dict]:
    """
    Get unique channels linked to deals (via MessagingDealLink), with random integration for sync.
    
    Only syncs channels that users have MANUALLY linked to deals.
    Returns one row per unique external_channel_id, with randomly selected integration.
    """
    
    # Step 1: Get all deal-linked channels grouped by external_channel_id
    # Each group may have multiple integrations (same channel, different users)
    
    stmt = (
        select(
            MessagingChannel.external_channel_id,
            func.array_agg(MessagingChannel.channel_id).label("channel_ids"),
            func.array_agg(MessagingChannel.integration_id).label("integration_ids"),
            func.min(MessagingChannel.channel_name).label("channel_name"),
            func.min(MessagingChannel.channel_type).label("channel_type"),
        )
        .select_from(MessagingChannel)
        .join(
            MessagingDealLink,
            MessagingChannel.channel_id == MessagingDealLink.channel_id,
        )
        .join(
            KnowledgeIntegration,
            MessagingChannel.integration_id == KnowledgeIntegration.integration_id,
        )
        .where(
            MessagingChannel.organisation_id == organisation_id,
            MessagingChannel.is_deleted == False,
            MessagingDealLink.is_deleted == False,
            KnowledgeIntegration.is_active == True,
            KnowledgeIntegration.connection_id.isnot(None),
        )
        .group_by(MessagingChannel.external_channel_id)
    )
    
    result = await session.execute(stmt)
    rows = result.all()
    
    # Step 2: For each unique channel, randomly select one integration
    channels_to_sync = []
    for row in rows:
        # Random selection distributes load across user tokens
        idx = random.randint(0, len(row.channel_ids) - 1)
        
        channels_to_sync.append({
            "external_channel_id": row.external_channel_id,
            "channel_id": row.channel_ids[idx],
            "integration_id": row.integration_ids[idx],
            "channel_name": row.channel_name,
            "channel_type": row.channel_type,
            "available_integrations": len(row.channel_ids),  # For logging
        })
    
    return channels_to_sync
```

### Alternative: SQL-Level Random Selection

```python
async def get_unique_deal_linked_channels_sql_random(
    session: AsyncSession,
    organisation_id: UUID,
) -> list[dict]:
    """
    Get unique deal-linked channels with SQL-level random selection.
    
    Uses ROW_NUMBER with RANDOM() ordering to pick one integration per channel.
    """
    
    # Subquery: rank channels by random order within each external_channel_id group
    ranked = (
        select(
            MessagingChannel.channel_id,
            MessagingChannel.external_channel_id,
            MessagingChannel.channel_name,
            MessagingChannel.channel_type,
            MessagingChannel.channel_metadata,
            MessagingChannel.integration_id,
            MessagingChannel.last_synced_at,
            func.row_number()
            .over(
                partition_by=MessagingChannel.external_channel_id,
                order_by=func.random(),  # Random selection
            )
            .label("rn"),
        )
        .select_from(MessagingChannel)
        .join(
            MessagingDealLink,
            MessagingChannel.channel_id == MessagingDealLink.channel_id,
        )
        .join(
            KnowledgeIntegration,
            MessagingChannel.integration_id == KnowledgeIntegration.integration_id,
        )
        .where(
            MessagingChannel.organisation_id == organisation_id,
            MessagingChannel.is_deleted == False,
            MessagingDealLink.is_deleted == False,
            KnowledgeIntegration.is_active == True,
            KnowledgeIntegration.connection_id.isnot(None),
        )
    ).subquery()
    
    # Select only rank=1 (randomly selected integration per unique channel)
    stmt = select(ranked).where(ranked.c.rn == 1)
    
    result = await session.execute(stmt)
    return result.mappings().all()
```

## Sync Time Window

| Scenario | Time Window |
|----------|-------------|
| Initial sync | Last 90 days |
| Incremental sync | Since `last_synced_at` minus 1-day buffer |
| Failed channel retry | Full 90 days (reset) |

```python
def get_sync_time_window(
    channel: MessagingChannel,
    is_initial: bool = False,
) -> datetime | None:
    """Calculate the time_min for message fetch."""
    
    if is_initial or channel.last_synced_at is None:
        # Initial: last 90 days
        return datetime.now(UTC) - timedelta(days=90)
    else:
        # Incremental: since last sync minus buffer
        return channel.last_synced_at - timedelta(days=1)
```

## Message Type Handling

```python
async def sync_channel_messages(
    self,
    channel: MessagingChannel,
    integration: KnowledgeIntegration,
    nango: NangoClient,
) -> int:
    """Fetch and store messages based on channel type."""
    
    time_min = get_sync_time_window(channel)
    
    if channel.channel_type in (ChannelType.PUBLIC, ChannelType.PRIVATE):
        # Team channel - needs team_id from metadata
        team_id = channel.channel_metadata.get("team_id")
        if not team_id:
            raise ValueError(f"Missing team_id for channel {channel.channel_id}")
        
        messages = await fetch_messages(
            nango=nango,
            connection_id=integration.connection_id,
            team_id=team_id,
            channel_id=channel.external_channel_id,
            time_min=time_min,
        )
    
    elif channel.channel_type in (ChannelType.GROUP_MESSAGE, ChannelType.MEETING):
        # Chat - use chat_id directly
        messages = await fetch_chat_messages(
            nango=nango,
            connection_id=integration.connection_id,
            chat_id=channel.external_channel_id,
            time_min=time_min,
        )
    
    else:
        # DIRECT_MESSAGE (1:1) - skip, excluded by design
        return 0
    
    # Upsert messages
    count = await self._store_messages(channel, messages)
    
    # Update sync timestamp
    channel.last_synced_at = datetime.now(UTC)
    
    return count
```

## Error Handling

```python
@app.task(
    bind=True,
    name="integrations.sync_deal_channel_messages",
    acks_late=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute
)
def sync_deal_channel_messages_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Sync with exponential backoff retry."""
    
    try:
        return run_async(_sync(organisation_id))
    except TokenExpiredError as e:
        # Don't retry - token needs refresh
        logger.warning("Integration token expired: %s", e)
        return {"status": "skipped", "reason": "token_expired"}
    except RateLimitError as e:
        # Retry with delay
        raise self.retry(exc=e, countdown=300)  # 5 minutes
    except Exception as e:
        # Exponential backoff
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
```

## Future Enhancement: Option B Migration

If deduplication at schema level becomes necessary:

### Migration Script

```sql
-- Step 1: Create junction table
CREATE TABLE messaging_message_integrations (
    message_id UUID NOT NULL,
    integration_id UUID NOT NULL,
    linked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (message_id, integration_id)
);

-- Step 2: Populate from existing data
INSERT INTO messaging_message_integrations (message_id, integration_id)
SELECT message_id, integration_id FROM messaging_messages;

-- Step 3: Deduplicate messages (keep first per external_message_id)
DELETE FROM messaging_messages a
USING messaging_messages b
WHERE a.external_message_id = b.external_message_id
  AND a.organisation_id = b.organisation_id
  AND a.message_id > b.message_id;

-- Step 4: Update constraint
ALTER TABLE messaging_messages 
DROP CONSTRAINT uq_msg_external;

ALTER TABLE messaging_messages 
ADD CONSTRAINT uq_msg_external_org 
UNIQUE (organisation_id, external_message_id);
```

This migration can be done later if needed, but Option A works well for MVP.

## Acceptance Criteria

- [ ] Task triggers hourly for all approved organisations
- [ ] **Only syncs channels from `MessagingDealLink`** (user-selected channels)
- [ ] Channels are deduplicated by `external_channel_id`
- [ ] **Random integration selection** for load distribution across user tokens
- [ ] Team channels use `fetch_messages(team_id, channel_id)`
- [ ] Chats use `fetch_chat_messages(chat_id)`
- [ ] Messages stored in `messaging_messages` without duplicates
- [ ] `last_synced_at` updated after successful sync
- [ ] Error handling with exponential backoff
- [ ] Logging includes which integration was selected for each channel

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/service/integrations/deal_channel_messages_service.py` | **CREATE** | Service layer with business logic |
| `src/workers/integrations_worker/tasks/deal_channel_messages_tasks.py` | **MODIFY** | Thin task, delegates to service |
| `src/workers/integrations_worker/config.py` | MODIFY | Add routes + schedule |
| `src/service/integrations/__init__.py` | MODIFY | Export new service |
| `test-local-scripts/test_deal_channel_messages_sync.py` | CREATE | Test script |

### Service Responsibilities

| Component | Responsibilities |
|-----------|------------------|
| **Task** | Celery orchestration, retry logic, enqueue per-org |
| **Service** | Query channels, fetch via providers, store messages, update timestamps |
| **Provider** | Teams Graph API calls via Nango proxy |

## Dependencies

- Teams `fetch_messages` and `fetch_chat_messages` working
- `messaging_deal_links` populated (channels linked to deals)
- `integrations-beat` service running (for scheduled execution)

## Related Documents

- [deal-channel-messages-sync.md](../Task%20List/deal-channel-messages-sync.md) - Original task spec
- [teams-channels-auto-sync.md](../Technical%20Documentation/deal/teams-channels-auto-sync.md) - Teams sync architecture
- [messaging-channel-members-sync.md](../Task%20List/messaging-channel-members-sync.md) - Related member sync task
