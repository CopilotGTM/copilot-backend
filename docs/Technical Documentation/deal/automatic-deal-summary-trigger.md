# Automatic Deal Summary Trigger

> **Status**: Implemented ✓
> **Author**: Claude
> **Created**: 2025-01-18
> **Updated**: 2025-01-18
> **Implemented Files**:
> - `src/service/crm/deal_meeting_summary_pipeline_service.py` (NEW)
> - `src/service/crm/transcript_fetch_task_service.py` (MODIFIED)
> - `src/workers/deal_worker/tasks/transcript_fetch_tasks.py` (MODIFIED)
> - `src/workers/deal_worker/config.py` (MODIFIED)

---

## Executive Summary

This document outlines the plan to automatically trigger deal summary generation through a sequential pipeline:

1. **Fetch ALL transcripts** for a deal's meetings
2. **Generate meeting summaries ONE BY ONE** (sequential, not parallel)
3. **Generate deal summary** once ALL meeting summaries are complete

Key design decisions:
- Process ALL past meetings (not just recent ones) for complete deal context
- Sequential processing with locks to prevent race conditions
- Event-driven pipeline: transcript → meeting summary → deal summary

---

## Problem Statement

### Current State

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CURRENT FLOW (Manual)                            │
└─────────────────────────────────────────────────────────────────────┘

User Request (API)  →  Create DealJob  →  Celery Task  →  AI Agent
                                                              │
                                                              ▼
                                              Agent fetches transcripts
                                              (may not exist yet!)
