# Deal Timeline Feature - Design Document

## Overview

This document outlines the design for a **Deal Timeline** feature that provides a chronological view of all activities related to a deal from start to end. The timeline aggregates multiple event sources (emails, meetings, and future integrations like Slack messages) into a unified, sorted view.

## Problem Statement

Currently, deal-related activities are stored across multiple tables:

- **Emails** → `deal_email_associations` (links via `internet_message_id`)
- **Meetings** → `calendar_event_deal_associations` (links via `event_id`)
- **Stage Changes** → `crm_deal_stage_history`
- **Future**: Slack messages, tasks, notes, etc.

To render the timeline UI (as shown in the reference mockup), we need:

1. **Fast retrieval** of all events for a deal in chronological order
2. **Pagination support** for deals with many activities
3. **Filtering by event type** (show only emails, only meetings, etc.)
4. **Preview data** for the timeline card (title, timestamp, participants, source type)

### Current Challenges

| Approach | Problem |
|----------|---------|
| Query all source tables at runtime | Multiple JOINs, sorting across tables is expensive, hard to paginate |
| Union query | Complex SQL, different schemas, no indexed sort across union |
| Denormalized timeline table | ✅ Single indexed table, fast chronological queries, easy pagination |

## Proposed Solution: `DealTimelineEvent` Table

Create a **denormalized timeline events table** that stores lightweight event metadata from all sources. This table acts as an **indexed timeline index** for fast retrieval while the actual event details are fetched from source tables on demand (when user clicks an event).

### Design Principles

1. **Single Source of Truth for Timeline Order** - All events indexed in one place
2. **Lightweight Records** - Store only enough data for timeline card rendering
3. **Source Linkage** - Reference back to original record for full details
4. **Extensible Event Types** - Easy to add new sources (Slack, tasks, etc.)
5. **Automatic Population** - Events added when associations are created

---

## Data Model

### DealTimelineEvent Table

```python
class TimelineEventType(str, Enum):
    """Types of events that appear on the deal timeline."""
    
    # Communication Events
    EMAIL = "EMAIL"               # Email sent/received
    MEETING = "MEETING"           # Calendar meeting
    MESSAGE = "MESSAGE"           # Generic message (Slack/Teams/etc.)
    
    # Deal Lifecycle Events
    DEAL_CREATED = "DEAL_CREATED"
    STAGE_CHANGE = "STAGE_CHANGE"
    
    # Activity Events (future)
    TASK = "TASK"
    NOTE = "NOTE"
    CALL = "CALL"
    
    # System Events
    BLOCKER_ADDED = "BLOCKER_ADDED"
    BLOCKER_RESOLVED = "BLOCKER_RESOLVED"
    RISK_FLAGGED = "RISK_FLAGGED"


class TimelineEventSource(str, Enum):
    """Source system that generated the event."""
    
    EMAIL_GMAIL = "EMAIL_GMAIL"
    EMAIL_OUTLOOK = "EMAIL_OUTLOOK"
    CALENDAR_GOOGLE = "CALENDAR_GOOGLE"
    CALENDAR_OUTLOOK = "CALENDAR_OUTLOOK"
    SLACK = "SLACK"
    TEAMS = "TEAMS"
    CRM_HUBSPOT = "CRM_HUBSPOT"
    CRM_SALESFORCE = "CRM_SALESFORCE"
    SYSTEM = "SYSTEM"  # Internal system events


class DealTimelineEvent(SQLModel, table=True):
    """
    Unified timeline index for all deal-related activities.
    
    This is a denormalized table optimized for fast chronological retrieval.
    Stores minimal data for timeline card rendering; full details fetched on demand.
    """
    
    __tablename__ = "deal_timeline_events"
    __table_args__ = (
        # Primary query pattern: all events for a deal, sorted by time
        Index("ix_timeline_deal_time", "deal_id", "event_timestamp"),
        # Filter by type within a deal
        Index("ix_timeline_deal_type_time", "deal_id", "event_type", "event_timestamp"),
        # Org-wide queries (admin dashboards)
        Index("ix_timeline_org_time", "organisation_id", "event_timestamp"),
        # Prevent duplicate entries for same source record
        UniqueConstraint("deal_id", "event_type", "source_record_id", name="uq_timeline_event_source"),
    )

    # Primary Key
    timeline_event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    
    # Organisation & Deal Context
    organisation_id: UUID = Field(index=True)
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="The deal this event belongs to.",
    )
    
    # Event Classification
    event_type: TimelineEventType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of timeline event (EMAIL, MEETING, STAGE_CHANGE, etc.).",
    )
    event_source: TimelineEventSource = Field(
        sa_column=Column(String(32), nullable=False),
        description="Source system (GMAIL, OUTLOOK, HUBSPOT, etc.).",
    )
    
    # Temporal Ordering (THE KEY FIELD FOR TIMELINE)
    event_timestamp: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the event occurred - used for chronological sorting.",
    )
    
    # Source Record Reference (for fetching full details)
    source_record_id: str = Field(
        sa_column=Column(String(512), nullable=False),
        description="ID of the source record (association_id, history_id, or external_id).",
    )
    # Note: we intentionally do NOT persist `source_table`.
    # The API can route to the correct storage using `event_type` (and `event_source` if needed).
    
    # Timeline Card Preview Data (denormalized for fast rendering)
    title: str = Field(
        sa_column=Column(String(500), nullable=False),
        description="Display title for timeline card (e.g., email subject, meeting title).",
    )
    subtitle: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Secondary text (e.g., 'Stage: Discovery → Evaluation').",
    )
    
    # Direction/Status Indicators
    direction: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="INBOUND/OUTBOUND/INTERNAL for emails; null for other types.",
    )
    status: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Event status (e.g., SUGGESTED/CONFIRMED for associations, Active for blockers).",
    )

    # Soft exclusion (hide from timeline without deleting)
    excluded_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=(
            "If set, this event is excluded from the default timeline query/UX. "
            "Use when an item was previously indexed but later rejected/cancelled (e.g., association rejected)."
        ),
    )
    
    # Participants Preview (for timeline card)
    participant_names: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        description="List of participant names for preview (max 3-4 names).",
    )
    participant_count: int = Field(
        default=0,
        description="Total participant count (for '+N more' display).",
    )
    
    # Optional Metadata (named event_metadata to avoid SQLModel base class conflict)
    event_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Additional type-specific metadata for rendering.",
    )
    
    # Timestamps
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
```

