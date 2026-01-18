# Transcript Fetch Tasks Guide

> **Purpose**: This document explains how meeting transcripts are fetched for deal-associated calendar events. It covers the single-query approach, integration priority, parallel processing, and retry mechanism.

## Overview

Meeting transcripts are automatically fetched for calendar events linked to deals. The system uses a simple approach: single query to get all meetings needing fetch, then process in parallel.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    TRANSCRIPT FETCH OVERVIEW                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌────────────────┐              ┌────────────────┐                        │
│   │ Calendar Event │              │    Meeting     │                        │
│   │                │              │                │                        │
│   │ - online_id    │─────────────▶│ - status       │                        │
│   │ - organizer    │   FETCH      │ - transcript   │                        │
│   │ - attendees    │              │ - attempts     │                        │
│   └────────────────┘              └────────────────┘                        │
│           │                                                                  │
│           │ via CalendarEventDealAssociation                                │
│           ▼                                                                  │
│   ┌────────────────┐                                                        │
│   │    CRM Deal    │                                                        │
│   └────────────────┘                                                        │
│                                                                              │
│   Single query fetches ALL meetings needing transcripts:                     │
│   - Associated with any deal (active association)                           │
│   - Meeting has ended                                                        │
│   - Has online meeting info                                                  │
│   - No READY transcript yet OR retryable status                             │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Architecture

### Two-Tier Task Structure

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TASK HIERARCHY                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   TIER 1: All Organizations (Coordinator)                                    │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ trigger_transcript_fetch_all_orgs_task()                            │   │
│   │                                                                     │   │
│   │ - Triggered: Every 30 minutes (Beat schedule at :15 and :45)       │   │
│   │ - Time Limit: 5 minutes                                             │   │
│   │ - Purpose: Query approved orgs, dispatch Tier 2                     │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      │ For each org                          │
│                                      ▼                                       │
│   TIER 2: Organization Level                                                 │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ fetch_meeting_transcripts_org_task(organisation_id)                 │   │
│   │                                                                     │   │
│   │ - Triggered: Tier 1 dispatch                                        │   │
│   │ - Time Limit: 10 minutes                                            │   │
│   │ - Retries: 2 (60s exponential backoff)                              │   │
│   │ - Lock: Redis lock per org (prevents concurrent execution)         │   │
│   │ - Delegates to: TranscriptFetchTaskService.process_all()           │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Processing Flow (Single Query Approach)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PROCESSING FLOW                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Step 1: Single Query to Get All Meetings                                   │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ get_meetings_needing_fetch()                                        │   │
│   │                                                                     │   │
│   │ One query returns ALL meetings for the org that need fetch:         │   │
│   │ - Associated with a deal (active association)                       │   │
│   │ - Meeting has ended (end_at < now)                                  │   │
│   │ - Has online meeting info (Teams/Zoom/Meet)                         │   │
│   │ - Either:                                                           │   │
│   │   a. No Meeting record exists yet, OR                               │   │
│   │   b. PENDING/FAILED status with attempts < 2                        │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      ▼                                       │
│   Step 2: Process in Parallel (max 3 concurrent)                             │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ process_meetings_parallel(events, max_concurrent=3)                 │   │
│   │                                                                     │   │
│   │ Uses asyncio.Semaphore to limit concurrent API calls                │   │
│   │ Prevents overloading external APIs (Teams, Zoom, etc.)              │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      ▼                                       │
│   Step 3: Return Results                                                     │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ TranscriptFetchResult                                               │   │
│   │                                                                     │   │
│   │ - meetings_found: Total meetings that needed fetch                  │   │
│   │ - transcripts_fetched: Successfully fetched                         │   │
│   │ - transcripts_pending: Will retry next run                          │   │
│   │ - transcripts_not_available: Permanently failed                     │   │
│   │ - transcripts_failed: Error during fetch                            │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Integration Priority for Transcript Fetch

