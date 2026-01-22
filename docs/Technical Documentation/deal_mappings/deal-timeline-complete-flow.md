# Deal Timeline - Complete Flow Documentation

This document provides a comprehensive overview of how Deal Timeline events are created, synced, and served in the CopilotGTM backend.

## Overview

The Deal Timeline provides a chronological view of all activities related to a deal:

| Event Type | Description | Source |
|------------|-------------|--------|
| `EMAIL` | Email interactions matched to deal | Gmail, Outlook |
| `MEETING` | Calendar meetings matched to deal | Google Calendar, Outlook Calendar |
| `STAGE_CHANGE` | Deal stage transitions | HubSpot, Salesforce |
| `DEAL_CREATED` | Deal creation event | HubSpot, Salesforce |
| `MESSAGE` | Channel message summaries | Teams, Slack (Future) |

---

## Architecture Overview

```
                              ┌─────────────────────────────────────┐
                              │           API Layer                 │
                              │   GET /deals/{id}/timeline          │
                              │         DealTimelineService         │
                              └─────────────────┬───────────────────┘
                                                │
                              ┌─────────────────▼───────────────────┐
                              │       DealTimelineEvent Table       │
                              │  (Denormalized for fast queries)    │
                              └─────────────────┬───────────────────┘
                                                │
              ┌─────────────────────────────────┼─────────────────────────────────┐
              │                                 │                                 │
    ┌─────────▼─────────┐           ┌──────────▼──────────┐           ┌──────────▼──────────┐
    │   Inline Creation │           │   Inline Creation   │           │   Backfill Tasks    │
    │  (Email/Meeting)  │           │  (CRM Sync)         │           │  (Migration/Repair) │
    └───────────────────┘           └─────────────────────┘           └─────────────────────┘
```

---

## Event Types and Their Sources

### 1. EMAIL Events

**Trigger:** When an email is matched to a deal via stakeholder email addresses.

**Created In:** `DealEmailMatcher._create_association()`

**File:** `src/workers/deal_worker/services/deal_email_matcher.py:617`

**Flow:**
```
Email Sync Task
      │
      ▼
DealEmailMatcher.match_emails_to_deals()
      │
      ├── Find deals with matching stakeholder emails
      │
      ▼
_create_association()
      │
      ├── Create DealEmailAssociation
      ├── resolve_email_event_source() → EMAIL_GMAIL or EMAIL_OUTLOOK
      ├── build_email_timeline_event()
      │
      ▼
DealTimelineEvent (EMAIL) created in same transaction
```

**Event Metadata:**
```python
{
    "has_attachments": bool,
    "attachment_count": int,
    "recipient_count": int,
    "is_from_internal": bool
}
```

---

### 2. MEETING Events

**Trigger:** When a calendar event is matched to a deal via attendee emails.

**Created In:** `DealMeetingMatcher._create_meeting_association()`

**File:** `src/workers/deal_worker/services/deal_meeting_matcher.py:708`

**Flow:**
```
Calendar Sync Task
      │
      ▼
DealMeetingMatcher.match_meetings_to_deals()
      │
      ├── Find deals with matching attendee emails
      │
      ▼
_create_meeting_association()
      │
      ├── Create DealMeetingAssociation
      ├── resolve_calendar_event_source() → CALENDAR_GOOGLE or CALENDAR_OUTLOOK
      ├── build_meeting_timeline_event()
      │
      ▼
DealTimelineEvent (MEETING) created in same transaction
```

**Event Metadata:**
```python
{
    "duration_minutes": int,
    "attendee_count": int,
    "location": str | None,
    "conference_type": str | None,  # "teams", "zoom", "meet"
    "is_recurring": bool
}
```

---

### 3. STAGE_CHANGE Events

**Trigger:** When a deal's stage changes in the CRM (HubSpot/Salesforce).

**Created In:** `DealSyncService._upsert_stage_history()`

**File:** `src/service/integrations/crm/entities/deal_sync_service.py:1008`