---

## Field Mapping by Event Type

### How Source Records Map to Timeline Events

| Event Type | event_source (examples) | Source Table | source_record_id | event_timestamp | title | subtitle |
|------------|--------------------------|--------------|------------------|-----------------|-------|----------|
| `EMAIL` | `EMAIL_GMAIL`, `EMAIL_OUTLOOK` | `deal_email_associations` | `association_id` | `email_date` | Email subject | From: sender |
| `MEETING` | `CALENDAR_GOOGLE`, `CALENDAR_OUTLOOK`, `TEAMS` | `calendar_event_deal_associations` | `association_id` | `CalendarEvent.start_at` | Meeting title | Duration (40 mins) |
| `MESSAGE` | `SLACK`, `TEAMS` | Future table | TBD | TBD | Message preview | Channel name |
| `STAGE_CHANGE` | `CRM_HUBSPOT`, `CRM_SALESFORCE` | `crm_deal_stage_history` | `history_id` | `entered_at` | "Stage Updated" | "Stage: {from} → {to}" |
| `DEAL_CREATED` | `CRM_HUBSPOT`, `CRM_SALESFORCE` | `crm_deals` | `deal_id` | `created_at` | "Deal Created" | "Opportunity created by {owner}" |
| `BLOCKER_ADDED` | `SYSTEM` | Future table | TBD | TBD | Blocker title | Status |

### Metadata Examples by Event Type

```json
// EMAIL
{
    "thread_count": 3,
    "has_attachments": true,
    "from_email": "sarah@acme.com",
    "internet_message_id": "abc123@gmail.com"
}

// MEETING
{
    "duration_minutes": 40,
    "location": "Zoom",
    "meeting_prep_status": "COMPLETED",
    "has_transcript": true
}

// STAGE_CHANGE
{
    "from_stage": "Qualification",
    "to_stage": "Discovery",
    "updated_by": "John (AE)"
}

// BLOCKER
{
    "blocker_type": "technical",
    "priority": "high",
    "assigned_to": "engineering"
}
```

---

## API Design

### Timeline Retrieval Endpoint