The system tries multiple integrations in priority order to maximize the chance of successfully fetching a transcript.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    INTEGRATION PRIORITY ORDER                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Priority 1: Meeting Organizer's Integration                                │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ find_integration_by_email(event.organiser_email)                    │   │
│   │                                                                     │   │
│   │ The organizer typically has the best access to meeting recordings   │   │
│   │ and transcripts. This is the most likely to succeed.                │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      │ If failed, try next                   │
│                                      ▼                                       │
│   Priority 2: Event's Default Integration                                    │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ get_integration(event.integration_id)                               │   │
│   │                                                                     │   │
│   │ The integration that synced the calendar event. May have access     │   │
│   │ if it's the same user who organized the meeting.                    │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      │ If failed, try next                   │
│                                      ▼                                       │
│   Priority 3: Attendees' Integrations                                        │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ For each attendee in event.attendees:                               │   │
│   │     find_integration_by_email(attendee.email)                       │   │
│   │                                                                     │   │
│   │ Try each attendee's integration until one succeeds.                 │   │
│   │ Some attendees may have access depending on org permissions.        │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      │ If all failed                         │
│                                      ▼                                       │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ Mark as NOT_AVAILABLE (if max attempts reached)                     │   │
│   │ or PENDING (for retry on next run)                                  │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Retry Logic

The system implements a retry mechanism to handle cases where transcripts are not immediately available (e.g., meeting ran longer than scheduled, provider processing delay).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         RETRY FLOW                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Meeting Ends → Task Runs (within 30 min)                                   │
│                                                                              │
│   Attempt 1:                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ Try all integrations in priority order                              │   │
│   │                                                                     │   │
│   │ ┌───────────────┐    ┌───────────────┐    ┌───────────────┐        │   │
│   │ │ SUCCESS       │    │ NOT AVAILABLE │    │ ERROR         │        │   │
│   │ │               │    │               │    │               │        │   │
│   │ │ status=READY  │    │ attempts=1    │    │ status=FAILED │        │   │
│   │ │ Done!         │    │ status=PENDING│    │ Will retry    │        │   │
│   │ └───────────────┘    └───────────────┘    └───────────────┘        │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                      │                                       │
│                                      │ Next task run (30 min later)          │
│                                      ▼                                       │
│   Attempt 2:                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ Try all integrations again                                          │   │
│   │                                                                     │   │
│   │ ┌───────────────┐    ┌───────────────┐    ┌───────────────┐        │   │
│   │ │ SUCCESS       │    │ NOT AVAILABLE │    │ ERROR         │        │   │
│   │ │               │    │               │    │               │        │   │
│   │ │ status=READY  │    │ attempts=2    │    │ attempts=2    │        │   │
│   │ │ Done!         │    │ status=       │    │ status=       │        │   │
│   │ │               │    │ NOT_AVAILABLE │    │ NOT_AVAILABLE │        │   │
│   │ │               │    │ (permanent)   │    │ (permanent)   │        │   │
│   │ └───────────────┘    └───────────────┘    └───────────────┘        │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   After 2 attempts: Meeting marked as NOT_AVAILABLE (no more retries)        │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Meeting Status Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MEETING STATUS TRANSITIONS                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   (No Meeting Record)                                                        │
│          │                                                                   │
│          │ First fetch attempt                                               │
│          ▼                                                                   │
│   ┌────────────┐                                                            │
│   │  FETCHING  │ ◀──────────────────────────────────────────────┐           │
│   └─────┬──────┘                                                │           │
│         │                                                       │           │
│         ├────────────────┬────────────────┬────────────────────┤           │
│         │                │                │                    │           │
│         ▼                ▼                ▼                    │           │
│   ┌──────────┐    ┌──────────┐    ┌─────────────────┐         │           │
│   │  READY   │    │  FAILED  │    │  NOT_AVAILABLE  │         │           │
│   │          │    │          │    │                 │         │           │
│   │ Success! │    │ Error    │    │ No transcript   │         │           │
│   │ Has      │    │ Will     │    │                 │         │           │
│   │ transcript│   │ retry    │    │ attempts < 2?   │         │           │
│   └──────────┘    └────┬─────┘    └───────┬─────────┘         │           │
│                        │                  │                    │           │
│                        │                  │ YES                │           │
│                        │                  ▼                    │           │
│                        │          ┌──────────┐                 │           │
│                        └─────────▶│ PENDING  │─────────────────┘           │
│                         Next run  └──────────┘  Next run                   │
│                                                                              │
│   Terminal States: READY (success), NOT_AVAILABLE (permanent failure)        │
│   Retryable States: PENDING, FAILED (with attempts < 2)                      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Configuration