```

### Issues

| # | Issue | Impact |
|---|-------|--------|
| 1 | **Manual-only trigger** | Users must explicitly request deal summaries |
| 2 | **Transcript fetch disabled** | `half-hourly-transcript-fetch` commented out in `config.py:112-116` |
| 3 | **No event-driven summary** | Summary doesn't auto-trigger when transcripts complete |
| 4 | **Race condition risk** | Summary may run before transcripts are ready |
| 5 | **No orchestration** | Transcript fetch and deal summary are uncoordinated |

### Current File Locations

| Component | Location | Current Behavior |
|-----------|----------|------------------|
| Transcript Fetch Task | `src/workers/deal_worker/tasks/transcript_fetch_tasks.py` | Fetches transcripts, returns counts only |
| Transcript Fetch Service | `src/service/crm/transcript_fetch_task_service.py` | Processes meetings, no deal-level tracking |
| Deal Summary Task | `src/workers/deal_worker/tasks/deal_summary_tasks.py` | Manual trigger only |
| Deal Summary Service | `src/workers/deal_worker/services/deal_summary_service.py` | Generates summary, requires meetings |
| Worker Config | `src/workers/deal_worker/config.py` | Transcript fetch schedule disabled |

---

## Proposed Solution

### Target Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                 PROPOSED FLOW (Sequential Pipeline)                          │
└─────────────────────────────────────────────────────────────────────────────┘

                              PHASE 1: TRANSCRIPT FETCH
┌─────────────────────────────────────────────────────────────────────────────┐
│ [Celery Beat: Every 30 min]                                                 │
│          │                                                                   │
│          ▼                                                                   │
│ trigger_transcript_fetch_all_orgs                                           │
│          │ (per org)                                                         │
│          ▼                                                                   │
│ fetch_meeting_transcripts_org ──► Fetch ALL transcripts for deal meetings   │
│          │                                                                   │
│          ▼                                                                   │
│ Track affected deal_ids ──► Check: Are ALL transcripts complete for deal?   │
│          │                          (status = READY or NOT_AVAILABLE)       │
│          │ YES                                                               │
│          ▼                                                                   │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ ENQUEUE: process_deal_meeting_summaries_task(deal_id)                   │ │
│ │ (Start sequential meeting summary processing)                            │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘

                         PHASE 2: SEQUENTIAL MEETING SUMMARIES
┌─────────────────────────────────────────────────────────────────────────────┐
│ process_deal_meeting_summaries_task(deal_id)                                │
│          │                                                                   │
│          ▼                                                                   │
│ ┌──────────────────────────────────────────────────────────────────────────┐│
│ │ ACQUIRE LOCK: deal_meeting_summary_lock:{deal_id}                        ││
│ │ (Prevents parallel processing of same deal)                              ││
│ └──────────────────────────────────────────────────────────────────────────┘│
│          │                                                                   │
│          ▼                                                                   │
│ Get meetings ordered by date (oldest first)                                  │
│          │                                                                   │
│          ▼                                                                   │
│ ┌──────────────────────────────────────────────────────────────────────────┐│
│ │ FOR EACH meeting (sequential, one-by-one):                               ││
│ │   │                                                                       ││
│ │   ├─► Check: meeting_summary already exists?                             ││
│ │   │      YES → Skip, move to next                                        ││
│ │   │      NO  ↓                                                           ││
│ │   │                                                                       ││
│ │   ├─► Generate meeting summary (AI call)                                 ││
│ │   │      │                                                               ││
│ │   │      ▼                                                               ││
│ │   ├─► Save meeting_summary_data to CalendarEventDealAssociation          ││
│ │   │      │                                                               ││
│ │   │      ▼                                                               ││
│ │   └─► Log: "Meeting {n}/{total} summarized for deal {deal_id}"           ││
│ │                                                                           ││
│ │   [CONTINUE TO NEXT MEETING]                                             ││
│ └──────────────────────────────────────────────────────────────────────────┘│
│          │                                                                   │
│          ▼                                                                   │
│ ALL meetings summarized?                                                     │
│          │ YES                                                               │
│          ▼                                                                   │
│ ┌──────────────────────────────────────────────────────────────────────────┐│
│ │ ENQUEUE: process_deal_summary_task(deal_id)                              ││
│ │ (Now generate the overall deal summary)                                   ││
│ └──────────────────────────────────────────────────────────────────────────┘│
│          │                                                                   │
│          ▼                                                                   │
│ RELEASE LOCK                                                                 │
└─────────────────────────────────────────────────────────────────────────────┘

                           PHASE 3: DEAL SUMMARY
┌─────────────────────────────────────────────────────────────────────────────┐
│ process_deal_summary_task(deal_id)                                          │
│          │                                                                   │
│          ▼                                                                   │
│ DealSummaryService.generate()                                               │
│   └─► Uses ALL meeting summaries as context                                 │
│   └─► Generates comprehensive deal summary                                  │
│          │                                                                   │
│          ▼                                                                   │
│ Save deal_summary_data to CRMDeal                                           │
│          │                                                                   │
│          ▼                                                                   │
│ COMPLETE ✓                                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Why Sequential Processing?

| Aspect | Parallel | Sequential (Chosen) |
|--------|----------|---------------------|
| **LLM Rate Limits** | Risk of hitting limits | Controlled, predictable |
| **Cost Control** | Spike in API costs | Spread over time |
| **Error Handling** | Complex rollback | Simple: stop at failure |
| **Debugging** | Hard to trace | Clear: "Meeting 3/10 failed" |
| **Resource Usage** | Memory spike | Steady resource usage |
| **Deal Summary Quality** | N/A | Can use previous summaries as context |

---

## Pipeline Readiness Criteria

### Stage 1: Ready for Meeting Summaries

A deal is ready to START the meeting summary pipeline when:

```python
# Criteria for starting meeting summary processing (ALL must be true):

1. deal.has_meetings == True
   # Deal must have at least 1 associated meeting

2. all_transcripts_resolved == True
   # Every deal meeting has transcript status in {READY, NOT_AVAILABLE}
   # (No PENDING, FETCHING, or PROCESSING status)

3. at_least_one_transcript == True
   # At least 1 meeting has status=READY with actual transcript content

4. no_active_meeting_summary_job == True
   # No active lock/job for meeting summary processing on this deal

5. has_unsummarized_meetings == True
   # At least 1 meeting without meeting_summary_data
```

### Stage 2: Ready for Deal Summary

A deal is ready for DEAL summary when:

```python
# Criteria for deal summary (ALL must be true):

1. all_meetings_summarized == True
   # Every meeting with transcript has meeting_summary_data populated

2. no_existing_deal_summary == True
   # Deal does not have deal_summary_data populated
   # OR deal_summary_data.generated_at is older than newest meeting_summary

