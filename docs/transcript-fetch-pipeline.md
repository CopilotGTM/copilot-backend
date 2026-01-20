# Transcript Fetch & Meeting Summary Pipeline

This document describes the automated pipeline for fetching meeting transcripts and generating AI-powered meeting summaries.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Three-Tier Task Structure](#three-tier-task-structure)
4. [Services](#services)
5. [Data Flow](#data-flow)
6. [Database Models](#database-models)
7. [Configuration](#configuration)
8. [Error Handling & Retry Logic](#error-handling--retry-logic)
9. [Logging](#logging)

---

## Overview

The transcript fetch pipeline automatically:
1. **Fetches transcripts** from online meetings (Teams, Zoom) for deal-associated calendar events
2. **Generates AI meeting summaries** for meetings with available transcripts
3. **Triggers deal summary updates** after processing all meetings for a deal

### Key Design Principles

- **Deal-Centric Processing**: Each deal completes fully (transcripts → summaries) before moving to the next
- **Sequential Execution**: Meetings within a deal are processed one at a time to avoid resource contention
- **Fresh Session Pattern**: Database sessions are acquired, used, and released promptly - never held during external API calls
- **Retry with Backoff**: Failed fetches retry up to MAX_ATTEMPTS times with exponential backoff

---

## Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CELERY BEAT SCHEDULER                                │
│                    (Every 30 min at :15 and :45)                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: trigger_transcript_fetch_all_orgs_task                             │
│  ─────────────────────────────────────────────                              │
│  • Query all APPROVED organizations                                          │
│  • Dispatch Tier 2 task for each organization                               │
│  • Fire-and-forget (returns immediately)                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────────┐
│  TIER 2: Org A        │ │  TIER 2: Org B        │ │  TIER 2: Org C        │
│  (parallel)           │ │  (parallel)           │ │  (parallel)           │
└───────────────────────┘ └───────────────────────┘ └───────────────────────┘
          │                         │                         │
          ▼                         ▼                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TranscriptFetchTaskService.process_all()                                   │
│  ─────────────────────────────────────────                                  │
│  For each OPEN deal with calendar events:                                   │
│    1. Fetch transcripts for deal's meetings (sequential)                    │
│    2. Trigger MeetingSummaryService pipeline                                │
│    3. Move to next deal                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  MeetingSummaryService.process_deal_pipeline()                              │
│  ─────────────────────────────────────────────                              │
│  • Find events with READY transcripts but no summary                        │
│  • Create DealJobs records                                                  │
│  • Chain Celery tasks for sequential execution                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 3: meeting_summary_task (chained, sequential)                         │
│  ─────────────────────────────────────────────────                          │
│  Task 1 → Task 2 → Task 3 (oldest meeting first)                            │
│  Each task generates AI summary using SummaryAgent                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Three-Tier Task Structure

### Tier 1: Coordinator Task

**Task Name**: `deal.trigger_transcript_fetch_all_orgs`

**Purpose**: Entry point triggered by Celery Beat scheduler

**Responsibilities**:
- Query all organizations with `status = APPROVED`
- Dispatch Tier 2 tasks for each organization (parallel)
- Return summary of dispatched tasks

**Schedule**: Every 30 minutes at :15 and :45 past the hour

```python
# config.py
"half-hourly-transcript-fetch-pipeline": {
    "task": "deal.trigger_transcript_fetch_all_orgs",
    "schedule": crontab(minute="15,45"),
    "options": {"queue": "deal", "routing_key": "deal"},
}
```

### Tier 2: Organization Task

**Task Name**: `deal.fetch_meeting_transcripts_org`

**Purpose**: Process all deals for a single organization

**Responsibilities**:
- Acquire distributed lock (prevents duplicate runs)
- Delegate to `TranscriptFetchTaskService.process_all()`
- Handle retries on failure (max 2 retries with exponential backoff)

**Distributed Lock**: Uses Redis lock with key `task_lock:fetch_meeting_transcripts_org:{org_id}` (TTL: 660 seconds)

### Tier 3: Meeting Summary Tasks

**Task Name**: `meeting_summary_task.process`

**Purpose**: Generate AI summary for a single meeting

**Execution Pattern**: Tasks are chained using Celery's `chain()` to run sequentially (oldest meeting first)

```python
# Example chain: task1 | task2 | task3
task_chain = chain(*task_signatures)
task_chain.apply_async()
```

---

## Services

### TranscriptFetchTaskService

**Location**: `src/workers/deal_worker/services/transcript_fetch_task_service.py`

**Purpose**: Orchestrates transcript fetching for all deals in an organization

#### Key Methods

| Method | Description |
|--------|-------------|
| `process_all()` | Main entry point - processes all deals for an org |
| `process_deal(deal_id)` | Process a single deal: fetch transcripts → trigger pipeline |
| `_get_open_deals()` | Get all open deals with calendar event associations |
| `_get_meetings_for_deal()` | Get meetings needing transcript fetch for a deal |
| `_process_single_meeting()` | Fetch transcript for one meeting |

#### Processing Flow

```
process_all()
    │
    ├── Get all open deals with calendar events
    │
    └── For each deal (sequential):
            │
            ├── process_deal(deal_id)
            │       │
            │       ├── Get meetings needing transcript fetch
            │       │
            │       ├── If meetings need fetch:
            │       │       │
            │       │       └── For each meeting (sequential):
            │       │               │
            │       │               └── TranscriptService.fetch_transcript_for_event()
            │       │
            │       └── ALWAYS: MeetingSummaryService.process_deal_pipeline(deal_id)
            │               │
            │               └── Pipeline has its own query to find READY transcripts
            │                   needing summaries (independent of fetch results)
            │
            └── Aggregate results
```

**Important**: The summary pipeline is ALWAYS triggered for every deal, regardless of whether
there were meetings needing transcript fetch. The pipeline has its own query
(`_get_events_needing_summary`) that finds events with READY transcripts and no summary.

### TranscriptService

**Location**: `src/service/integrations/transcripts/service.py`

**Purpose**: Provider-agnostic transcript fetching from Teams/Zoom/etc.

#### Key Methods

| Method | Description |
|--------|-------------|
| `fetch_transcript_for_event()` | Main entry - fetches with retry tracking |
| `find_integrations_for_event()` | Find integrations to try (organizer first, then attendees) |
| `try_fetch_with_integration()` | Try fetching with a specific integration |
| `ensure_transcript()` | Ensure transcript exists (used by summary generation) |

#### Integration Priority

When fetching a transcript, integrations are tried in this order:
1. **Organizer's integration** (event.organiser_email)
2. **Event's linked integration** (event.integration_id)
3. **Attendee integrations** (event.attendees[].email)

### MeetingSummaryService

**Location**: `src/workers/deal_worker/services/deal_insight_services/meeting_summary_service.py`

**Purpose**: Generates AI meeting summaries and orchestrates batch processing

#### Key Methods

| Method | Description |
|--------|-------------|
| `generate()` | Generate summary for a single event (requires deal_id, event_id) |
| `process_deal_pipeline(deal_id)` | Create jobs and chain tasks for a deal's events |
| `_get_events_needing_summary()` | Find events: ended, transcript READY, no summary |
| `_create_job()` | Create DealJobs record for tracking |

#### Pipeline Logic

```
process_deal_pipeline(deal_id)
    │
    ├── Query events where:
    │       • CalendarEventDealAssociation.status = ACTIVE
    │       • CalendarEvent.end_at < now (meeting ended)
    │       • Meeting.status = READY (transcript available)
    │       • meeting_summary_data IS NULL (no summary yet)
    │       • ORDER BY start_at ASC (oldest first)
    │
    ├── For each event:
    │       │
    │       ├── Check for existing active job (PENDING/IN_PROGRESS)
    │       │       └── If exists: skip creation, still add to chain
    │       │
    │       └── Create new DealJobs record:
    │               • job_type = MEETING_SUMMARY
    │               • user_id = AUTOMATED_PIPELINE_USER_ID
    │               • status = PENDING
    │
    └── Build and dispatch Celery chain:
            chain(
                signature("meeting_summary_task.process", [org_id, job_id_1]),
                signature("meeting_summary_task.process", [org_id, job_id_2]),
                signature("meeting_summary_task.process", [org_id, job_id_3]),
            ).apply_async()
```

---

## Data Flow

### Complete Pipeline Flow

```
                                    ┌─────────────────┐
                                    │  Celery Beat    │
                                    │  (every 30 min) │
                                    └────────┬────────┘
                                             │
                                             ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                        TIER 1: All Orgs Coordinator                        │
├────────────────────────────────────────────────────────────────────────────┤
│  SELECT organisation_id FROM organisations WHERE status = 'APPROVED'       │
│                                                                            │
│  For each org: dispatch Tier 2 task                                        │
└────────────────────────────────────────────────────────────────────────────┘
                                             │
                          ┌──────────────────┼──────────────────┐
                          ▼                  ▼                  ▼
                    ┌───────────┐      ┌───────────┐      ┌───────────┐
                    │  Org A    │      │  Org B    │      │  Org C    │
                    │ (parallel)│      │ (parallel)│      │ (parallel)│
                    └─────┬─────┘      └─────┬─────┘      └─────┬─────┘
                          │                  │                  │
                          ▼                  ▼                  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     TIER 2: Per-Organization Processing                    │
├────────────────────────────────────────────────────────────────────────────┤
│  1. Acquire distributed lock (Redis)                                       │
│  2. Get all OPEN deals with calendar event associations                    │
│  3. For each deal:                                                         │
│     ┌────────────────────────────────────────────────────────────────────┐ │
│     │  DEAL PROCESSING (sequential)                                      │ │
│     ├────────────────────────────────────────────────────────────────────┤ │
│     │  a. Query meetings needing transcript fetch:                       │ │
│     │     • end_at < now (meeting ended)                                 │ │
│     │     • online_meeting_id IS NOT NULL                                │ │
│     │     • No Meeting record OR status in (PENDING, FAILED)             │ │
│     │     • transcript_fetch_attempts < MAX_ATTEMPTS (2)                 │ │
│     │                                                                    │ │
│     │  b. For each meeting (sequential):                                 │ │
│     │     ┌────────────────────────────────────────────────────────────┐ │ │
│     │     │  TRANSCRIPT FETCH                                          │ │ │
│     │     ├────────────────────────────────────────────────────────────┤ │ │
│     │     │  • Find integrations (organizer → attendees)               │ │ │
│     │     │  • Try each integration:                                   │ │ │
│     │     │    - Resolve Teams meeting ID                              │ │ │
│     │     │    - Fetch VTT transcript                                  │ │ │
│     │     │    - Parse and save segments                               │ │ │
│     │     │  • Update Meeting.status:                                  │ │ │
│     │     │    - READY (transcript fetched)                            │ │ │
│     │     │    - PENDING (will retry next run)                         │ │ │
│     │     │    - NOT_AVAILABLE (max attempts reached)                  │ │ │
│     │     └────────────────────────────────────────────────────────────┘ │ │
│     │                                                                    │ │
│     │  c. Trigger MeetingSummaryService.process_deal_pipeline()          │ │
│     │     ┌────────────────────────────────────────────────────────────┐ │ │
│     │     │  SUMMARY PIPELINE                                          │ │ │
│     │     ├────────────────────────────────────────────────────────────┤ │ │
│     │     │  • Find events with READY transcripts, no summary          │ │ │
│     │     │  • Create DealJobs records (PENDING status)                │ │ │
│     │     │  • Chain meeting_summary_task.process tasks                │ │ │
│     │     │  • Fire-and-forget (async execution)                       │ │ │
│     │     └────────────────────────────────────────────────────────────┘ │ │
│     └────────────────────────────────────────────────────────────────────┘ │
│  4. Release distributed lock                                               │
└────────────────────────────────────────────────────────────────────────────┘
                                             │
                                             ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                    TIER 3: Meeting Summary Generation                      │
├────────────────────────────────────────────────────────────────────────────┤
│  Celery task chain (sequential execution):                                 │
│                                                                            │
│  Task 1 (oldest meeting)                                                   │
│      │                                                                     │
│      ├── Load deal, event, association                                     │
│      ├── Verify transcript (TranscriptService.ensure_transcript)           │
│      ├── Call SummaryAgent with meeting transcript                         │
│      ├── Save summary to CalendarEventDealAssociation.meeting_summary_data │
│      └── Mark association.meeting_summary_status = COMPLETED               │
│                                                                            │
│      ▼                                                                     │
│  Task 2 (next oldest meeting)                                              │
│      │                                                                     │
│      └── Same process...                                                   │
│                                                                            │
│      ▼                                                                     │
│  Task 3 (next oldest meeting)                                              │
│      │                                                                     │
│      └── Same process...                                                   │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Database Models

### Core Models

| Model | Table | Purpose |
|-------|-------|---------|
| `Organisation` | `public.organisations` | Organization registry |
| `CRMDeal` | `org_*.crm_deals` | Deal records |
| `CalendarEvent` | `org_*.calendar_events` | Calendar events |
| `CalendarEventDealAssociation` | `org_*.calendar_event_deal_associations` | Links events to deals |
| `Meeting` | `org_*.meetings` | Meeting records with transcripts |
| `TranscriptSegment` | `org_*.transcript_segments` | Parsed transcript segments |
| `KnowledgeIntegration` | `org_*.knowledge_integrations` | OAuth integrations |
| `DealJobs` | `org_*.deal_jobs` | Job tracking for async processing |

### Key Status Enums

#### MeetingStatus
```python
class MeetingStatus(str, Enum):
    PENDING = "pending"           # Waiting for transcript (will retry)
    FETCHING = "fetching"         # Currently fetching
    READY = "ready"               # Transcript available
    FAILED = "failed"             # Temporary failure (will retry)
    NOT_AVAILABLE = "not_available"  # Permanent - no transcript possible
```

#### GenerationStatus
```python
class GenerationStatus(str, Enum):
    PENDING = "pending"           # Not started
    GENERATING = "generating"     # AI processing in progress
    COMPLETED = "completed"       # Successfully generated
    FAILED = "failed"             # Generation failed
```

#### JobStatus
```python
class JobStatus(str, Enum):
    PENDING = "pending"           # Queued
    IN_PROGRESS = "in_progress"   # Currently processing
    COMPLETED = "completed"       # Finished successfully
    FAILED = "failed"             # Failed (may retry)
```

### Query Patterns

#### Meetings Needing Transcript Fetch
```sql
SELECT ce.*
FROM calendar_events ce
JOIN calendar_event_deal_associations ceda ON ceda.event_id = ce.event_id
LEFT JOIN meetings m ON m.calendar_event_id = ce.event_id
WHERE ce.organisation_id = :org_id
  AND ce.end_at < NOW()                           -- Meeting has ended
  AND ce.online_meeting_id IS NOT NULL            -- Has online meeting
  AND ceda.status = 'ACTIVE'
  AND (
    m.meeting_id IS NULL                          -- No Meeting record yet
    OR (m.status = 'PENDING' AND m.transcript_fetch_attempts < 2)
    OR (m.status = 'FAILED' AND m.transcript_fetch_attempts < 2)
  )
```

#### Events Needing Summary
```sql
SELECT ce.event_id, ceda.association_id, ceda.deal_id
FROM calendar_events ce
JOIN calendar_event_deal_associations ceda ON ceda.event_id = ce.event_id
JOIN meetings m ON m.calendar_event_id = ce.event_id
WHERE ceda.organisation_id = :org_id
  AND ceda.deal_id = :deal_id
  AND ceda.status = 'ACTIVE'
  AND ce.end_at < NOW()
  AND m.status = 'READY'                          -- Transcript available
  AND ceda.meeting_summary_data IS NULL           -- No summary yet
ORDER BY ce.start_at ASC                          -- Oldest first
```

---

## Configuration

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `TRANSCRIPT_LOOKBACK_DAYS` | 90 | How far back to look for meetings |
| `TRANSCRIPT_MAX_FETCH_ATTEMPTS` | 2 | Max retry attempts per meeting |
| `AUTOMATED_PIPELINE_USER_ID` | `00000000-0000-0000-0000-000000000001` | System user for automated jobs |
| `SCHEMA_VERSION` | `1.0` | Version for generated content payloads |

### Celery Beat Schedule

```python
# src/workers/deal_worker/config.py
"half-hourly-transcript-fetch-pipeline": {
    "task": "deal.trigger_transcript_fetch_all_orgs",
    "schedule": crontab(minute="15,45"),  # :15 and :45 past each hour
    "options": {"queue": "deal", "routing_key": "deal"},
}
```

### Task Configuration

| Task | Queue | Soft Limit | Hard Limit | Max Retries |
|------|-------|------------|------------|-------------|
| `trigger_transcript_fetch_all_orgs` | deal | 300s | 360s | 0 |
| `fetch_meeting_transcripts_org` | deal | 3000s | 3300s | 2 |
| `meeting_summary_task.process` | deal_jobs | 600s | 660s | 3 |

---

## Error Handling & Retry Logic

### Transcript Fetch Retry Strategy

```
Attempt 1 (Run at :15)
    │
    ├── Success → Meeting.status = READY
    │
    └── Failure → Meeting.status = PENDING, attempts = 1
            │
            └── Will retry at :45 (30 min later)

Attempt 2 (Run at :45)
    │
    ├── Success → Meeting.status = READY
    │
    └── Failure → Meeting.status = NOT_AVAILABLE, attempts = 2
            │
            └── Permanent failure (no more retries)
```

### Why Retry?

When the API returns an empty transcript list, we cannot distinguish between:
- **Meeting wasn't recorded** (permanent - no transcript will ever exist)
- **Transcript still processing** (temporary - will be available soon)
- **User lacks permission** (temporary - try another user's integration)

The retry strategy gives a ~1 hour window for transcripts to become available.

### Task-Level Retry

| Task | Retry Strategy |
|------|----------------|
| `fetch_meeting_transcripts_org` | 2 retries, exponential backoff (60s, 120s) |
| `meeting_summary_task.process` | 3 retries, 60s delay; NonRetryableJobError skips retry |

### NonRetryableJobError

Used for permanent failures that shouldn't trigger retry:
- No transcript available (meeting wasn't recorded)
- Invalid meeting data
- User-facing errors that won't resolve with retry

```python
class NonRetryableJobError(Exception):
    def __init__(self, message: str, error_code: str = "permanent_failure"):
        super().__init__(message)
        self.error_code = error_code
```

---

## Logging

### Log Format

All logs include context via structured logging:
- `request_id` - Request correlation ID
- `celery_task` - Task name and ID
- `queue` - Celery queue name

### Key Log Messages

#### Tier 1 (Coordinator)
```
INFO - Tier 1: Starting transcript fetch for all organizations
INFO - Found 2 approved organization(s) for transcript fetch
INFO - Transcript fetch dispatch complete: triggered=2 skipped=0 total=2
```

#### Tier 2 (Organization)
```
INFO - Transcript fetch: Starting for org {org_id}
INFO - Found 15 deals needing transcript fetch for org {org_id}
INFO - [1/15] Processing deal {deal_id} for transcript fetch
INFO -   No meetings need transcript fetch for deal {deal_id} (skipping)
INFO - [1/15] Deal {deal_id} complete: meetings=0, fetched=0, pending=0, not_available=0, failed=0
```

#### Transcript Fetch
```
INFO -   Fetching transcripts for 2 meeting(s) in deal {deal_id}
INFO -   → Fetching transcript for event {event_id} (title: Discovery Call, provider: teams)
INFO -   ← Event {event_id} result: status=fetched, integrations_tried=1, fetched_with=user@example.com, error=None
INFO -   Processed 2/2 meetings
INFO - Transcript fetch for deal {deal_id}: found=2, fetched=1, pending=0, not_available=1, failed=0
```

#### Pipeline
```
INFO -   Triggering meeting summary pipeline for deal {deal_id}
INFO - Processing 3 events for deal {deal_id} (creating jobs, chaining tasks)
INFO - Triggered chain of 3 tasks for deal {deal_id} (created=2, skipped=1)
INFO -   Pipeline result for deal {deal_id}: jobs_created=2, jobs_skipped=1, tasks_triggered=3
```

#### Summary Generation
```
INFO - Preparing summary for meeting {event_id} (deal {deal_id})
INFO - Fetched Teams transcript: 45 segments, 12500 chars
```

---

## Troubleshooting

### Common Issues

| Symptom | Likely Cause | Resolution |
|---------|--------------|------------|
| "No deals need transcript fetch" | No open deals with calendar events | Check CRMDeal.deal_status and associations |
| "No integrations for event" | No active calendar integrations | User needs to reconnect calendar |
| "Could not resolve meeting ID" | Meeting URL not recognized | Check CalendarEvent.conference_link |
| "No transcript found" | Meeting wasn't recorded | Check Teams recording settings |
| "Already running" (skipped) | Previous task still running | Wait for lock to release (660s TTL) |

### Debugging Commands

```bash
# Manually trigger for all orgs
uv run celery -A src.workers.deal_worker.celery_app call deal.trigger_transcript_fetch_all_orgs

# Manually trigger for specific org
uv run celery -A src.workers.deal_worker.celery_app call deal.fetch_meeting_transcripts_org \
  --kwargs='{"organisation_id": "your-org-uuid"}'

# Check active tasks
uv run celery -A src.workers.deal_worker.celery_app inspect active

# Check scheduled tasks
uv run celery -A src.workers.deal_worker.celery_app inspect scheduled
```

---

## File References

| File | Description |
|------|-------------|
| `src/workers/deal_worker/tasks/transcript_fetch_tasks.py` | Celery tasks (Tier 1 & 2) |
| `src/workers/deal_worker/services/transcript_fetch_task_service.py` | Main service orchestrating fetch |
| `src/service/integrations/transcripts/service.py` | TranscriptService (provider-agnostic) |
| `src/service/integrations/transcripts/teams_provider.py` | Teams-specific transcript fetching |
| `src/workers/deal_worker/services/deal_insight_services/meeting_summary_service.py` | Summary generation + pipeline |
| `src/workers/deal_worker/tasks/meeting_insight_tasks/meeting_summary_task.py` | Meeting summary Celery task |
| `src/workers/deal_worker/config.py` | Celery Beat schedule configuration |
