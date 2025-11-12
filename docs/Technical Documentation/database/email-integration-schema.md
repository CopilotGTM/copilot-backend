# Email Integration Schema Documentation

## Overview

The email integration system provides comprehensive tracking of emails from Gmail, Outlook, and other providers with thread summarization and knowledge base integration for semantic search.

## Architecture Goals

1. **Cost-Optimized Embeddings**: Only embed thread summaries (~90% cost savings vs embedding all emails)
2. **Simple Deal Association**: Emails can be manually linked to deals via `deal_id` field
3. **Unified Search**: Export summaries to knowledge base for cross-source semantic search
4. **Flexible Retention**: Configurable retention policies per folder (default 180 days for raw emails, permanent summaries)
5. **Thread Context**: Maintain conversation threads with drill-down to individual emails

---

## Tables Explained

### 1. `email_folders`

**Purpose**: Represents email folders (Gmail labels, Outlook folders) with selective sync configuration.

**Why We Need This**:

- **Selective Monitoring**: Not all folders need to be synced (skip "Spam", "Trash", sync only "Deals", "Customers")
- **Organization Structure**: Maps provider-specific folder hierarchy to our system
- **Sync Configuration**: Each folder can have different sync settings (start date, retention policy)
- **Folder Analytics**: Track folder stats (total emails, unread count, last activity)

**Key Fields**:

```python
external_folder_id  # Provider's ID (Gmail label ID, Outlook folder ID)
folder_name         # Human-readable name ("Inbox", "Deals/Acme Corp")
folder_path         # Full nested path ("Inbox/Deals/Acme Corp")
is_monitored        # ✅ Sync this folder actively
sync_from_date      # Don't pull emails before this date (performance)
email_retention_days # How long to keep raw emails (default 180 days)

# Application-level sync state (Nango handles OAuth/provider tokens)
last_sync_cursor    # Where we left off (last email ID, timestamp)
last_synced_at      # Last successful sync timestamp
sync_retry_count    # Failed sync attempts counter
```

**Use Cases**:

- User selects "Deals" folder → set `is_monitored=true`, sync only deal-related emails
- Set `sync_from_date=2024-01-01` → don't sync 5 years of old emails
- Configure `email_retention_days=90` for "Archive" folder (shorter retention)

---

### 2. `emails`

**Purpose**: Individual email messages with full metadata, thread tracking, and participant resolution.

**Why We Need This**:

- **Audit Trail**: Complete history of all email communications
- **Thread Reconstruction**: Link emails via `thread_id`, `in_reply_to`, `references`
- **Participant Tracking**: Resolve email addresses to canonical participants (unified identity)
- **Drill-Down**: Access raw email content from thread summaries
- **Deal Context**: View all emails associated with a deal

**Key Fields**:

```python
external_message_id    # Provider's unique message ID
thread_id              # Group emails in same conversation
in_reply_to            # Parent message ID (for reply chains)
references             # Full conversation chain

from_participant_id    # Sender resolved to IntegrationParticipant
from_email / from_name # Raw sender data
to_participants        # List of resolved recipient participant IDs
to_emails / cc_emails  # Raw recipient data

subject / body_text / body_html  # Email content
snippet                          # Short preview (150 chars)

received_at / sent_at  # Timestamps
direction              # INBOUND | OUTBOUND
priority               # LOW | NORMAL | HIGH | URGENT

is_important           # ⭐ Flag for optional individual embedding
has_attachments        # Whether email has files
attachments            # File metadata (name, size, mime_type, download_url)

deal_id                # Associated deal (auto-detected or manual)
email_embedding        # Optional vector (only for important emails)
```

**Why Not Embed All Emails?**

- **Cost**: 30,000 emails/month × $0.00002 = $0.60/month
- **Storage**: 30,000 × 1536 dimensions × 4 bytes = 184 MB
- **Alternative**: Embed only summaries → **$0.06/month (90% savings)**

**Important Email Embedding**:

- User flags email as important → `is_important=true`
- System generates individual embedding → `email_embedding=[...]`
- Use case: Precise retrieval of critical contract emails, pricing discussions, etc.

---

### 3. `email_thread_summaries`

**Purpose**: AI-generated summaries of email threads with embeddings for semantic search.

**Why We Need This**:

- **Cost Optimization**: 1 embedding per thread vs 10+ embeddings per email
- **Context Retrieval**: Summaries provide conversation context without reading every email
- **Unified Search**: Export to knowledge base for cross-source semantic search (emails + files + meetings + chats)
- **Permanent Storage**: Summaries kept forever, raw emails expire after 180 days
- **Analytics**: Extract key points, action items, sentiment, urgency

