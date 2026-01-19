# Deal Worker Connection Management Plan

**Purpose**: Analysis and recommendations for managing database connections in long-running Celery tasks, specifically for deal worker mapper tasks.

**Date**: January 2025

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current State Analysis](#2-current-state-analysis)
3. [Key Findings from Research](#3-key-findings-from-research)
4. [Identified Risks](#4-identified-risks)
5. [Recommendations](#5-recommendations)
6. [Implementation Plan](#6-implementation-plan)
7. [Monitoring & Validation](#7-monitoring--validation)

---

## 1. Executive Summary

### Question: Is it okay to hold a session for 15-20 minutes?

**Short Answer**: No, holding a single database session open for 15-20 minutes is NOT recommended and violates SQLAlchemy best practices.

**Why**:
1. **Idle-in-transaction state**: Long-held transactions block PostgreSQL resources (vacuum, locks)
2. **Connection exhaustion**: Ties up limited connection pool resources
3. **Transaction rollback risk**: Any failure after 15 minutes loses all uncommitted work
4. **Memory bloat**: Session's identity map accumulates objects over time

**Recommended Approach**: Use batch processing with periodic commits (every 50-100 records), creating checkpoints that allow recovery from failures.

---

## 2. Current State Analysis

### 2.1 Task Time Limits

| Task | Soft Limit | Hard Limit | Purpose |
|------|------------|------------|---------|
| `sync_deal_stakeholders_task` | 5 min | 6 min | Single deal stakeholder sync |
| `sync_all_deals_stakeholders_org_task` | 30 min | 31 min | All deals in org |
| `match_calendar_to_deals_org_task` | 10 min | 11 min | Calendar matching for org |
| `match_email_to_deals_org_task` | 10 min | 11 min | Email matching for org |
| `sync_org_timeline_task` | 30 min | 31 min | Timeline sync for org |

### 2.2 Current Session Pattern

The current implementation uses async sessions via `acquire_org_async_session()`:

```python
async def _run() -> dict[str, Any]:
    async for session in acquire_org_async_session(org_uuid):
        service = DealMeetingMatcher(session, org_uuid)
        stats = await service.process_organisation()
        return {...}
```

**Observations**:
- Session opened at task start
- Session held through entire task execution
- Single commit at the end (in services like `DealMeetingMatcher.process_organisation()`)
- No intermediate commits/checkpoints

### 2.3 Services Analysis

#### StakeholderSyncService (`stakeholder_sync_service.py`)
- **Good**: Commits after each deal in `sync_for_deal()` (line 269)
- **Good**: Handles rollback on per-deal failure (line 173)
- **Issue**: Session passed at construction, held for entire org sync

#### DealMeetingMatcher (`deal_meeting_matcher.py`)
- **Issue**: Single commit at end of `process_organisation()` (line 370)
- **Risk**: Processing 100+ deals in one transaction
- **Memory**: Caches contacts in `_contact_cache_by_id` (unbounded)

#### DealEmailMatcher (`deal_email_matcher.py`)
- **Issue**: Single commit at end of `process_organisation()` (line 724)
- **Risk**: Processes BATCH_SIZE=500 emails in one transaction
- **Memory**: In-memory maps (`_team_email_to_deals`, `_contact_email_to_deals`)

### 2.4 Connection Pool Configuration

From `provider_async.py`:
```python
pool_pre_ping=True,                          # Test connection before use
pool_recycle=database_config.POOL_RECYCLE_SEC,  # Recycle stale connections
pool_size=database_config.DATABASE_POOL_SIZE,
max_overflow=database_config.DATABASE_MAX_OVERFLOW,
```

Workers set minimal pool limits (from `celery_app.py`):
```python
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"
```

---

## 3. Key Findings from Research

### 3.1 SQLAlchemy Session Best Practices

From [SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html):

> "Keep the lifecycle of the session separate and external from functions and objects that access and/or manipulate database data."
> "Make transactions short, meaning they end at the series of a sequence of operations, instead of being held open indefinitely."

**Key Principles**:
1. Sessions should be short-lived (seconds, not minutes)
2. Transactions should end quickly after a logical unit of work
3. After `commit()`, connection is released back to pool
4. Use context managers for reliable cleanup

### 3.2 Idle-in-Transaction Problem

From [Gorgias Engineering Blog](https://www.gorgias.com/blog/prevent-idle-in-transaction-engineering):

> "Idle in transaction connections occur due to connections stuck in a transaction... waiting for the transaction to be committed or rolled back."

**Impacts**:
- Blocks PostgreSQL vacuum (table bloat)
- Holds row locks unnecessarily
- Consumes connection pool resources
- Can cause deadlocks in concurrent scenarios

### 3.3 PostgreSQL Timeout Settings

From [PostgreSQL Documentation](https://www.postgresql.org/docs/current/runtime-config-client.html):

| Setting | Purpose | Recommendation |
|---------|---------|----------------|
| `statement_timeout` | Max query duration | 30-60s for web, longer for batch |
| `idle_in_transaction_session_timeout` | Max idle time in transaction | 5-10 min |
| `transaction_timeout` (PG 17+) | Max transaction duration | Set per-session for long tasks |

### 3.4 Celery + SQLAlchemy Best Practices

From [Celery School](https://celery.school/sqlalchemy-session-celery-tasks):

1. **Session per task request**: Create session at task start, close at end
2. **Pass IDs, not objects**: Never pass SQLAlchemy model objects to tasks
3. **Checkpoint pattern**: Use separate sessions for domain work vs. progress tracking
4. **expire_on_commit=False**: Prevent DetachedInstanceError in long tasks

---

## 4. Identified Risks

### 4.1 High Priority

| Risk | Current State | Impact |
|------|---------------|--------|
| **Long transactions** | 10-30 min sessions | Blocks vacuum, connection exhaustion |
| **Single commit** | All work in one transaction | Complete rollback on late failure |
| **Memory growth** | Unbounded caches | OOM on large orgs |
| **No checkpointing** | No intermediate saves | Lost work on crash |

### 4.2 Medium Priority

| Risk | Current State | Impact |
|------|---------------|--------|
| **No progress tracking** | Binary success/fail | No visibility into long tasks |
| **Session reuse** | Single session for all deals | Accumulated identity map |
| **Missing pool_recycle** | Default value | Stale connections possible |

### 4.3 Low Priority (Informational)

| Risk | Current State | Notes |
|------|---------------|-------|
| **No LIFO pool** | Default FIFO | LIFO reduces stale connections |
| **No DB-level timeouts** | App-level only | Defense in depth missing |

---

## 5. Recommendations

### 5.1 Batch Processing with Periodic Commits

**Pattern**: Process records in batches, commit after each batch.

```python
async def process_organisation(self) -> dict[str, int]:
    BATCH_SIZE = 50
    stats = {"deals_processed": 0, "meetings_linked": 0}

    # Get all deal IDs first
    deal_ids = await self._get_all_deal_ids()

    for i, deal_id in enumerate(deal_ids):
        await self._process_single_deal(deal_id, stats)

        # Checkpoint every BATCH_SIZE deals
        if (i + 1) % BATCH_SIZE == 0:
            await self.session.commit()
            logger.info(f"Checkpoint: processed {i + 1}/{len(deal_ids)} deals")

    # Final commit for remaining
    await self.session.commit()
    return stats
```

**Benefits**:
- Limits transaction scope
- Creates recovery points
- Releases locks periodically
- Provides progress visibility

### 5.2 Fresh Session Per Logical Unit

**Pattern**: Create new session for each major operation.

```python
async def process_organisation(self) -> dict[str, int]:
    deal_ids = await self._get_deal_ids()  # Quick query

    stats = {"processed": 0, "linked": 0}

    for deal_id in deal_ids:
        # Fresh session for each deal
        async for session in acquire_org_async_session(self.org_id):
            try:
                result = await self._process_deal(session, deal_id)
                stats["processed"] += 1
                stats["linked"] += result["linked"]
            except Exception as e:
                logger.error(f"Failed deal {deal_id}: {e}")
                # Other deals continue

    return stats
```

**Benefits**:
- Isolated failures
- Clean identity map per deal
- Predictable memory usage
- Better resource utilization

### 5.3 Cache Management

**Pattern**: Bound cache sizes and clear periodically.

```python
from functools import lru_cache

class DealMeetingMatcher:
    def __init__(self, session, org_id):
        self.session = session
        self.org_id = org_id
        # Use LRU cache with max size
        self._get_contact_cached = lru_cache(maxsize=1000)(self._get_contact)

    async def process_organisation(self):
        for i, deal in enumerate(deals):
            await self._process_deal(deal)

            # Clear cache every 100 deals to bound memory
            if (i + 1) % 100 == 0:
                self._get_contact_cached.cache_clear()
```

### 5.4 Configure Database-Level Timeouts

**Pattern**: Set PostgreSQL connection-level timeouts.

```python
engine = create_async_engine(
    url,
    connect_args={
        "server_settings": {
            "search_path": f'"{schema}",public',
            "statement_timeout": "300000",  # 5 min per statement
            "idle_in_transaction_session_timeout": "600000",  # 10 min idle
        },
    },
)
```

**For long tasks, override per-session**:
```python
async for session in acquire_org_async_session(org_id):
    await session.execute(text("SET statement_timeout = '1800000'"))  # 30 min
    # Long-running work...
```

### 5.5 Progress Tracking Table

**Pattern**: Persist task progress for resumability.

```python
# New model: TaskProgress
class TaskProgress(Base):
    __tablename__ = "task_progress"
    task_id: str  # Celery task ID
    organisation_id: UUID
    total_items: int
    processed_items: int
    last_processed_id: UUID | None
    status: str  # RUNNING, COMPLETED, FAILED
    updated_at: datetime

# In task:
async def process_with_resume(self, task_id, org_id):
    progress = await get_or_create_progress(task_id, org_id)

    deals = await get_deals_after(progress.last_processed_id)
    for deal in deals:
        await process_deal(deal)
        await update_progress(progress, deal.deal_id)  # Separate session
```

---

## 6. Implementation Plan

### Phase 1: Quick Wins (Low Risk)

| Change | Location | Effort |
|--------|----------|--------|
| Add batch commits to `DealMeetingMatcher.process_organisation()` | `deal_meeting_matcher.py` | 1 hour |
| Add batch commits to `DealEmailMatcher.process_organisation()` | `deal_email_matcher.py` | 1 hour |
| Bound cache sizes with LRU | Both matchers | 30 min |
| Add progress logging every N records | All org-level tasks | 30 min |

### Phase 2: Structural Improvements (Medium Risk)

| Change | Location | Effort |
|--------|----------|--------|
| Configure idle_in_transaction_session_timeout | `provider_async.py` | 1 hour |
| Refactor matchers to commit per-deal instead of per-org | Both matchers | 2 hours |
| Add session refresh after batch commits | All services | 1 hour |

### Phase 3: Resilience (Higher Effort)

| Change | Location | Effort |
|--------|----------|--------|
| Implement TaskProgress model for resumability | New model + tasks | 4 hours |
| Fresh session per deal pattern | All org-level tasks | 4 hours |
| Add distributed lock per-deal | Task lock utility | 2 hours |

---

## 7. Monitoring & Validation

### 7.1 Metrics to Track

```sql
-- PostgreSQL: Check idle-in-transaction connections
SELECT pid, state, query_start, now() - query_start AS duration
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - query_start > interval '5 minutes';

-- Connection pool usage
SELECT count(*) as connections, state
FROM pg_stat_activity
GROUP BY state;
```

### 7.2 Application Metrics

Add logging for:
- Session duration per task
- Records processed per commit
- Cache hit/miss rates
- Memory usage per task

```python
import time

async def process_organisation(self):
    start = time.time()
    # ... processing ...
    duration = time.time() - start
    logger.info(f"Task completed: duration={duration:.1f}s, deals={stats['deals_processed']}")
```

### 7.3 Validation Tests

1. **Load test**: Process 1000+ deals, verify no connection exhaustion
2. **Failure test**: Kill task mid-execution, verify partial work saved
3. **Memory test**: Monitor memory growth over 1000 deals
4. **Concurrency test**: Run multiple org-level tasks simultaneously

---

## References

- [SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [SQLAlchemy Connection Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- [Gorgias: Avoiding Idle-in-Transaction](https://www.gorgias.com/blog/prevent-idle-in-transaction-engineering)
- [PostgreSQL Timeout Configuration](https://www.postgresql.org/docs/current/runtime-config-client.html)
- [Celery School: SQLAlchemy Session Handling](https://celery.school/sqlalchemy-session-celery-tasks)
- [Crunchy Data: Statement Timeout](https://www.crunchydata.com/blog/control-runaway-postgres-queries-with-statement-timeout)

---

## Appendix: Current Code Patterns

### A.1 Current Task Pattern (Anti-Pattern for Long Tasks)

```python
# Current: Single session for entire org processing
@app.task(soft_time_limit=1800)  # 30 minutes!
def sync_org_timeline_task(self, organisation_id):
    async def _run():
        async for session in acquire_org_async_session(org_uuid):
            service = DealTimelineSyncService(session, org_uuid)
            result = await service.sync_organisation_timeline()
            await session.commit()  # Single commit after 30 min work
            return result
    return run_async(_run())
```

### A.2 Recommended Pattern

```python
# Recommended: Batch commits with progress tracking
@app.task(soft_time_limit=1800)
def sync_org_timeline_task(self, organisation_id):
    async def _run():
        BATCH_SIZE = 50
        org_uuid = UUID(organisation_id)

        # Get work items first (quick query)
        async for session in acquire_org_async_session(org_uuid):
            deal_ids = await get_all_deal_ids(session)

        # Process in batches with fresh session scope
        results = {"processed": 0, "created": 0}
        for batch_start in range(0, len(deal_ids), BATCH_SIZE):
            batch = deal_ids[batch_start:batch_start + BATCH_SIZE]

            async for session in acquire_org_async_session(org_uuid):
                for deal_id in batch:
                    r = await process_deal_timeline(session, deal_id)
                    results["processed"] += 1
                    results["created"] += r.get("created", 0)

                await session.commit()  # Checkpoint every BATCH_SIZE
                logger.info(f"Checkpoint: {results['processed']}/{len(deal_ids)}")

        return results

    return run_async(_run())
```