```
GET /api/v1/deals/{deal_id}/timeline
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 20 | Number of events per page |
| `offset` | int | 0 | Pagination offset |
| `event_types` | list[str] | all | Filter by event type(s) |
| `start_date` | datetime | null | Filter events after this date |
| `end_date` | datetime | null | Filter events before this date |
| `sort` | str | "desc" | Sort order (desc = newest first) |
| `include_excluded` | bool | false | Include events where `excluded_at` is set |

**Response:**

```json
{
    "deal_id": "uuid",
    "total_count": 45,
    "events": [
        {
            "timeline_event_id": "uuid",
            "event_type": "MEETING",
            "event_source": "CALENDAR_GOOGLE",
            "event_timestamp": "2025-10-15T11:00:00Z",
            "title": "PoC Kickoff Call - Acme Corp",
            "subtitle": "Duration: 40 mins",
            "direction": null,
            "status": "CONFIRMED",
            "participant_names": ["Mark (CTO, Acme)", "John (AE)", "Priya (SE)"],
            "participant_count": 3,
            "source_record_id": "assoc-uuid"
        },
        {
            "timeline_event_id": "uuid",
            "event_type": "EMAIL",
            "event_source": "EMAIL_GMAIL",
            "event_timestamp": "2025-10-10T10:40:00Z",
            "title": "Re: Sandbox Credentials Shared",
            "subtitle": "From: Sarah (Acme)",
            "direction": "INBOUND",
            "status": "CONFIRMED",
            "participant_names": ["Sarah (Acme)", "Priya (SE)"],
            "participant_count": 2,
            "metadata": {
                "thread_count": 3
            }
        }
    ],
    "pagination": {
        "limit": 20,
        "offset": 0,
        "has_more": true
    }
}
```

### Event Detail Fetch (On Click)

When user clicks a timeline event, fetch full details from the source table:

```
GET /api/v1/timeline/{timeline_event_id}/details
```

This endpoint reads `event_type`, `event_source`, and `source_record_id` from the timeline event, then fetches and returns the full record from the appropriate storage.

---

## Population Strategy

### When to Create Timeline Events

Timeline events should be created **automatically** when:

1. **Email Association Created** → Create `EMAIL` timeline event
2. **Meeting Association Created** → Create `MEETING` timeline event
3. **Deal Stage Changes** → Create `STAGE_CHANGE` timeline event
4. **Deal Created** → Create `DEAL_CREATED` timeline event
5. **Future: Blocker/Risk Added** → Create corresponding timeline event

### Implementation Approach

**Option A: Inline Creation (Recommended for MVP)**

- Add timeline event creation in the same transaction when creating associations
- Ensures consistency; no separate job needed
- Example: In `DealEmailMatcher.create_association()`, also create timeline event

**Option B: Background Job (For Future Bulk Operations)**

- Celery task to backfill timeline events from existing associations
- Useful for: migration, data repair, bulk imports

### Code Integration Points

```python
# In deal_email_matcher.py - after creating DealEmailAssociation
async def _create_email_association(self, ...):
    # ... existing association creation code ...
    
    # Create timeline event
    timeline_event = DealTimelineEvent(
        organisation_id=self.organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.EMAIL,
        event_source=self._get_email_source(email),
        event_timestamp=email.received_at,
        source_record_id=str(association.association_id),
        title=email.subject or "(No Subject)",
        subtitle=f"From: {email.from_name or email.from_email}",
        direction=association.direction.value,
        status=association.status.value,
        participant_names=self._extract_participant_names(email),
        participant_count=len(email.to_emails) + len(email.cc_emails) + 1,
        metadata={
            "has_attachments": email.has_attachments,
            "internet_message_id": association.internet_message_id,
        }
    )
    session.add(timeline_event)