**Key Fields**:

```python
thread_id           # Email thread identifier (groups related emails)
subject             # Thread subject line
deal_id             # Associated deal (if detected)

summary_text        # AI-generated summary (e.g., "5-email thread discussing Acme pricing...")
key_points          # ["Agreed on $100k annual contract", "Net 30 payment terms", ...]
action_items        # ["Send proposal by Friday", "Schedule kickoff call", ...]
participants_summary # { "john@acme.com": 3, "jane@ourcompany.com": 2 }

email_count         # Number of emails in this summary
time_range_start    # First email timestamp
time_range_end      # Last email timestamp

sentiment_score     # -1.0 (negative) to 1.0 (positive)
urgency_level       # low | medium | high | critical
topics              # ["pricing", "contract negotiation", "payment terms"]

summary_embedding   # ⭐ Vector embedding for semantic search
source_email_ids    # List of email IDs included in summary (for drill-down)

exported_to_kb      # Has this been exported to knowledge base?
kb_source_id        # Reference to OrganisationSource (for linking)
```

**Summary Generation Process**:

```
Night 1 (Day's emails synced):
├─ 10 emails in thread "Acme Pricing Discussion"
├─ Raw emails stored in `emails` table (no embeddings)
└─ Wait for nightly summary job...

Night 2 (Summary generation at 2 AM):
├─ Group emails by thread_id
├─ Generate AI summary with GPT-4o-mini
│  └─ "5-email thread between John (Acme) and Jane (Sales) discussing annual
│      contract pricing. Key points: agreed on $100k, net 30 terms, 
│      pending legal review. Action: send proposal by Friday."
├─ Create 1 embedding for summary text (text-embedding-3-small)
├─ Extract key_points, action_items, sentiment
└─ Store in `email_thread_summaries` with embedding

Night 3 (KB export at 3 AM):
├─ Find summaries where `exported_to_kb=false`
├─ Create OrganisationSource (source_type="email_thread")
├─ Create OrganisationDocument
│  ├─ content = summary_text
│  └─ content_embedding = summary_embedding (REUSE, don't re-embed!)
├─ Update summary: `exported_to_kb=true`, `kb_source_id=source_id`
└─ Now searchable in unified RAG query!
```

**Cost Analysis**:

| Approach | Embeddings/Month | Cost | Storage |
|----------|------------------|------|---------|
| Embed all emails | 30,000 | $0.60 | 184 MB |
| Embed summaries only | 3,000 | **$0.06** | 18.4 MB |
| **Savings** | **90% fewer** | **90% less** | **90% less** |

---

## Data Flow: Email Sync to Knowledge Base

### Phase 1: Email Sync (Real-time/Hourly)

```
Gmail/Outlook API
    ↓
Sync monitored folders (is_monitored=true)
    ↓
Store in `email_folders` + `emails` tables
    ↓
Resolve participants → link to `integration_participants`
    ↓
Run deal rules → create `email_deal_links`
    ↓
No embeddings yet (cost optimization)
```

### Phase 2: Summary Generation (Nightly at 2 AM)

```
Query: SELECT thread_id, array_agg(email_id) 
       FROM emails 
       WHERE received_at >= CURRENT_DATE - 1
       GROUP BY thread_id
       HAVING count(*) >= 2
    ↓
For each thread:
    ├─ Fetch all emails in thread
    ├─ Generate AI summary (GPT-4o-mini)
    ├─ Create 1 embedding (text-embedding-3-small)
    ├─ Extract key_points, action_items, sentiment
    └─ Store in `email_thread_summaries`
```

### Phase 3: Knowledge Base Export (Nightly at 3 AM)

```
Query: SELECT * FROM email_thread_summaries 
       WHERE exported_to_kb=false
    ↓
For each summary:
    ├─ Create OrganisationSource (source_type="email_thread")
    ├─ Create OrganisationDocument
    │   ├─ content = summary_text
    │   └─ content_embedding = summary_embedding (REUSE!)
    └─ Update: exported_to_kb=true, kb_source_id=source_id
    ↓
Now searchable in unified RAG!
```

### Phase 4: User Query (Real-time)

```
User: "What's the latest on Acme pricing?"
    ↓
Vector search across organisation_documents (includes email summaries)
    ↓
Find: "Acme Pricing Discussion" (summary from email_thread_summaries)
    ↓
Display summary with:
    ├─ Key points: $100k contract, net 30 terms
    ├─ Action items: send proposal by Friday
    ├─ Participants: john@acme.com, jane@ourcompany.com
    └─ Drill-down: Show individual emails via source_email_ids
```

---

## Example Queries

### 1. Find All Emails for a Deal