**Flow:**
```
CRM Sync (HubSpot/Salesforce)
      │
      ▼
DealSyncService.sync()
      │
      ├── _extract_stage_history() - Extract stage transitions from CRM data
      │
      ▼
_upsert_stage_history()
      │
      ├── Insert/Update CRMDealStageHistory record
      ├── resolve_crm_event_source() → CRM_HUBSPOT or CRM_SALESFORCE
      ├── build_stage_change_timeline_event()
      │
      ▼
DealTimelineEvent (STAGE_CHANGE) created in same transaction
```

**Builder Function:** `build_stage_change_timeline_event()`

**File:** `src/service/crm/deal_timeline_sync_service.py`

**Event Metadata:**
```python
{
    "old_stage": str | None,
    "new_stage": str,
    "stage_label": str | None,
    "from_stage_label": str | None
}
```

**Title Format:**
- With previous stage: `"Stage: Discovery → Evaluation"`
- Initial stage: `"Stage: Discovery"`

---

### 4. DEAL_CREATED Events

**Trigger:** When a new deal is synced from CRM for the first time.

**Created In:** `DealSyncService.sync()` when `is_new=True`

**Files:**
- `src/service/integrations/crm/entities/deal_sync_service.py:136` (initial sync)
- `src/service/integrations/crm/entities/deal_sync_service.py:330` (incremental sync)

**Flow:**
```
CRM Sync (HubSpot/Salesforce)
      │
      ▼
DealSyncService.sync()
      │
      ├── Check if deal exists
      │
      ▼
is_new = True (new deal)
      │
      ├── Create CRMDeal record
      ├── resolve_crm_event_source() → CRM_HUBSPOT or CRM_SALESFORCE
      ├── build_deal_created_timeline_event()
      │
      ▼
DealTimelineEvent (DEAL_CREATED) created in same transaction
```

**Builder Function:** `build_deal_created_timeline_event()`

**File:** `src/service/crm/deal_timeline_sync_service.py`

**Event Metadata:**
```python
{
    "initial_stage": str | None,
    "pipeline_name": str | None,
    "owner_id": str | None,
    "amount": float | None
}
```

**Title Format:** `"Deal Created: {deal_name}"`

**Subtitle:** `"Initial Stage: {stage_name}"` (if available)

---

## Source Resolution

The `TimelineEventSourceResolver` determines the `event_source` field based on integration type.

**File:** `src/service/crm/timeline_event_source_resolver.py`

| Integration Type | Event Source |
|-----------------|--------------|
| Gmail | `EMAIL_GMAIL` |
| Outlook Email | `EMAIL_OUTLOOK` |
| Google Calendar | `CALENDAR_GOOGLE` |
| Outlook Calendar | `CALENDAR_OUTLOOK` |
| HubSpot | `CRM_HUBSPOT` |
| Salesforce | `CRM_SALESFORCE` |

**Functions:**
- `resolve_email_event_source(integration_type)` - For EMAIL events
- `resolve_calendar_event_source(integration_type)` - For MEETING events
- `resolve_crm_event_source(integration)` - For STAGE_CHANGE and DEAL_CREATED events

---

## Inline Creation Pattern (Real-Time)

All timeline events are created **inline** within the same database transaction as their source data. This ensures:

1. **Atomicity**: Timeline event is created only if source data is successfully saved
2. **Consistency**: No orphaned timeline events or missing events
3. **Real-time**: Events appear immediately after sync

**Pattern:**
```python
# In the service that creates source data:

# 1. Create the source record
association = DealEmailAssociation(...)
self.db.add(association)

# 2. Resolve the event source
event_source = resolve_email_event_source(integration_type)

# 3. Build the timeline event
timeline_event = build_email_timeline_event(
    organisation_id=org_id,
    association=association,
    email=email,
    event_source=event_source,
)

# 4. Add to same transaction
self.db.add(timeline_event)

# 5. Commit atomically
await self.db.commit()
```

---

## Backfill Tasks (Migration/Repair)

Backfill tasks are used for:
- Populating timeline events for existing deals (migration)
- Repairing missing timeline events
- One-time data fixes

**NOT used for real-time creation** - all normal operations use inline creation.

### Task Hierarchy

```
Tier 1: sync_all_organisations_timeline_task
   │
   │    For each organisation
   ▼
Tier 2: sync_organisation_timeline_task
   │
   │    For each deal (batched)
   ▼
Tier 3: sync_deal_timeline_task
   │
   │    For each event type
   ▼
Create missing DealTimelineEvent records
```