3. no_active_deal_summary_job == True
   # No DealJobs with job_type=DEAL_SUMMARY and status in {PENDING, IN_PROGRESS}
```

---

## Implementation Details

### Phase 1: Core Infrastructure - Sequential Processing Service

#### 1.1 Create DealMeetingSummaryPipelineService

**New File**: `src/workers/deal_worker/services/deal_meeting_summary_pipeline_service.py`

```python
"""
Service for sequential meeting summary processing.

Key Design:
- Processes ALL meetings for a deal (not just recent ones)
- Processes ONE meeting at a time (sequential, not parallel)
- Uses distributed lock to prevent concurrent processing
- Triggers deal summary only after ALL meeting summaries complete
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class MeetingSummaryProgress:
    """Progress tracking for meeting summary pipeline."""
    deal_id: UUID
    total_meetings: int = 0
    meetings_with_transcript: int = 0
    meetings_summarized: int = 0
    meetings_skipped: int = 0  # No transcript available
    current_meeting_index: int = 0
    is_complete: bool = False
    error: str | None = None


@dataclass
class PipelineReadinessResult:
    """Result of pipeline readiness check."""
    deal_id: UUID
    is_ready: bool
    reason: str | None = None
    total_meetings: int = 0
    meetings_with_transcript: int = 0
    meetings_pending_transcript: int = 0
    meetings_needing_summary: int = 0


class DealMeetingSummaryPipelineService:
    """
    Sequential pipeline for meeting summaries → deal summary.

    Flow:
    1. Check if all transcripts are ready for a deal
    2. Process meeting summaries ONE BY ONE (oldest first)
    3. After all meetings summarized, trigger deal summary
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def check_pipeline_readiness(self, deal_id: UUID) -> PipelineReadinessResult:
        """
        Check if a deal is ready to start the meeting summary pipeline.

        Ready when:
        - All transcripts resolved (READY or NOT_AVAILABLE)
        - At least 1 transcript available
        - At least 1 meeting needs summary
        """
        ...

    async def process_deal_meetings_sequentially(
        self,
        deal_id: UUID,
        on_meeting_complete: callable | None = None,  # Callback for progress
    ) -> MeetingSummaryProgress:
        """
        Process all meetings for a deal, ONE BY ONE.

        Args:
            deal_id: The deal to process
            on_meeting_complete: Optional callback(meeting_index, total, meeting_id)

        Returns:
            Progress result with counts and any errors
        """
        ...

    async def _generate_single_meeting_summary(
        self,
        association: CalendarEventDealAssociation,
        meeting: Meeting,
    ) -> dict[str, Any]:
        """Generate summary for a single meeting."""
        ...

    async def _get_meetings_for_deal(
        self,
        deal_id: UUID,
    ) -> list[tuple[CalendarEventDealAssociation, Meeting | None]]:
        """
        Get all meetings for a deal, ordered by date (oldest first).

        Returns list of (association, meeting) tuples.
        """
        ...
```

#### 1.2 Create Distributed Lock Service

**New File**: `src/workers/deal_worker/services/deal_pipeline_lock.py`

```python
"""
Distributed lock for deal pipeline processing.

Uses Redis to ensure only ONE process handles a deal's meeting summaries at a time.
"""

from contextlib import asynccontextmanager
from uuid import UUID

import redis.asyncio as redis

from src.infra.config.configs import redis_config


