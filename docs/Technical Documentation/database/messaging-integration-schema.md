# Messaging Integration Schema Documentation

## Overview

The messaging integration system provides comprehensive tracking of conversations from Slack, Microsoft Teams, and Google Chat with intelligent deal association, thread summarization, and knowledge base integration for semantic search.

## Architecture Goals

1. **Cost-Optimized Embeddings**: Only embed thread summaries (~90% cost savings vs embedding all messages)
2. **Selective Channel Monitoring**: Sync only relevant channels (avoid noise from general chat)
3. **Unified Search**: Export summaries to knowledge base for cross-source semantic search
4. **Flexible Retention**: Configurable retention policies per channel (default 90 days for raw messages, permanent summaries)
5. **Thread Context**: Maintain conversation threads with drill-down to individual messages

---

## Tables Explained

### 1. `messaging_channels`

**Purpose**: Represents communication channels from Slack, Teams, or Google Chat with selective sync configuration.

**Why We Need This**:

- **Selective Monitoring**: Not all channels are business-critical (skip #random, #memes, sync only #deals, #customer-acme)
- **Multi-Platform Support**: Unified schema for Slack channels, Teams channels, and Google Chat spaces
- **Sync Configuration**: Each channel can have different settings (start date, retention policy)
- **Channel Analytics**: Track message counts, member counts, last activity

**Key Fields**:

```python
external_channel_id  # Provider's ID (Slack channel ID, Teams channel ID)
channel_name         # Human-readable name ("deals-acme-corp")
channel_type         # PUBLIC_CHANNEL | PRIVATE_CHANNEL | DIRECT_MESSAGE | GROUP_MESSAGE
provider_type        # slack | teams | google_chat

is_monitored         # ✅ Sync this channel actively
sync_from_date       # Don't pull messages before this date (performance)
message_retention_days # How long to keep raw messages (default 90 days)

deal_id              # Associated deal (for deal-specific channels)
member_count         # Number of members in channel
message_count        # Total messages in channel
last_activity_at     # Most recent message timestamp

# Application-level sync state (Nango handles OAuth/provider tokens)
last_sync_cursor     # Where we left off (Slack message ts, last message ID)
last_synced_at       # Last successful sync timestamp
sync_retry_count     # Failed sync attempts counter
```

**Channel Types**:

| Type | Example | Use Case |
|------|---------|----------|
| `PUBLIC_CHANNEL` | #deals-public | Team-wide deal discussions |
| `PRIVATE_CHANNEL` | #acme-corp-internal | Confidential deal channel |
| `DIRECT_MESSAGE` | @john ↔ @jane | 1:1 conversations |
| `GROUP_MESSAGE` | @john, @jane, @bob | Small group DMs |

**Use Cases**:

- Sales team creates private Slack channel "#acme-corp-deal" for a specific deal
- Set `is_monitored=true`, `deal_id=deal-123`
- Configure `sync_from_date=2024-01-01` to avoid syncing old unrelated messages
- Set `message_retention_days=90` (summaries kept forever)

---

### 2. `messaging_messages`

**Purpose**: Individual messages from messaging platforms with full metadata, thread tracking, and participant resolution.

**Why We Need This**:

- **Conversation History**: Complete record of all business communications
- **Thread Reconstruction**: Link messages via `thread_id`, `parent_message_id`
- **Participant Tracking**: Resolve user IDs to canonical participants (unified identity)
- **Drill-Down**: Access raw message content from thread summaries
- **Deal Context**: View all messages related to a deal

**Key Fields**:

```python
external_message_id    # Provider's unique message ID
thread_id              # Thread/conversation identifier
parent_message_id      # Parent message for threaded replies

author_participant_id  # Sender resolved to IntegrationParticipant
author_display_name    # Display name in chat platform
author_external_id     # Provider's user ID

message_text           # Raw message content
message_type           # TEXT | FILE_SHARE | MENTION | SYSTEM | REACTION

posted_at              # When message was posted
edited_at              # Last edit timestamp (if edited)
is_edited              # Whether message was edited

mentions               # List of @mentioned user IDs
reactions              # Reaction counts (👍: 5, ❤️: 3)
has_attachments        # Whether message has files
attachments            # File metadata

is_important           # ⭐ Flag for optional individual embedding
message_embedding      # Optional vector (only for important messages)

deal_id                # Associated deal (inherited from channel or auto-detected)
```

**Why Not Embed All Messages?**

- **Cost**: 50,000 messages/month × $0.00002 = $1.00/month
- **Storage**: 50,000 × 1536 dimensions × 4 bytes = 307 MB
- **Alternative**: Embed only summaries → **$0.10/month (90% savings)**

**Important Message Embedding**:

- User pins message → `is_important=true`
- System generates individual embedding → `message_embedding=[...]`
- Use case: Retrieve specific decisions, commitments, action items

**Message Types**:

| Type | Example | Description |
|------|---------|-------------|
| `TEXT` | "Let's schedule a call" | Regular text message |
| `FILE_SHARE` | "Uploaded contract.pdf" | File attachment shared |
| `MENTION` | "@john can you review?" | Message with @mentions |
| `SYSTEM` | "Jane joined channel" | System notification |
| `REACTION` | "👍 to previous message" | Emoji reaction |

---

### 3. `messaging_channel_members`

**Purpose**: Track channel membership for compliance, analytics, and participant-based deal detection.

**Why We Need This**:

- **Compliance Audit Trail**: Answer "who had access to this conversation?" for legal/regulatory requirements
- **Member History**: Track when users joined/left channels (detect access changes)
- **Participant Overlap Heuristics**: Auto-detect deal relevance when channel members match deal contacts
- **Analytics**: Member engagement metrics, channel growth patterns

**Key Fields**:

```python
channel_id            # Channel this membership belongs to
participant_id        # Resolved canonical participant
joined_at             # When member joined
left_at               # When member left (NULL if still active)
member_role           # owner | admin | member | guest
added_by_participant_id  # Who invited this member
```

**Use Cases**:

- Compliance query: "Show all users who had access to #acme-deal channel in Q4 2024"
- Auto-detection: If 4/5 channel members are on Acme deal contacts → suggest linking channel
- Analytics: Track channel activity per member, identify key contributors

---

### 4. `messaging_deal_links`

**Purpose**: Track message/channel-to-deal associations with detection metadata and verification workflow.

**Why We Need This**:

- **Multi-Deal Support**: Same DM or channel can relate to multiple deals
- **Audit Trail**: Know HOW each link was detected (channel config, participant match, manual)
- **Confidence Scoring**: Auto-detected links have confidence scores for user verification
- **Flexible Linking**: Support both channel-level and message-level associations

**Key Fields**:

```python
message_id            # Message being linked (NULL for channel-level)
channel_id            # Channel being linked (NULL for message-level)
deal_id               # Deal being linked to
detection_method      # channel_config | participant_match | channel_name | manual
confidence_score      # 0.0-1.0 (1.0 for manual links)

linked_at             # When link was created
linked_by             # User who manually linked (NULL for auto)

is_verified           # Has user verified this auto-link?
verified_at           # When verified
verified_by           # User who verified
```

**Detection Methods**:

| Method | Description | Confidence |
|--------|-------------|------------|
| `channel_config` | Channel.deal_id explicitly set | 1.0 |
| `participant_match` | 2+ channel members are deal contacts | 0.85 |
| `channel_name` | Channel name contains deal identifier | 0.75 |
| `manual` | User explicitly linked | 1.0 |

**Example Scenarios**:

- DM between sales rep and customer contacts 2 deals → 2 `messaging_deal_links` records
- Private channel has 5 members, 4 are on Deal X contacts → auto-create link with confidence=0.90
- User manually links a thread to a deal → confidence=1.0, no verification needed

---

### 5. `messaging_thread_summaries`

**Purpose**: AI-generated summaries of message threads with embeddings for semantic search.

**Why We Need This**:

- **Cost Optimization**: 1 embedding per thread vs 20+ embeddings per message
- **Context Retrieval**: Summaries provide conversation context without reading every message
- **Unified Search**: Export to knowledge base for cross-source semantic search (messages + files + emails + meetings)
- **Permanent Storage**: Summaries kept forever, raw messages expire after 90 days
- **Analytics**: Extract key points, decisions, action items, sentiment

**Key Fields**:

```python
channel_id          # Channel this summary belongs to
thread_id           # Thread identifier (groups related messages)

deal_id             # Associated deal (if detected)
thread_starter_id   # Participant who started the thread

summary_text        # AI-generated summary (e.g., "15-message thread discussing...")
key_points          # ["Agreed to extend trial", "Pricing at $50k annual", ...]
decisions_made      # ["Approved contract terms", "Postponed kickoff to next week"]
action_items        # ["@john to send proposal", "@jane to schedule demo"]
participants_summary # { "user_123": 8, "user_456": 7 }

message_count       # Number of messages in this summary
time_range_start    # First message timestamp
time_range_end      # Last message timestamp

sentiment_score     # -1.0 (negative) to 1.0 (positive)
urgency_level       # low | medium | high | critical
topics              # ["pricing", "product demo", "contract negotiation"]

summary_embedding   # ⭐ Vector embedding for semantic search
source_message_ids  # List of message IDs included (for drill-down)

exported_to_kb      # Has this been exported to knowledge base?
kb_source_id        # Reference to OrganisationSource (for linking)
```

**Summary Generation Process**:

```text
Day 1 (Messages synced in real-time):
├─ 15 messages in thread "Acme Pricing Discussion"
├─ Raw messages stored in `messaging_messages` table (no embeddings)
└─ Wait for nightly summary job...

Night (Summary generation at 2 AM):
├─ Group messages by thread_id
├─ Generate AI summary with GPT-4o-mini
│  └─ "15-message thread in #acme-corp-deal discussing contract pricing.
│      Key decisions: approved $50k annual contract, extended trial by 2 weeks.
│      Action items: @john to send updated proposal, @jane to schedule kickoff."
├─ Create 1 embedding for summary text (text-embedding-3-small)
├─ Extract key_points, decisions_made, action_items, sentiment
└─ Store in `messaging_thread_summaries` with embedding

Night (KB export at 3 AM):
├─ Find summaries where `exported_to_kb=false`
├─ Create OrganisationSource (source_type="message_thread")
├─ Create OrganisationDocument
│  ├─ content = summary_text
│  └─ content_embedding = summary_embedding (REUSE, don't re-embed!)
├─ Update summary: `exported_to_kb=true`, `kb_source_id=source_id`
└─ Now searchable in unified RAG query!
```

**Cost Analysis**:

| Approach | Embeddings/Month | Cost | Storage |
|----------|------------------|------|---------|
| Embed all messages | 50,000 | $1.00 | 307 MB |
| Embed summaries only | 5,000 | **$0.10** | 30.7 MB |
| **Savings** | **90% fewer** | **90% less** | **90% less** |

---

## Data Flow: Message Sync to Knowledge Base

### Phase 1: Message Sync (Real-time via Webhooks)

```text
Slack/Teams/Google Chat Webhook
    ↓
Receive new message event
    ↓
Store in `messaging_channels` + `messaging_messages` tables
    ↓
Resolve author → link to `integration_participants`
    ↓
Inherit deal_id from channel (if channel.deal_id is set)
    ↓
No embeddings yet (cost optimization)
```

### Phase 2: Summary Generation (Nightly at 2 AM)

```sql
-- Find active threads from yesterday
SELECT thread_id, array_agg(message_id) 
FROM messaging_messages 
WHERE posted_at >= CURRENT_DATE - 1
  AND thread_id IS NOT NULL
GROUP BY thread_id
HAVING count(*) >= 3  -- At least 3 messages to form meaningful thread
```

```text
For each thread:
    ├─ Fetch all messages in thread
    ├─ Generate AI summary (GPT-4o-mini)
    ├─ Create 1 embedding (text-embedding-3-small)
    ├─ Extract key_points, decisions_made, action_items, sentiment
    └─ Store in `messaging_thread_summaries`
```

### Phase 3: Knowledge Base Export (Nightly at 3 AM)

```sql
-- Find unexported summaries
SELECT * FROM messaging_thread_summaries 
WHERE exported_to_kb = false
```

```text
For each summary:
    ├─ Create OrganisationSource (source_type="message_thread")
    ├─ Create OrganisationDocument
    │   ├─ content = summary_text
    │   └─ content_embedding = summary_embedding (REUSE!)
    └─ Update: exported_to_kb=true, kb_source_id=source_id
    ↓
Now searchable in unified RAG!
```

### Phase 4: User Query (Real-time)

```text
User: "What decisions were made on Acme pricing?"
    ↓
Vector search across organisation_documents (includes message summaries)
    ↓
Find: "Acme Pricing Discussion" (summary from messaging_thread_summaries)
    ↓
Display summary with:
    ├─ Key decisions: Approved $50k annual contract
    ├─ Action items: @john to send proposal
    ├─ Participants: john (8 messages), jane (7 messages)
    └─ Drill-down: Show individual messages via source_message_ids
```

---

## Deal Association Strategies

Unlike Slack/Teams where channels can be explicitly linked to deals, we need intelligent strategies:

### Strategy 1: Channel-Level Association (Primary)

**Setup**:

- Sales team creates Slack channel "#acme-corp-deal"
- Admin links channel to deal in UI: `messaging_channels.deal_id = deal-123`
- All messages in this channel automatically inherit `deal_id`

**Implementation**:

```python
# When syncing messages
message = MessagingMessage(
    channel_id=channel.channel_id,
    deal_id=channel.deal_id,  # Inherit from channel
    ...
)
```

### Strategy 2: Participant-Based Detection (Auto)

**Logic**:

- If message involves 2+ participants who are deal contacts (from `crm_deal_participants`)
- Auto-associate message with that deal

**Implementation**:

```python
# Check if message participants match deal contacts
deal_contacts = get_deal_participants(deal_id)
message_participants = [message.author_participant_id] + message.mentions

overlap = set(message_participants) & set(deal_contacts)
if len(overlap) >= 2:
    message.deal_id = deal_id
```

### Strategy 3: Channel Name Pattern Matching

**Logic**:

- Channel name contains deal name or identifier
- Examples: "#acme-corp", "#deal-acme", "#customer-acme"

**Implementation**:

```python
# Pattern matching on channel name
if "acme" in channel.channel_name.lower():
    channel.deal_id = find_deal_by_name("Acme Corp")
```

### Strategy 4: Manual Linking (UI)

**User Action**:

- User browses channel/thread
- Clicks "Link to Deal" → selects deal from dropdown
- Creates association at channel or thread level

---

## Example Queries

### 1. Find All Messages for a Deal

```sql
-- Via deal_id (direct association)
SELECT m.* 
FROM messaging_messages m
WHERE m.deal_id = 'deal-123'
ORDER BY m.posted_at DESC;

-- Via channel association
SELECT m.* 
FROM messaging_messages m
JOIN messaging_channels c ON m.channel_id = c.channel_id
WHERE c.deal_id = 'deal-123'
ORDER BY m.posted_at DESC;
```

### 2. Get Thread Summary with Source Messages

```sql
-- Get summary
SELECT * FROM messaging_thread_summaries 
WHERE thread_id = 'thread-456';

-- Get individual messages in thread
SELECT m.* 
FROM messaging_messages m
WHERE m.thread_id = 'thread-456'
ORDER BY m.posted_at ASC;
```

### 3. Find Active Channels for a Deal

```sql
SELECT 
    c.channel_name,
    c.provider_type,
    c.message_count,
    c.last_activity_at,
    c.is_monitored
FROM messaging_channels c
WHERE c.deal_id = 'deal-123'
  AND c.is_monitored = true
ORDER BY c.last_activity_at DESC;
```

### 4. Find Important/Pinned Messages

```sql
SELECT 
    m.message_text,
    m.author_display_name,
    m.posted_at,
    c.channel_name
FROM messaging_messages m
JOIN messaging_channels c ON m.channel_id = c.channel_id
WHERE m.is_important = true
  AND m.deal_id = 'deal-123'
ORDER BY m.posted_at DESC;
```

### 5. Search Message Summaries (Semantic Search)

```python
# Vector search across knowledge base (includes message summaries)
query_embedding = await create_embedding("Acme pricing decisions")

results = await db.execute(
    """
    SELECT 
        od.content,
        os.source_type,
        os.source_metadata,
        1 - (od.content_embedding <=> :query_embedding) as similarity
    FROM organisation_documents od
    JOIN organisation_sources os ON od.source_id = os.source_id
    WHERE os.source_type IN ('message_thread', 'email_thread', 'file', 'meeting')
    ORDER BY od.content_embedding <=> :query_embedding
    LIMIT 10
    """,
    {"query_embedding": query_embedding}
)

# If result is message thread, drill down to individual messages
if result.source_type == "message_thread":
    thread_id = result.source_metadata["thread_id"]
    messages = await fetch_thread_messages(thread_id)
```

### 6. Channel Activity Analytics

```sql
SELECT 
    c.channel_name,
    c.provider_type,
    COUNT(m.message_id) as messages_last_7_days,
    COUNT(DISTINCT m.author_participant_id) as active_participants,
    MAX(m.posted_at) as last_message_at
FROM messaging_channels c
LEFT JOIN messaging_messages m ON c.channel_id = m.channel_id
    AND m.posted_at >= CURRENT_DATE - INTERVAL '7 days'
WHERE c.deal_id = 'deal-123'
GROUP BY c.channel_id, c.channel_name, c.provider_type
ORDER BY messages_last_7_days DESC;
```

---

## Retention Policy

| Data Type | Table | Retention | Reason |
|-----------|-------|-----------|--------|
| Raw Messages | `messaging_messages` | 90 days (configurable) | Drill-down access, compliance |
| Message Summaries | `messaging_thread_summaries` | Permanent | Long-term context, embeddings |
| KB Documents | `organisation_documents` | Permanent | Unified search requires persistence |
| Channels | `messaging_channels` | Until manually deleted | Active channel metadata |

**Archival Strategy**:

- After 90 days: Delete `messaging_messages.message_text` (keep metadata)
- Keep: author, timestamp, thread_id, reactions, is_important flag
- Reason: Summaries provide sufficient context for old threads

**Compliance Considerations**:

- Some industries require longer retention (e.g., financial services)
- Configurable via `messaging_channels.message_retention_days`
- Legal hold: Flag channels to prevent deletion

---

## Integration Points

### Participant Resolution

```python
# Message sync resolves platform user IDs to canonical participants
message = MessagingMessage(
    author_external_id="slack_user_U12345",
    author_participant_id=resolve_participant("slack_user_U12345", "slack")
)

# Uses integration_participants table (unified identity across all integrations)
# Same participant might be: Slack user, Gmail address, Gong speaker, CRM contact
```

### Deal Auto-Detection

```python
# Check if message participants match deal contacts
deal_contacts = get_deal_participants(deal_id)
message_participants = [message.author_participant_id] + message.mentions

overlap = set(message_participants) & set(deal_contacts)
if len(overlap) >= 2:
    link_message_to_deal(message, deal_id, confidence=0.85)
```

### Knowledge Base Export

```python
# Reuse embedding from summary (don't re-embed!)
document = OrganisationDocument(
    content=summary.summary_text,
    content_embedding=summary.summary_embedding,  # REUSE!
    source_type="message_thread",
    source_metadata={
        "channel_name": channel.channel_name,
        "provider_type": channel.provider_type,
        "thread_id": summary.thread_id,
        "message_count": summary.message_count
    }
)
```

### Webhook Integration

```python
# Real-time message ingestion via webhooks
@app.post("/webhooks/slack/events")
async def handle_slack_event(event: SlackEvent):
    if event.type == "message":
        # Store message
        message = MessagingMessage(
            channel_id=get_or_create_channel(event.channel),
            external_message_id=event.ts,
            message_text=event.text,
            author_external_id=event.user,
            posted_at=datetime.fromtimestamp(event.ts)
        )
        await save_message(message)
        
        # Update channel stats
        await update_channel_stats(event.channel)
```

---

## Multi-Platform Considerations

### Slack-Specific Features

- **Threads**: Native threading via `thread_ts`
- **Reactions**: Emoji reactions on messages
- **Pins**: Important messages pinned in channel
- **Apps/Bots**: Filter out bot messages (optional)

### Microsoft Teams-Specific Features

- **Channels**: Teams → Channels → Messages hierarchy
- **Tabs**: Rich content tabs in channels
- **Mentions**: @mentions with user IDs
- **Adaptive Cards**: Rich message formatting

### Google Chat-Specific Features

- **Spaces**: Equivalent to channels
- **Threads**: All messages in spaces are threaded
- **Bots**: Google Chat apps/bots
- **Cards**: Rich card-based messages

### Unified Schema Mapping

| Concept | Slack | Teams | Google Chat | Our Schema |
|---------|-------|-------|-------------|------------|
| Container | Channel | Channel | Space | `messaging_channels` |
| User ID | `U12345` | `user@domain.com` | `users/123` | `external_user_id` |
| Message ID | Timestamp | Message ID | Message Name | `external_message_id` |
| Thread ID | `thread_ts` | `replyToId` | `thread.name` | `thread_id` |

---

## Future Enhancements

1. **Smart Threading**: Use ML to link related messages across different threads
2. **Auto-Importance Detection**: AI flags critical decisions/commitments for individual embedding
3. **Sentiment Tracking**: Monitor conversation sentiment over time per deal
4. **Real-time Alerts**: Notify on urgent messages or negative sentiment spikes
5. **Message Templates**: Detect and categorize template-based messages
6. **Integration with CRM**: Auto-create tasks from action items in messages
7. **Message Analytics**: Response time tracking, message volume per deal, participant engagement

---

## Comparison: Messaging vs Email

| Aspect | Messaging | Email |
|--------|-----------|-------|
| **Volume** | High (50k+ msgs/month) | Medium (1k emails/month) |
| **Retention** | 90 days default | 180 days default |
| **Threading** | Native (thread_id) | Manual (in_reply_to, references) |
| **Real-time** | Yes (webhooks) | Polling (every 15-60 min) |
| **Deal Association** | Channel-level + auto-detect | Rule-based + auto-detect |
| **Embedding Cost** | ~$0.10/month (summaries) | ~$0.06/month (summaries) |
| **Search Priority** | Recent decisions/context | Historical communication |

---

## Summary

| Table | Purpose | Key Benefit |
|-------|---------|-------------|
| `messaging_channels` | Channel metadata + sync config | Selective monitoring, deal association |
| `messaging_messages` | Raw message storage | Real-time sync, drill-down access |
| `messaging_thread_summaries` | AI summaries + embeddings | 90% cost savings, unified search |

**Cost-Optimized Architecture**: Raw messages (no embeddings) → Thread summaries (1 embedding) → KB export (reuse embedding) → Unified semantic search across all sources.

**Deal Association**: Primary via channel-level linking, secondary via participant matching, with manual override capability.