**Files:**
- `src/workers/deal_worker/tasks/deal_mappers/timeline_sync_tasks.py`
- `src/service/crm/deal_timeline_sync_service.py`

### Task Parameters

```python
sync_deal_timeline_task(
    deal_id: str,                    # Deal to sync
    event_types: list[str] | None,   # Filter by type (EMAIL, MEETING, STAGE_CHANGE, DEAL_CREATED)
)

sync_organisation_timeline_task(
    organisation_id: str,
    deal_ids: list[str] | None,      # Specific deals or all
    event_types: list[str] | None,   # Filter by type
    batch_size: int = 50,
)
```

### Deduplication

Backfill uses `INSERT ... ON CONFLICT DO NOTHING` to avoid duplicates:

```python
# Unique constraint: (event_type, source_record_id)
stmt = insert(DealTimelineEvent).values(...).on_conflict_do_nothing(
    index_elements=["event_type", "source_record_id"]
)
```

---

## API Layer

**Endpoint:** `GET /users/crm/deals/{deal_id}/timeline`

**Service:** `DealTimelineService`

**File:** `src/service/crm/deal_timeline_service.py`

### Response Structure

```json
{
  "items": [
    {
      "timeline_event_id": "uuid",
      "event_type": "STAGE_CHANGE",
      "event_source": "CRM_HUBSPOT",
      "event_timestamp": "2025-01-15T14:30:00Z",
      "title": "Stage: Discovery → Evaluation",
      "subtitle": null,
      "event_data": {
        "old_stage": "Discovery",
        "new_stage": "Evaluation",
        "old_stage_label": "Discovery",
        "new_stage_label": "Evaluation"
      }
    },
    {
      "timeline_event_id": "uuid",
      "event_type": "EMAIL",
      "event_source": "EMAIL_GMAIL",
      "event_timestamp": "2025-01-14T10:00:00Z",
      "title": "Re: Project Discussion",
      "subtitle": "From: john@example.com",
      "event_data": {
        "has_attachments": true,
        "attachment_count": 2,
        "sender_name": "John Doe",
        "sender_email": "john@example.com"
      }
    }
  ],
  "total": 25,
  "page": 1,
  "page_size": 20
}
```

### Preview Data Builder

The `_build_preview_event_data()` method transforms `event_metadata` into `event_data` for API responses:

```python
def _build_preview_event_data(self, event: DealTimelineEvent) -> dict:
    if event_type == TimelineEventType.EMAIL:
        return {"sender_name": ..., "has_attachments": ...}

    if event_type == TimelineEventType.MEETING:
        return {"duration_minutes": ..., "attendee_count": ...}

    if event_type == TimelineEventType.STAGE_CHANGE:
        return {"old_stage": ..., "new_stage": ...}

    if event_type == TimelineEventType.DEAL_CREATED:
        return {"initial_stage": ..., "pipeline_name": ...}
```

---

## Database Schema

### DealTimelineEvent Table

```sql
CREATE TABLE deal_timeline_event (
    timeline_event_id UUID PRIMARY KEY,
    organisation_id UUID NOT NULL,
    deal_id UUID NOT NULL REFERENCES crm_deal(deal_id),
    event_type VARCHAR NOT NULL,  -- EMAIL, MEETING, STAGE_CHANGE, DEAL_CREATED, MESSAGE
    event_source VARCHAR NOT NULL, -- EMAIL_GMAIL, CRM_HUBSPOT, etc.
    event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    source_record_id VARCHAR NOT NULL,
    title VARCHAR,
    subtitle VARCHAR,
    event_metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    CONSTRAINT uq_timeline_event_source UNIQUE (event_type, source_record_id)
);

CREATE INDEX ix_timeline_deal_timestamp ON deal_timeline_event(deal_id, event_timestamp DESC);
CREATE INDEX ix_timeline_org ON deal_timeline_event(organisation_id);
```

### CRMDealStageHistory Table

Used for STAGE_CHANGE events:

```sql
CREATE TABLE crm_deal_stage_history (
    history_id UUID PRIMARY KEY,
    deal_id UUID NOT NULL REFERENCES crm_deal(deal_id),
    stage_external_id VARCHAR,
    stage_name VARCHAR,
    stage_label VARCHAR,
    from_stage_external_id VARCHAR,
    from_stage_name VARCHAR,
    from_stage_label VARCHAR,
    entered_at TIMESTAMP WITH TIME ZONE,
    external_id VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

---

## Complete Flow Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           DATA SOURCES                                        │
├────────────────┬────────────────┬────────────────┬───────────────────────────┤
│     Gmail      │    Outlook     │    HubSpot     │       Salesforce          │
│     Email      │    Calendar    │      CRM       │          CRM              │
└───────┬────────┴───────┬────────┴───────┬────────┴───────────┬───────────────┘
        │                │                │                    │
        ▼                ▼                ▼                    ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                           SYNC LAYER                                          │
├───────────────────┬───────────────────┬───────────────────────────────────────┤
│   Email Sync      │  Calendar Sync    │           CRM Sync                    │
│     Task          │     Task          │   DealSyncService.sync()              │
└─────────┬─────────┴─────────┬─────────┴─────────────────┬─────────────────────┘
          │                   │                           │
          ▼                   ▼                           ▼
┌───────────────────┬───────────────────┬─────────────────┬─────────────────────┐
│  DealEmailMatcher │ DealMeetingMatcher│   is_new=True   │ _upsert_stage_      │
│  _create_         │ _create_meeting_  │   (New Deal)    │    history()        │
│   association()   │   association()   │                 │                     │
└─────────┬─────────┴─────────┬─────────┴────────┬────────┴──────────┬──────────┘
          │                   │                  │                   │
          ▼                   ▼                  ▼                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                       TIMELINE EVENT CREATION                                 │
├───────────────────┬───────────────────┬─────────────────┬─────────────────────┤
│  build_email_     │  build_meeting_   │  build_deal_    │  build_stage_       │
│  timeline_event() │  timeline_event() │  created_       │  change_timeline_   │
│                   │                   │  timeline_event │  event()            │
└─────────┬─────────┴─────────┬─────────┴────────┬────────┴──────────┬──────────┘
          │                   │                  │                   │
          ▼                   ▼                  ▼                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                      DealTimelineEvent Table                                  │
│                                                                               │
│  ┌─────────┐  ┌─────────┐  ┌──────────────┐  ┌──────────────┐                │
│  │  EMAIL  │  │ MEETING │  │ DEAL_CREATED │  │ STAGE_CHANGE │                │
│  └─────────┘  └─────────┘  └──────────────┘  └──────────────┘                │
└───────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                           API LAYER                                           │
│                                                                               │
│    GET /deals/{id}/timeline  →  DealTimelineService  →  Timeline Response    │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Key Files Reference

| Component | File | Description |
|-----------|------|-------------|
| Timeline Service | `src/service/crm/deal_timeline_service.py` | API layer, queries, enrichment |
| Sync Service | `src/service/crm/deal_timeline_sync_service.py` | Builder functions, backfill logic |
| Source Resolver | `src/service/crm/timeline_event_source_resolver.py` | Integration → EventSource mapping |
| Email Matcher | `src/workers/deal_worker/services/deal_email_matcher.py` | EMAIL event inline creation |
| Meeting Matcher | `src/workers/deal_worker/services/deal_meeting_matcher.py` | MEETING event inline creation |
| Deal Sync | `src/service/integrations/crm/entities/deal_sync_service.py` | STAGE_CHANGE, DEAL_CREATED inline |
| Backfill Tasks | `src/workers/deal_worker/tasks/deal_mappers/timeline_sync_tasks.py` | Migration/repair tasks |
| Models | `src/models/crm/timeline_models.py` | DealTimelineEvent, enums |
| Stage History | `src/models/crm/deals.py` | CRMDealStageHistory model |

---

## Detailed Query Logic (Backfill Service)

This section explains the exact queries used in `DealTimelineSyncService` for backfill operations, why each join exists, and potential gaps.

### Email Timeline Sync Query

```python
stmt = (
    select(DealEmailAssociation, Email, KnowledgeIntegration)
    .outerjoin(
        Email,
        (assoc_table.c.internet_message_id == email_table.c.internet_message_id)
        & (email_table.c.organisation_id == self.organisation_id),
    )
    .outerjoin(
        KnowledgeIntegration,
        email_table.c.integration_id == integration_table.c.integration_id,
    )
    .where(
        assoc_table.c.organisation_id == self.organisation_id,
        assoc_table.c.deal_id == deal_id,
    )
)
```

**Why this structure?**

| Join | Purpose | Explanation |
|------|---------|-------------|
| `DealEmailAssociation` | Base table | Contains deal-email links, unique by `internet_message_id + deal_id` |
| `→ Email` | Get email details | Subject, sender, attachments for timeline display |
| `→ KnowledgeIntegration` | Get integration type | Needed to resolve `event_source` (Gmail vs Outlook) |

**Why join KnowledgeIntegration?**

The `event_source` field (EMAIL_GMAIL or EMAIL_OUTLOOK) is determined by the integration type that synced the email. We need to know if it came from Gmail or Outlook to display the correct icon/label in the UI.

**Data relationship:**
```
DealEmailAssociation (1) ←─────────────── (N) Email
        │                                      │
        │ internet_message_id                  │ integration_id
        │ (same email, multiple copies)        │
        │                                      ▼
        └──────────────────────────────► KnowledgeIntegration
                                          (integration_type)