class DealPipelineLock:
    """
    Distributed lock for deal summary pipeline.

    Prevents:
    - Multiple workers processing same deal simultaneously
    - Race conditions when transcript fetch triggers pipeline
    """

    LOCK_PREFIX = "deal_pipeline_lock:"
    DEFAULT_TTL = 3600  # 1 hour max lock time (safety)

    def __init__(self):
        self.redis_client: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if not self.redis_client:
            self.redis_client = redis.from_url(redis_config.url)
        return self.redis_client

    @asynccontextmanager
    async def acquire_deal_lock(
        self,
        deal_id: UUID,
        ttl: int = DEFAULT_TTL,
    ):
        """
        Acquire lock for processing a deal's meeting summaries.

        Usage:
            async with lock.acquire_deal_lock(deal_id) as acquired:
                if acquired:
                    # Process meetings
                else:
                    # Another process is handling this deal
        """
        redis_client = await self._get_redis()
        lock_key = f"{self.LOCK_PREFIX}{deal_id}"

        # Try to acquire lock
        acquired = await redis_client.set(
            lock_key,
            "locked",
            nx=True,  # Only set if not exists
            ex=ttl,
        )

        try:
            yield bool(acquired)
        finally:
            if acquired:
                await redis_client.delete(lock_key)

    async def is_deal_locked(self, deal_id: UUID) -> bool:
        """Check if a deal is currently being processed."""
        redis_client = await self._get_redis()
        lock_key = f"{self.LOCK_PREFIX}{deal_id}"
        return await redis_client.exists(lock_key) > 0

    async def extend_lock(self, deal_id: UUID, ttl: int = DEFAULT_TTL) -> bool:
        """Extend lock TTL for long-running operations."""
        redis_client = await self._get_redis()
        lock_key = f"{self.LOCK_PREFIX}{deal_id}"
        return await redis_client.expire(lock_key, ttl)
```

#### 1.3 Create DealSummaryReadinessService

**New File**: `src/workers/deal_worker/services/deal_summary_readiness_service.py`

```python
"""Service for checking if deals are ready for final deal summary."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class DealSummaryReadinessResult:
    """Result of deal summary readiness check."""
    deal_id: UUID
    is_ready: bool
    reason: str | None = None
    total_meetings: int = 0
    meetings_summarized: int = 0
    meetings_pending_summary: int = 0


class DealSummaryReadinessService:
    """Check if deals are ready for final deal summary generation."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def check_deal_summary_readiness(self, deal_id: UUID) -> DealSummaryReadinessResult:
        """
        Check if a deal is ready for deal summary (after all meeting summaries done).

        Ready when:
        - All meetings with transcripts have meeting_summary_data
        - No existing deal_summary_data (or outdated)
        - No active deal summary job
        """
        ...

    async def get_deals_ready_for_deal_summary(self) -> list[UUID]:
        """Get all deals ready for deal summary."""
        ...
```

**SQL Query for Pipeline Readiness Check**:

```sql
-- Find deals ready to start meeting summary pipeline
WITH deal_meeting_status AS (
    SELECT
        ceda.deal_id,
        ceda.association_id,
        ce.event_id,
        ce.start_at,
        m.status as transcript_status,
        m.full_transcript IS NOT NULL as has_transcript,
        ceda.meeting_summary_data IS NOT NULL as has_meeting_summary
    FROM calendar_event_deal_associations ceda
    JOIN calendar_events ce ON ce.event_id = ceda.event_id
    LEFT JOIN meetings m ON m.calendar_event_id = ce.event_id
    WHERE ceda.organisation_id = :org_id
      AND ceda.status = 'active'
      AND ce.end_at < NOW()  -- Meeting has ended
),
deal_summary AS (
    SELECT
        deal_id,
        COUNT(*) as total_meetings,
        COUNT(CASE WHEN transcript_status = 'ready' THEN 1 END) as transcripts_ready,
        COUNT(CASE WHEN transcript_status IN ('pending', 'fetching', 'processing') THEN 1 END) as transcripts_pending,
        COUNT(CASE WHEN has_transcript THEN 1 END) as with_transcript,
        COUNT(CASE WHEN has_transcript AND NOT has_meeting_summary THEN 1 END) as needs_meeting_summary
    FROM deal_meeting_status
    GROUP BY deal_id
)
SELECT
    ds.deal_id,
    ds.total_meetings,
    ds.transcripts_ready,
    ds.transcripts_pending,
    ds.with_transcript,
    ds.needs_meeting_summary
FROM deal_summary ds
WHERE ds.transcripts_pending = 0     -- All transcripts resolved
  AND ds.with_transcript > 0         -- At least 1 transcript
  AND ds.needs_meeting_summary > 0   -- At least 1 meeting needs summary