| Constant | Value | Description |
|----------|-------|-------------|
| `TRANSCRIPT_MAX_FETCH_ATTEMPTS` | 2 | Attempts before marking NOT_AVAILABLE |
| `TRANSCRIPT_PARALLEL_FETCH_LIMIT` | 3 | Max concurrent fetches per org |

**Note:** The query automatically excludes meetings with READY transcripts or permanently failed (NOT_AVAILABLE with max attempts), so every run only processes meetings that actually need work.

## Key Files

| File | Purpose |
|------|---------|
| `src/workers/deal_worker/tasks/transcript_fetch_tasks.py` | Celery tasks (orchestration only) |
| `src/service/crm/transcript_fetch_task_service.py` | Business logic service |
| `src/service/integrations/transcript_service.py` | Transcript fetching with integration priority |
| `src/domain/models/organisation_tables/sync/meeting_intelligence.py` | Meeting model with status and attempts tracking |
| `src/workers/deal_worker/config.py` | Beat schedule configuration |

## Database Schema

### Meeting Table Fields

```sql
-- Key fields for transcript fetch tracking
meeting_id          UUID PRIMARY KEY
calendar_event_id   UUID REFERENCES calendar_events(event_id)
status              VARCHAR(32)  -- PENDING, FETCHING, READY, FAILED, NOT_AVAILABLE
full_transcript     TEXT         -- The actual transcript content
transcript_fetch_attempts INTEGER DEFAULT 0  -- Retry counter
status_message      TEXT         -- Error message or status details
transcript_fetched_at TIMESTAMP  -- When transcript was successfully fetched
source_data         JSONB        -- Includes fetched_with_integration info
```

## Query Logic

### Get Meetings Needing Fetch

Single query that finds all meetings needing transcript fetching:

```python
# Filters applied:
# 1. Associated with a deal (via CalendarEventDealAssociation with ACTIVE status)
# 2. Meeting has ended (end_at < now)
# 3. Has online meeting info (online_meeting_id IS NOT NULL)
# 4. Either:
#    a. No Meeting record exists yet, OR
#    b. Has PENDING/FAILED status with attempts < MAX_ATTEMPTS (2)
#
# Automatically excludes:
# - Meetings with READY status (already have transcript)
# - Meetings with NOT_AVAILABLE status and attempts >= MAX_ATTEMPTS
#
# Uses DISTINCT to handle meetings associated with multiple deals
```

## Task Output

The task returns statistics:

```json
{
  "status": "success",
  "organisation_id": "...",
  "started_at": "2024-01-17T10:15:00Z",
  "completed_at": "2024-01-17T10:17:30Z",
  "meetings_found": 42,
  "transcripts_fetched": 28,
  "transcripts_pending": 6,
  "transcripts_not_available": 6,
  "transcripts_failed": 2
}
```

## Troubleshooting

### Common Issues

1. **No integrations found for meeting**
   - Cause: Organizer and attendees don't have connected calendar integrations
   - Solution: Ensure at least one meeting participant has connected their calendar

2. **Transcript not available after 2 attempts**
   - Cause: Meeting wasn't recorded, or recording was deleted
   - Status: `NOT_AVAILABLE` with message explaining the situation

3. **All integrations failed with permission errors**
   - Cause: Users don't have permission to access the transcript
   - Solution: Meeting organizer should connect their integration

4. **Task timeout**
   - Cause: Too many meetings to process in time limit
   - Solution: Increase `time_limit` or reduce parallel fetch limit