```

**Key insight:** The same email (`internet_message_id`) may exist multiple times in the `Email` table - once per user integration. But `DealEmailAssociation` is unique per `internet_message_id + deal_id`. The join picks ANY matching email copy (they have identical content) just to get the integration type.

#### EMAIL Sync Gaps & Issues

| Gap | Description | Impact | Mitigation |
|-----|-------------|--------|------------|
| **Multiple email copies** | Same `internet_message_id` exists N times (one per user integration). Query picks arbitrary one. | Minor - content is identical, only integration_type might differ | Could add `ORDER BY` or `DISTINCT ON` for determinism |
| **Email deleted** | If all Email copies are deleted but association remains | Timeline event has minimal data (subject from association only) | Association stores `email_subject` as backup |
| **Integration deleted** | If KnowledgeIntegration is deleted | Falls back to default `EMAIL_GMAIL` | Acceptable fallback |
| **No email record found** | Outer join returns NULL for email | Uses association's cached `email_subject`, `email_date` | Design handles this |

---

### Meeting Timeline Sync Query

```python
stmt = (
    select(CalendarEventDealAssociation, CalendarEvent)
    .outerjoin(
        CalendarEvent,
        (assoc_table.c.event_id == event_table.c.event_id)
        & (event_table.c.organisation_id == self.organisation_id),
    )
    .options(selectinload(CalendarEvent.attendees))
    .options(selectinload(CalendarEvent.user_states))
    .where(
        assoc_table.c.organisation_id == self.organisation_id,
        assoc_table.c.deal_id == deal_id,
    )
)

# Then bulk fetch integrations for all referenced integration_ids
integration_ids = set()
for _, event in rows:
    if event.integration_id:
        integration_ids.add(event.integration_id)
    for state in event.user_states:
        if state.integration_id:
            integration_ids.add(state.integration_id)