```

### Phase 2: Celery Tasks for Sequential Pipeline

#### 2.1 Create Meeting Summary Pipeline Task

**New File**: `src/workers/deal_worker/tasks/meeting_summary_pipeline_tasks.py`

```python
"""
Celery tasks for sequential meeting summary processing.

Pipeline Flow:
1. process_deal_meeting_summaries_task - Process ALL meetings for a deal (sequential)
2. On completion → automatically triggers process_deal_summary_task
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task

from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_meeting_summary_pipeline_service import (
    DealMeetingSummaryPipelineService,
)
from src.workers.deal_worker.services.deal_pipeline_lock import DealPipelineLock

logger = get_logger(__name__)


@app.task(
    name="deal.process_deal_meeting_summaries",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
    acks_late=True,
    soft_time_limit=3600,   # 1 hour (many meetings)
    time_limit=3900,
)
def process_deal_meeting_summaries_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
) -> dict[str, Any]:
    """
    Process ALL meeting summaries for a deal, ONE BY ONE.

    This task:
    1. Acquires a distributed lock for the deal
    2. Gets all meetings ordered by date (oldest first)
    3. Generates summary for each meeting sequentially
    4. On completion, triggers the deal summary task

    Triggered by: transcript_fetch_tasks when all transcripts are ready
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    d_id = UUID(deal_id)

    async def _process() -> dict[str, Any]:
        lock = DealPipelineLock()

        async with lock.acquire_deal_lock(d_id) as acquired:
            if not acquired:
                logger.info(
                    "Deal %s is already being processed by another worker, skipping",
                    deal_id,
                )
                return {
                    "status": "skipped",
                    "reason": "lock_held_by_another_process",
                    "deal_id": deal_id,
                    "started_at": started_at,
                }

            logger.info("Starting sequential meeting summary pipeline for deal %s", deal_id)

            async for session in acquire_org_async_session(org_id):
                service = DealMeetingSummaryPipelineService(session, org_id)

                # Process all meetings one by one
                progress = await service.process_deal_meetings_sequentially(
                    deal_id=d_id,
                    on_meeting_complete=lambda idx, total, mid: logger.info(
                        "Meeting %d/%d summarized for deal %s (meeting_id=%s)",
                        idx + 1, total, deal_id, mid
                    ),
                )

                await session.commit()

            # If all meetings summarized, trigger deal summary
            if progress.is_complete and progress.meetings_summarized > 0:
                from src.workers.deal_worker.tasks.deal_summary_tasks import (
                    process_deal_summary_job,
                )

                logger.info(
                    "All %d meetings summarized for deal %s, triggering deal summary",
                    progress.meetings_summarized,
                    deal_id,
                )

                # Trigger deal summary task
                process_deal_summary_job.apply_async(
                    kwargs={
                        "organisation_id": organisation_id,
                        "deal_id": deal_id,
                        "auto_triggered": True,
                    },
                )

            return {
                "status": "completed" if progress.is_complete else "partial",
                "deal_id": deal_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "total_meetings": progress.total_meetings,
                "meetings_with_transcript": progress.meetings_with_transcript,
                "meetings_summarized": progress.meetings_summarized,
                "meetings_skipped": progress.meetings_skipped,
                "error": progress.error,
                "deal_summary_triggered": progress.is_complete and progress.meetings_summarized > 0,
            }

    try:
        return run_async(_process())
    except Exception as e:
        logger.error(
            "Meeting summary pipeline failed: deal=%s error=%s",
            deal_id, str(e), exc_info=True
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e) from e
        return {
            "status": "failed",
            "deal_id": deal_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
        }