```sql
-- Via deal_id (direct association)
SELECT e.* 
FROM emails e
WHERE e.deal_id = 'deal-123';

-- Via email_deal_links (includes all link types)
SELECT e.* 
FROM emails e
JOIN email_deal_links edl ON e.email_id = edl.email_id
WHERE edl.deal_id = 'deal-123'
ORDER BY e.received_at DESC;
```

### 2. Get Thread Summary with Source Emails

```sql
-- Get summary
SELECT * FROM email_thread_summaries 
WHERE thread_id = 'thread-456';

-- Get individual emails in thread
SELECT e.* 
FROM emails e
WHERE e.thread_id = 'thread-456'
ORDER BY e.received_at ASC;
```

### 3. Find Unverified Auto-Detected Links

```sql
SELECT 
    edl.*,
    e.subject,
    e.from_email,
    d.deal_name
FROM email_deal_links edl
JOIN emails e ON edl.email_id = e.email_id
JOIN crm_deals d ON edl.deal_id = d.deal_id
WHERE edl.is_verified = false
  AND edl.confidence_score < 0.9
ORDER BY edl.confidence_score ASC;
```

### 4. Rule Performance Analytics

```sql
SELECT 
    rule_type,
    rule_pattern,
    match_count,
    last_matched_at,
    is_active
FROM deal_email_rules
WHERE deal_id = 'deal-123'
ORDER BY match_count DESC;
```

### 5. Search Email Summaries (Semantic Search)

```python
# Vector search across knowledge base (includes email summaries)
query_embedding = await create_embedding("Acme pricing discussion")

results = await db.execute(
    """
    SELECT 
        od.content,
        os.source_type,
        os.source_metadata,
        1 - (od.content_embedding <=> :query_embedding) as similarity
    FROM organisation_documents od
    JOIN organisation_sources os ON od.source_id = os.source_id
    WHERE os.source_type IN ('email_thread', 'file', 'meeting', 'message')
    ORDER BY od.content_embedding <=> :query_embedding
    LIMIT 10
    """,
    {"query_embedding": query_embedding}
)

# If result is email thread, drill down to individual emails
if result.source_type == "email_thread":
    thread_id = result.source_metadata["thread_id"]
    emails = await fetch_thread_emails(thread_id)
```

---

## Retention Policy

| Data Type | Table | Retention | Reason |
|-----------|-------|-----------|--------|
| Raw Emails | `emails` | 180 days (configurable) | Audit trail, drill-down access |
| Email Summaries | `email_thread_summaries` | Permanent | Long-term context, embeddings |
| KB Documents | `organisation_documents` | Permanent | Unified search requires persistence |

**Archival Strategy**:

- After 180 days: Delete `emails.body_text`, `emails.body_html` (keep metadata)
- Keep: subject, snippet, participants, timestamps, deal_id
- Reason: Summaries provide sufficient context for old threads

---

## Integration Points

### Participant Resolution

```python
# Email sync resolves email addresses to canonical participants
email = Email(
    from_email="john@acmecorp.com",
    from_participant_id=resolve_participant("john@acmecorp.com")
)

# Uses integration_participants table (unified identity across integrations)
```

### Deal Auto-Detection

```python
# Check all active rules for this email
for rule in active_rules:
    if rule.matches(email):
        create_link(email, rule.deal_id, rule.rule_type, confidence=0.85)
```

### Knowledge Base Export

```python
# Reuse embedding from summary (don't re-embed!)
document = OrganisationDocument(
    content=summary.summary_text,
    content_embedding=summary.summary_embedding,  # REUSE!
    source_type="email_thread"
)
```

---

## Future Enhancements

1. **Smart Threading**: Use ML to link emails across different thread_ids
2. **Auto-Importance Detection**: AI flags important emails for individual embedding
3. **Participant Sentiment**: Track sentiment per participant over time
4. **Email Templates**: Detect and categorize template-based emails (invoices, newsletters)
5. **Attachment Processing**: Extract text from PDFs, process into knowledge base
6. **Real-time Alerts**: Notify on high-urgency email threads
7. **Email Analytics**: Response time tracking, email volume per deal

---

## Summary

| Table | Purpose | Key Benefit |
|-------|---------|-------------|
| `email_folders` | Folder metadata + sync config | Selective monitoring, retention policies |
| `emails` | Raw email storage + manual deal linking | Audit trail, drill-down access, simple deal_id field |
| `email_thread_summaries` | AI summaries + embeddings | 90% cost savings, unified search |

**Cost-Optimized Architecture**: Raw emails (no embeddings, manual deal_id) → Thread summaries (1 embedding) → KB export (reuse embedding) → Unified semantic search across all sources.