```

**Why this structure?**

| Join/Load | Purpose | Explanation |
|-----------|---------|-------------|
| `CalendarEventDealAssociation` | Base table | Contains deal-meeting links |
| `→ CalendarEvent` | Get meeting details | Title, start/end time, location |
| `→ attendees` (selectinload) | Get participant names | For preview display |
| `→ user_states` (selectinload) | Get per-user integration IDs | Multiple users may have this event |
| Bulk fetch `KnowledgeIntegration` | Get integration types | Resolve CALENDAR_GOOGLE vs CALENDAR_OUTLOOK |

**Why the complexity with user_states?**

A calendar event may be synced by multiple users (shared meeting). Each user's copy has a different `integration_id` in `CalendarEventUserState`. We need to pick one to determine `event_source`.

**Resolution logic:**
1. First check `event.integration_id`
2. If not available, look at `user_states`
3. Sort by `integration_id` for deterministic pick
4. Use first one's integration type

#### MEETING Sync Gaps & Issues

| Gap | Description | Impact | Mitigation |
|-----|-------------|--------|------------|
| **Multiple user states** | Same event synced by N users with different integrations | Minor - picks first alphabetically by UUID | Deterministic but arbitrary |
| **Mixed providers** | User A has Google Calendar, User B has Outlook Calendar for same meeting | Shows provider of first user alphabetically | Could show "multiple sources" |
| **Event deleted** | CalendarEvent deleted but association remains | Minimal timeline event (just "Meeting" title) | No cached data in association - **GAP** |
| **No attendees loaded** | Async context issues with lazy loading | Empty participant list | Using `selectinload` fixes this |

---

### Stage Change Timeline Sync Query

```python
stmt = (
    select(CRMDealStageHistory, KnowledgeIntegration)
    .outerjoin(
        KnowledgeIntegration,
        history_table.c.integration_id == integration_table.c.integration_id,
    )
    .where(
        history_table.c.organisation_id == self.organisation_id,
        history_table.c.deal_id == deal_id,
        history_table.c.is_deleted.is_(False),
    )
)
```

**Why this structure?**

| Join | Purpose | Explanation |
|------|---------|-------------|
| `CRMDealStageHistory` | Base table | Contains all stage transitions |
| `→ KnowledgeIntegration` | Get CRM type | HubSpot vs Salesforce |

**Source record ID:** Uses `external_id` from history (HubSpot's property history ID).

#### STAGE_CHANGE Sync Gaps & Issues

| Gap | Description | Impact | Mitigation |
|-----|-------------|--------|------------|
| **No stage labels in history** | `CRMDealStageHistory` stores `stage_external_id` but not display labels | Timeline shows IDs like "123456" instead of "Discovery" | **GAP** - Need to join with pipeline stages |
| **Missing from_stage on first entry** | Initial stage has no previous stage | Shows "Stage: Discovery" (no arrow) | Handled correctly |
| **Integration not stored** | If `integration_id` is NULL on history record | Falls back to CRM_HUBSPOT | Need to ensure integration_id is populated |

**Recommended fix for stage labels:**
```python
# Should also join CRMDealPipelineStage to get labels
.outerjoin(
    CRMDealPipelineStage,
    history_table.c.stage_external_id == stage_table.c.external_id,
)
```

---

### Deal Created Timeline Sync Query

```python
stmt = (
    select(CRMDeal, KnowledgeIntegration)
    .outerjoin(
        KnowledgeIntegration,
        deal_table.c.integration_id == integration_table.c.integration_id,
    )
    .where(
        deal_table.c.organisation_id == self.organisation_id,
        deal_table.c.deal_id == deal_id,
    )
)
```

**Why this structure?**

| Join | Purpose | Explanation |
|------|---------|-------------|
| `CRMDeal` | Base table | The deal itself |
| `→ KnowledgeIntegration` | Get CRM type | HubSpot vs Salesforce |

**Source record ID:** Uses `deal_id` (unique per deal, only one DEAL_CREATED event).

#### DEAL_CREATED Sync Gaps & Issues

| Gap | Description | Impact | Mitigation |
|-----|-------------|--------|------------|
| **source_created_at NULL** | Some deals may not have CRM creation timestamp | Uses `created_at` (sync time) instead | May show wrong date |
| **Deal name changes** | If deal name is updated after creation | Timeline shows old name | **GAP** - Could update on re-sync |
| **Initial stage changes** | Stage shown is current stage, not initial | Misleading if stage changed | **GAP** - Should store initial stage at creation time |

---

## Deduplication Strategy

### Unique Constraint
```sql
CONSTRAINT uq_timeline_event_source UNIQUE (event_type, source_record_id)
```

### How Each Event Type Uses It

| Event Type | `source_record_id` Value | Uniqueness Guarantee |
|------------|-------------------------|---------------------|
| EMAIL | `association_id` (UUID) | One event per email-deal association |
| MEETING | `association_id` (UUID) | One event per meeting-deal association |
| STAGE_CHANGE | `external_id` (CRM history ID) | One event per stage transition |
| DEAL_CREATED | `deal_id` (UUID) | One event per deal |

### Insert Logic
```python
stmt = (
    insert(DealTimelineEvent)
    .values(**values)
    .on_conflict_do_nothing(constraint="uq_timeline_event_source")
)
result = await self.session.execute(stmt)
return result.rowcount > 0  # True if inserted, False if duplicate
```

---

## Gap Summary & Recommendations

### Critical Gaps

| ID | Gap | Severity | Recommendation |
|----|-----|----------|----------------|
| G1 | Stage labels not shown (only IDs) | High | Join `CRMDealPipelineStage` in backfill query |
| G2 | DEAL_CREATED shows current stage, not initial | Medium | Store `initial_stage` at inline creation time |
| G3 | Meeting association has no cached data | Medium | Add `event_title`, `event_start_at` to association |

### Minor Gaps

| ID | Gap | Severity | Recommendation |
|----|-----|----------|----------------|
| G4 | Non-deterministic email copy selection | Low | Add `ORDER BY integration_id` |
| G5 | Mixed calendar providers not indicated | Low | Could show "Multiple Sources" badge |
| G6 | Deal name updates not reflected | Low | Could update title on re-sync |

### Data Integrity Checks

To verify timeline completeness, run:

```sql
-- Emails without timeline events
SELECT a.association_id, a.deal_id
FROM deal_email_association a
LEFT JOIN deal_timeline_event t
  ON t.event_type = 'EMAIL' AND t.source_record_id = a.association_id::text