```

#### 2.2 Modify Transcript Fetch Task to Trigger Pipeline

**File**: `src/workers/deal_worker/tasks/transcript_fetch_tasks.py`

**Changes**: After fetching transcripts, check if any deals are ready for the meeting summary pipeline:

```python
@app.task(...)
def fetch_meeting_transcripts_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Fetch transcripts for deal-associated meetings in an organisation."""

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)

        # Existing: Fetch transcripts
        service = TranscriptFetchTaskService(org_uuid)
        result = await service.process_all()

        # NEW: Check and trigger meeting summary pipeline for ready deals
        triggered_pipelines = []
        if result.transcripts_fetched > 0 and result.affected_deal_ids:
            triggered_pipelines = await _trigger_meeting_summary_pipelines(
                org_uuid, result.affected_deal_ids
            )

        return {
            "status": "success",
            ...
            "affected_deal_ids": [str(d) for d in result.affected_deal_ids],
            "meeting_summary_pipelines_triggered": triggered_pipelines,
        }

    ...


async def _trigger_meeting_summary_pipelines(
    organisation_id: UUID,
    affected_deal_ids: set[UUID],
) -> list[str]:
    """
    Check affected deals and trigger meeting summary pipeline if all transcripts ready.

    For each deal:
    1. Check if ALL transcripts are resolved (READY or NOT_AVAILABLE)
    2. Check if at least 1 meeting needs summary
    3. If yes, enqueue process_deal_meeting_summaries_task
    """
    from src.workers.deal_worker.services.deal_meeting_summary_pipeline_service import (
        DealMeetingSummaryPipelineService,
    )
    from src.workers.deal_worker.tasks.meeting_summary_pipeline_tasks import (
        process_deal_meeting_summaries_task,
    )

    triggered = []

    async for session in acquire_org_async_session(organisation_id):
        pipeline_service = DealMeetingSummaryPipelineService(session, organisation_id)

        for deal_id in affected_deal_ids:
            readiness = await pipeline_service.check_pipeline_readiness(deal_id)

            if readiness.is_ready:
                # Enqueue sequential meeting summary task
                process_deal_meeting_summaries_task.apply_async(
                    kwargs={
                        "organisation_id": str(organisation_id),
                        "deal_id": str(deal_id),
                    },
                )
                triggered.append(str(deal_id))
                logger.info(
                    "Triggered meeting summary pipeline for deal %s "
                    "(meetings_needing_summary=%d)",
                    deal_id,
                    readiness.meetings_needing_summary,
                )

    return triggered
```

#### 2.3 Modify TranscriptFetchTaskService

**File**: `src/service/crm/transcript_fetch_task_service.py`

**Changes**: Track affected deal IDs during processing:

```python
@dataclass
class TranscriptFetchResult:
    """Result of transcript fetch for the organisation."""
    meetings_found: int = 0
    transcripts_fetched: int = 0
    transcripts_pending: int = 0
    transcripts_not_available: int = 0
    transcripts_failed: int = 0
    affected_deal_ids: set[UUID] = field(default_factory=set)  # NEW


class TranscriptFetchTaskService:
    ...

    async def _get_deal_ids_for_event(
        self, session: AsyncSession, event_id: UUID
    ) -> list[UUID]:
        """Get all deal IDs associated with an event."""
        stmt = (
            select(CalendarEventDealAssociation.deal_id)
            .where(
                col(CalendarEventDealAssociation.event_id) == event_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
            )
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]
```

### Phase 3: Configuration Updates

#### 3.1 Re-enable Transcript Fetch Schedule

**File**: `src/workers/deal_worker/config.py`

```python
self.beat_schedule = {
    ...
    # RE-ENABLE: Half-hourly transcript fetch
    "half-hourly-transcript-fetch": {
        "task": "deal.trigger_transcript_fetch_all_orgs",
        "schedule": crontab(minute="15,45"),  # Every 30 minutes
        "options": {"queue": "deal", "routing_key": "deal"},
    },
}
```

#### 3.2 Add System User for Auto-Triggered Jobs

For automatically triggered jobs, we need a system user ID. Options:

1. **Use first admin user of org** - Query for admin user
2. **Use dedicated system user UUID** - Hardcoded constant
3. **Make user_id nullable** - Modify DealJobs model

**Recommended**: Option 1 - Use first admin user:

```python
async def get_system_user_for_org(session: AsyncSession, org_id: UUID) -> UUID:
    """Get a system user ID for auto-triggered jobs."""
    # Get first admin user in org, or fall back to any user
    stmt = (
        select(User.user_id)
        .where(col(User.organisation_id) == org_id)
        .order_by(col(User.is_admin).desc(), col(User.created_at).asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row:
        return row[0]
    raise ValueError(f"No users found in organisation {org_id}")
```

### Phase 4: Documentation Updates

#### 4.1 Update deal-summary-tasks.md

Add new section for automatic triggering:

```markdown
## Automatic Pipeline Trigger

Deal summaries are automatically triggered through a sequential pipeline:

### Pipeline Stages

```
Stage 1: Transcript Fetch (Celery Beat every 30 min)
    ↓
Stage 2: Meeting Summary Pipeline (sequential, one-by-one)
    ↓
Stage 3: Deal Summary Generation
```

### Detailed Flow

```
[Celery Beat: Every 30 min]
    ↓
fetch_meeting_transcripts_org
    ↓
Check: Are ALL transcripts resolved for any deal?
    ↓ YES
Enqueue: process_deal_meeting_summaries_task(deal_id)
    ↓
[SEQUENTIAL LOOP]
For meeting 1/N:
    └─ Generate meeting summary
    └─ Save to CalendarEventDealAssociation.meeting_summary_data
    └─ Log: "Meeting 1/N summarized"
For meeting 2/N:
    └─ ... (repeat)
... continue until N/N
    ↓
ALL DONE
    ↓
Enqueue: process_deal_summary_task(deal_id)
    ↓
Generate deal summary using ALL meeting summaries
```

### Configuration

Requires `half-hourly-transcript-fetch` schedule enabled in `config.py`.
```

#### 4.2 Update task-orchestration-map.md

Add new chain:

```markdown
### Chain 6: Transcript → Meeting Summaries → Deal Summary (Sequential Pipeline)

```
[Celery Beat: Every 30 min]
    ↓
deal.trigger_transcript_fetch_all_orgs
    ↓
deal.fetch_meeting_transcripts_org (per org)
    ↓
TranscriptFetchTaskService.process_all()
    ↓
├── Fetch transcripts for all deal meetings
├── Track affected_deal_ids
└── For each deal where ALL transcripts ready:
    └── DealMeetingSummaryPipelineService.check_pipeline_readiness()
        ↓
        If ready: enqueue process_deal_meeting_summaries_task
            ↓
            [SEQUENTIAL - WITH LOCK]
            ├── Acquire deal_pipeline_lock:{deal_id}
            ├── For each meeting (oldest first):
            │   ├── Generate meeting_summary_data (AI call)
            │   ├── Save to CalendarEventDealAssociation
            │   └── Log progress: "Meeting {n}/{total} done"
            ├── Release lock
            └── If all done: enqueue process_deal_summary_task
                ↓
                DealSummaryService.generate()
                    └── Uses ALL meeting_summary_data as context
```
```

---

## File Changes Summary (Implemented)

| File | Change Type | Description | Status |
|------|-------------|-------------|--------|
| `src/service/crm/deal_meeting_summary_pipeline_service.py` | **NEW** | Sequential meeting summary processing + deal summary trigger | ✓ Done |
| `src/service/crm/transcript_fetch_task_service.py` | **MODIFY** | Track affected deal_ids from fetched transcripts | ✓ Done |
| `src/workers/deal_worker/tasks/transcript_fetch_tasks.py` | **MODIFY** | Trigger meeting summary pipeline after transcript fetch | ✓ Done |
| `src/workers/deal_worker/config.py` | **MODIFY** | Enable transcript fetch schedule (every 30 min at :15/:45) | ✓ Done |

### Simplified Implementation

The implementation was simplified from the original plan:
- No separate lock service needed - sequential processing within a single Celery task handles this
- No separate readiness service needed - pipeline service handles readiness checks inline
- No separate pipeline task needed - integrated directly into transcript fetch task
- Uses existing `SummaryAgent` and `DealSummaryService` without modification

---

## Testing Plan

### Unit Tests

1. **DealMeetingSummaryPipelineService**
   - Test deal with no meetings → not ready
   - Test deal with pending transcripts → not ready
   - Test deal with all transcripts resolved → ready for pipeline
   - Test sequential processing order (oldest first)
   - Test skipping already-summarized meetings
   - Test progress tracking (n/total)

2. **DealPipelineLock**
   - Test lock acquisition succeeds when not held
   - Test lock acquisition fails when held by another process
   - Test lock auto-releases after TTL
   - Test lock extension for long operations
   - Test concurrent lock attempts

3. **DealSummaryReadinessService**
   - Test deal with pending meeting summaries → not ready
   - Test deal with all meeting summaries complete → ready
   - Test deal with existing deal summary → not ready

4. **TranscriptFetchTaskService**
   - Test affected_deal_ids populated correctly
   - Test multiple deals per meeting handled

### Integration Tests

1. **Full Pipeline Test (Single Deal)**
   - Create deal with 3 meetings
   - Mock transcript fetch (all READY)
   - Verify pipeline triggered
   - Verify meetings summarized sequentially (check logs: 1/3, 2/3, 3/3)
   - Verify deal summary triggered after all complete

2. **Concurrent Pipeline Test**
   - Create 2 deals, each with 2 meetings
   - Trigger pipeline for both simultaneously
   - Verify each deal processed independently (separate locks)
   - Verify no cross-contamination

3. **Failure Recovery Test**
   - Start pipeline with 5 meetings
   - Fail on meeting 3 (mock LLM error)
   - Verify meetings 1-2 are saved
   - Retry task
   - Verify resumes from meeting 3 (skips 1-2)

4. **Lock Contention Test**
   - Trigger same deal pipeline twice simultaneously
   - Verify second attempt skipped (lock held)

---

## Rollout Plan

### Phase 1: Deploy Code (No Auto-Trigger)

1. Deploy new services: pipeline service, lock service, readiness service
2. Deploy new task: `process_deal_meeting_summaries_task`
3. Keep transcript fetch schedule disabled
4. Verify no regressions in manual summary generation

### Phase 2: Enable Transcript Fetch Only

1. Enable `half-hourly-transcript-fetch` schedule
2. Monitor transcript fetch success rates
3. Verify affected_deal_ids tracking works
4. **DO NOT enable pipeline trigger yet**

### Phase 3: Enable Meeting Summary Pipeline

1. Enable pipeline triggering in `transcript_fetch_tasks.py`
2. Monitor with small subset (e.g., 1 org)
3. Watch for:
   - Lock acquisition success rate
   - Meeting summary generation time
   - Sequential processing logs

### Phase 4: Full Rollout

1. Enable for all orgs
2. Monitor deal summary generation end-to-end

### Monitoring

| Metric | Alert Threshold |
|--------|-----------------|
| Transcript fetch success rate | < 90% |
| Meeting summary pipeline start rate | Anomaly detection |
| Meeting summary per-meeting time | > 2 min average |
| Lock acquisition failure rate | > 5% |
| Deal summary trigger rate | Anomaly detection |
| Pipeline completion rate | < 80% |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Long-running pipeline blocks deal | User can't manually trigger | Lock TTL = 1 hour, auto-release |
| LLM rate limits hit during sequential processing | Pipeline stalls | Retry with exponential backoff, extend lock |
| Redis unavailable (lock service) | Pipeline can't start | Fallback: skip lock, accept potential duplicates |
| Too many meetings (>20) | Very long pipeline | Add configurable max meetings limit |
| Duplicate pipelines triggered | Wasted LLM calls | Distributed lock prevents this |
| Meeting summary fails mid-pipeline | Partial state | Already-summarized meetings are idempotent (skip) |

---

## Future Enhancements

1. **Progress API**: Expose pipeline progress via API for UI status indicator
2. **Priority queue**: High-value deals processed first
3. **Parallel with limit**: Process N meetings in parallel (e.g., 2) instead of strictly sequential
4. **Regeneration**: Re-run pipeline when new transcripts arrive for existing summaries
5. **User notification**: Notify deal owner when pipeline completes

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-01-18 | Process ALL meetings (not just recent) | Complete deal context is more valuable than speed |
| 2025-01-18 | Sequential processing (one-by-one) | Predictable LLM costs, easier debugging, no rate limit risk |
| 2025-01-18 | Use Redis distributed lock | Prevents duplicate processing, handles multi-worker scenario |
| 2025-01-18 | Oldest-first order | Chronological context builds naturally |
| 2025-01-18 | Trigger deal summary only after ALL meeting summaries | Ensures comprehensive deal summary |
| 2025-01-18 | Use first admin user for auto-jobs | Simpler than model changes, maintains audit trail |

---

## References

- [Deal Summary Tasks](./deal-summary-tasks.md)
- [Task Orchestration Map](./task-orchestration-map.md)
- [Stakeholder Analysis Tasks](./stakeholder-analysis-tasks.md)
- [Celery Worker Creation Guide](../celery-worker-creation-guide.md)