```

---

## Database Migration

### Alembic Migration Script Outline

```python
def upgrade():
    # 1. Create enum types (if not using string columns)
    
    # 2. Create deal_timeline_events table
    op.create_table(
        'deal_timeline_events',
        sa.Column('timeline_event_id', UUID(), primary_key=True),
        sa.Column('organisation_id', UUID(), nullable=False),
        sa.Column('deal_id', UUID(), sa.ForeignKey('crm_deals.deal_id'), nullable=False),
        sa.Column('event_type', sa.String(32), nullable=False),
        sa.Column('event_source', sa.String(32), nullable=False),
        sa.Column('event_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_record_id', sa.String(512), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('subtitle', sa.String(500), nullable=True),
        sa.Column('direction', sa.String(32), nullable=True),
        sa.Column('status', sa.String(32), nullable=True),
        sa.Column('excluded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('participant_names', JSONB, server_default="'[]'::jsonb"),
        sa.Column('participant_count', sa.Integer, default=0),
        sa.Column('metadata', JSONB, server_default="'{}'::jsonb"),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    
    # 3. Create indexes
    op.create_index('ix_timeline_deal_time', 'deal_timeline_events', ['deal_id', 'event_timestamp'])
    op.create_index('ix_timeline_deal_type_time', 'deal_timeline_events', ['deal_id', 'event_type', 'event_timestamp'])
    op.create_index('ix_timeline_org_time', 'deal_timeline_events', ['organisation_id', 'event_timestamp'])
    
    # 4. Create unique constraint
    op.create_unique_constraint('uq_timeline_event_source', 'deal_timeline_events', 
                                ['deal_id', 'event_type', 'source_record_id'])


def downgrade():
    op.drop_table('deal_timeline_events')
```

### Backfill Script for Existing Data

A one-time migration script to populate timeline events from existing associations:

```python
async def backfill_timeline_events(session: AsyncSession, organisation_id: UUID):
    """Backfill timeline events from existing email and meeting associations."""
    
    # 1. Backfill from email associations
    email_assocs = await session.execute(
        select(DealEmailAssociation, Email)
        .join(Email, DealEmailAssociation.internet_message_id == Email.internet_message_id)
        .where(DealEmailAssociation.organisation_id == organisation_id)
    )
    
    for assoc, email in email_assocs:
        timeline_event = DealTimelineEvent(...)  # Map fields
        session.add(timeline_event)
    
    # 2. Backfill from meeting associations
    # ... similar pattern ...
    
    # 3. Backfill from stage history
    # ... similar pattern ...
    
    await session.commit()
```

---

## Performance Considerations

### Query Patterns

| Query | Index Used | Expected Performance |
|-------|------------|---------------------|
| All events for deal (sorted; default view excludes `excluded_at`) | `ix_timeline_deal_time` | < 10ms for 1000 events |
| Filtered by type | `ix_timeline_deal_type_time` | < 5ms |
| Paginated (LIMIT/OFFSET) | Same indexes | Consistent with pagination |

Note: if we expect a high volume of excluded events, we can add a Postgres partial index later (e.g., on `(deal_id, event_timestamp)` where `excluded_at IS NULL`) to keep default queries maximally efficient.

### Storage Estimates

| Field | Avg Size | Notes |
|-------|----------|-------|
| Fixed columns | ~200 bytes | UUIDs, timestamps, enums |
| Title/subtitle | ~100 bytes | Truncated for display |
| Participants JSON | ~150 bytes | Max 4 names stored |
| Metadata JSON | ~100 bytes | Minimal preview data |
| **Total per event** | **~550 bytes** | |

For a deal with 100 events: ~55 KB  
For an org with 1000 deals × 100 events: ~55 MB

---

## Future Extensibility

### Adding New Event Types

To add a new event type (e.g., Slack/Teams messages):

1. Add new value to `TimelineEventType` enum
2. Add new source to `TimelineEventSource` enum
3. Create timeline event in the integration's sync/association logic
4. Define metadata schema for the new type
5. Update detail fetch endpoint to handle new source table

### Potential Enhancements

1. **Activity Feed** - Real-time updates via WebSocket
2. **Timeline Search** - Full-text search across event titles
3. **Timeline Analytics** - Engagement trends, activity gaps
4. **Grouping** - Group events by day/week for dense timelines
5. **Pinned Events** - Allow users to pin important events

---

## Implementation Phases

### Phase 1: MVP (Current Sprint)

- [x] Design document (this file)
- [ ] Create `DealTimelineEvent` model
- [ ] Create database migration
- [ ] Integrate with `DealEmailMatcher` (auto-create on email association)
- [ ] Integrate with `DealMeetingMatcher` (auto-create on meeting association)
- [ ] Create timeline API endpoint

### Phase 2: Enhancements

- [ ] Backfill script for existing data
- [ ] Stage change events from `crm_deal_stage_history`
- [ ] Deal created event
- [ ] Timeline detail fetch endpoint

### Phase 3: Future Sources

- [ ] Slack message integration
- [ ] Blocker/Risk events
- [ ] Task/Note events

---

## Open Questions for Review

1. **Soft Delete vs Hard Delete**: Should we soft-delete timeline events when associations are removed, or hard delete?

2. **Event Deduplication**: If an email association is deleted and recreated, should we create a new timeline event or update the existing one?

3. **Status Updates**: When association status changes (SUGGESTED → CONFIRMED), should we update the timeline event status or keep it immutable?

4. **Exclusion Semantics**: For items that become rejected/cancelled later, do we set `excluded_at` (hide) or also keep `status` aligned (e.g., `REJECTED`)?

5. **Participant Name Resolution**: Should we store resolved names (from stakeholder/contact tables) or raw email addresses?

6. **Event Grouping**: Should the table support grouping related events (e.g., email thread as single entry)?

---

## Timeline Sync Strategy - Multi-Tenant Scale Considerations

### Problem: Per-Event Task Overhead

Creating a separate Celery task for each email/meeting timeline event is **not scalable** for a multi-tenant SaaS:

| Scale Factor | Volume | Tasks if Per-Event |
|--------------|--------|-------------------|
| Organisations | 100 | - |
| Deals per org | 50 | - |
| Events per deal | 100 | - |
| **Total** | 500,000 events | **500,000 tasks** ❌ |

**Problems with per-event tasks:**

- Session acquisition overhead per task (~50-100ms each)
- Celery queue serialization/deserialization overhead
- Redis memory pressure from task queue
- Worker context switching overhead
- Network round-trips between worker and broker

### Recommended Approach: Batch Processing Within Existing Tasks

Instead of creating separate timeline tasks, **integrate timeline creation directly into existing matcher services** within the same database transaction.

#### Strategy A: Inline Timeline Creation (Recommended for Real-Time)

Create timeline events in the same transaction when creating associations:

```python
# In DealEmailMatcher._create_association() or process_deal_emails()
async def _create_association(self, deal_id, email, ...):
    # 1. Create the association (existing code)
    association = DealEmailAssociation(...)
    self.session.add(association)
    
    # 2. Create timeline event IN THE SAME TRANSACTION
    timeline_event = DealTimelineEvent(
        organisation_id=self.organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.EMAIL,
        event_source=self._determine_email_source(),
        event_timestamp=email.received_at,
        source_record_id=str(association.association_id),
        title=email.subject or "(No Subject)",
        subtitle=f"From: {email.from_name or email.from_email}",
        direction=association.direction.value,
        status=association.status.value,
        # ... other fields
    )
    self.session.add(timeline_event)
    
    # Single flush/commit handles both
```

**Benefits:**

- Zero additional tasks or network calls
- Atomic consistency (association + timeline in one transaction)
- No extra session acquisition overhead
- Uses existing batch processing patterns

#### Strategy B: Deal-Level Batch Sync Task

For backfill or repair scenarios, provide a **deal-level** sync task:

```python
@app.task(name="deal.sync_deal_timeline")
def sync_deal_timeline_task(organisation_id: str, deal_id: str, event_types: list[str] | None = None):
    """
    Sync all timeline events for a single deal.
    
    Processes all associations in one pass:
    - Fetches all email associations for the deal
    - Fetches all meeting associations for the deal
    - Creates/updates timeline events in batch
    - Single commit at the end
    
    Args:
        organisation_id: Org UUID
        deal_id: Deal UUID
        event_types: Optional filter (["EMAIL", "MEETING"]) - defaults to all
    """
    async def _run():
        async for session in acquire_org_async_session(UUID(organisation_id)):
            service = DealTimelineSyncService(session, UUID(organisation_id))
            return await service.sync_deal_timeline(
                deal_id=UUID(deal_id),
                event_types=event_types,
            )
    return run_async(_run())
```

#### Strategy C: Organisation-Level Batch Sync Task

For full org backfill or periodic reconciliation:

```python
@app.task(name="deal.sync_org_timeline")
def sync_org_timeline_task(organisation_id: str, deal_ids: list[str] | None = None):
    """
    Sync timeline events for all deals in an organisation (or specific deals).
    
    Processes in batches of 50 deals to manage memory:
    - Batch 1: deals 1-50, create all timeline events, commit
    - Batch 2: deals 51-100, create all timeline events, commit
    - ...
    
    Uses INSERT ... ON CONFLICT DO UPDATE for idempotency.
    """
```

### Recommended Service: `DealTimelineSyncService`

Create a dedicated service for efficient batch timeline operations:

```python
class DealTimelineSyncService:
    """Service for batch timeline sync operations."""
    
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
    
    async def sync_deal_timeline(
        self,
        deal_id: UUID,
        event_types: list[TimelineEventType] | None = None,
    ) -> dict[str, int]:
        """
        Sync all timeline events for a deal in one pass.
        
        Returns:
            {"emails_synced": N, "meetings_synced": M, "total_created": X, "total_updated": Y}
        """
        results = {"emails_synced": 0, "meetings_synced": 0, "total_created": 0, "total_updated": 0}
        
        # Fetch existing timeline events for this deal (for upsert logic)
        existing_events = await self._get_existing_timeline_events(deal_id)
        existing_keys = {(e.event_type, e.source_record_id) for e in existing_events}
        
        if not event_types or TimelineEventType.EMAIL in event_types:
            results.update(await self._sync_email_events(deal_id, existing_keys))
        
        if not event_types or TimelineEventType.MEETING in event_types:
            results.update(await self._sync_meeting_events(deal_id, existing_keys))
        
        await self.session.flush()
        return results
    
    async def _sync_email_events(self, deal_id: UUID, existing_keys: set) -> dict:
        """Batch sync all email associations → timeline events."""
        # Fetch all email associations for this deal with emails joined
        stmt = (
            select(DealEmailAssociation, Email)
            .outerjoin(Email, DealEmailAssociation.internet_message_id == Email.internet_message_id)
            .where(
                DealEmailAssociation.organisation_id == self.organisation_id,
                DealEmailAssociation.deal_id == deal_id,
            )
        )
        result = await self.session.execute(stmt)
        
        created, updated = 0, 0
        for assoc, email in result:
            key = (TimelineEventType.EMAIL, str(assoc.association_id))
            if key in existing_keys:
                # Optionally update existing event
                updated += 1
                continue
            
            # Create new timeline event
            timeline_event = self._build_email_timeline_event(deal_id, assoc, email)
            self.session.add(timeline_event)
            created += 1
        
        return {"emails_synced": created + updated, "total_created": created, "total_updated": updated}
    
    async def _sync_meeting_events(self, deal_id: UUID, existing_keys: set) -> dict:
        """Batch sync all meeting associations → timeline events."""
        # Similar pattern...
```

### Task Hierarchy for Timeline Operations

```
┌─────────────────────────────────────────────────────────────────┐
│  Scheduled Tasks (Celery Beat)                                  │
├─────────────────────────────────────────────────────────────────┤
│  trigger_deal_email_processing_all_orgs (hourly)                │
│  trigger_deal_meeting_processing_all_orgs (every 3 hours)       │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Per-Org Tasks                                                  │
├─────────────────────────────────────────────────────────────────┤
│  process_deal_emails_task(org_id)                               │
│  process_deal_meetings_task(org_id)                             │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Inside task: DealEmailMatcher / DealMeetingMatcher     │   │
│  │  - Creates associations                                  │   │
│  │  - Creates timeline events (inline, same transaction)   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  On-Demand Tasks (Manual / Backfill)                            │
├─────────────────────────────────────────────────────────────────┤
│  sync_deal_timeline_task(org_id, deal_id)     ← Single deal     │
│  sync_org_timeline_task(org_id)               ← All deals       │
└─────────────────────────────────────────────────────────────────┘
```

### Implementation Recommendation

**Phase 1 (Immediate):**

1. **Do NOT use per-event timeline tasks** in production matchers
2. Modify `DealEmailMatcher` to create timeline events inline when creating associations
3. Modify `DealMeetingMatcher` to create timeline events inline when creating associations
4. Keep the per-event tasks only for testing/debugging purposes

**Phase 2 (Backfill & Maintenance):**

1. Create `DealTimelineSyncService` for batch operations
2. Create `sync_deal_timeline_task` for single-deal sync
3. Create `sync_org_timeline_task` for org-wide backfill

**Phase 3 (Exclusion Handling):**

1. When association status changes to REJECTED/CANCELLED:
   - Update timeline event `status` field
   - Set `excluded_at` timestamp
   - Do this inline in the same transaction, not via separate task

### Performance Comparison

| Approach | Tasks per 1000 events | Session acquisitions | Network calls |
|----------|----------------------|---------------------|---------------|
| Per-event tasks | 1000 | 1000 | 2000+ |
| Inline in matcher | 0 | 0 (reuses existing) | 0 |
| Deal-level batch | 1 per deal | 1 per deal | ~2 per deal |
| Org-level batch | 1 per org | 1 per org | ~2 per org |

**Inline creation is 1000x more efficient** than per-event tasks.

---

## Approval Checklist

- [ ] Table schema approved
- [ ] Index strategy approved
- [ ] API design approved
- [ ] Integration approach approved
- [ ] Migration strategy approved
- [ ] **Timeline sync strategy approved** (inline vs batch)

**Please review and provide feedback before implementation begins.**