WHERE t.timeline_event_id IS NULL;

-- Meetings without timeline events
SELECT a.association_id, a.deal_id
FROM calendar_event_deal_association a
LEFT JOIN deal_timeline_event t
  ON t.event_type = 'MEETING' AND t.source_record_id = a.association_id::text
WHERE t.timeline_event_id IS NULL;

-- Stage changes without timeline events
SELECT h.external_id, h.deal_id
FROM crm_deal_stage_history h
LEFT JOIN deal_timeline_event t
  ON t.event_type = 'STAGE_CHANGE' AND t.source_record_id = h.external_id
WHERE t.timeline_event_id IS NULL AND h.is_deleted = false;

-- Deals without DEAL_CREATED event
SELECT d.deal_id, d.name
FROM crm_deal d
LEFT JOIN deal_timeline_event t
  ON t.event_type = 'DEAL_CREATED' AND t.source_record_id = d.deal_id::text
WHERE t.timeline_event_id IS NULL AND d.is_deleted = false;
```

---

## Stakeholder Considerations

### REMOVED Stakeholders

Stakeholders with `approval_status = REMOVED` are:
- **NOT reactivated** by CRM sync (respected in `stakeholder_sync_service.py`)
- **NOT used** for email/calendar matching (filtered in matchers)

This ensures users who explicitly remove a stakeholder don't have them automatically restored.

**File:** `src/workers/deal_worker/services/stakeholder_sync_service.py`

---

## Timeline View Example

```
┌─────────────────────────────────────────────────────────────────┐
│  Jan 15, 2025                                                   │
├─────────────────────────────────────────────────────────────────┤
│  2:30 PM ─●─ 🔄 HubSpot CRM                                     │
│              Stage: Discovery → Evaluation                      │
├─────────────────────────────────────────────────────────────────┤
│  Jan 14, 2025                                                   │
├─────────────────────────────────────────────────────────────────┤
│  10:00 AM ─●─ 📧 Gmail                                          │
│              Re: Project Discussion                             │
│              From: John Doe                                     │
├─────────────────────────────────────────────────────────────────┤
│  Jan 10, 2025                                                   │
├─────────────────────────────────────────────────────────────────┤
│  3:00 PM ─●─ 📅 Google Calendar                                 │
│              Kickoff Meeting                                    │
│              Duration: 60 min | Attendees: 5                    │
├─────────────────────────────────────────────────────────────────┤
│  Jan 5, 2025                                                    │
├─────────────────────────────────────────────────────────────────┤
│  9:00 AM  ─●─ ✨ HubSpot CRM                                    │
│              Deal Created: Acme Corp Enterprise                 │
│              Initial Stage: Discovery                           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Summary

| Aspect | Approach |
|--------|----------|
| Real-time creation | Inline in same transaction as source data |
| Backfill | Task-based, ON CONFLICT DO NOTHING |
| Deduplication | Unique constraint on (event_type, source_record_id) |
| Source resolution | Integration type → EventSource mapping |
| New event types | STAGE_CHANGE and DEAL_CREATED via CRM sync |
| REMOVED stakeholders | Respected, not reactivated |
