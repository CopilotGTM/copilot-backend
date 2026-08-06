# Meeting Trigger Release Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development while implementing each task. This plan is executed inline because it depends on the current staged trigger slice.

**Goal:** Make meeting summaries single-flight per organisation and calendar event, and make runtime meeting authorization use the same owner rule as event production.

**Architecture:** All three summary-job entry paths will reuse one transaction-scoped PostgreSQL advisory-lock lookup keyed by organisation and event. Every summary worker will use an event-scoped Redis lock and generate the canonical meeting-level artifact once; the saved payload will be copied to all active deal associations before the event is emitted, then existing deal insight pipelines will fan out per association. Runtime authorization will reuse `load_meeting_event_context`, which already implements organiser-first ownership with integration-owner fallback.

**Tech Stack:** Python 3.12, SQLAlchemy/SQLModel, PostgreSQL advisory locks, Celery, pytest.

---

### Task 1: One durable summary job per event

**Files:**
- Modify: `src/service/crm/jobs/deal_meeting_job_service.py`
- Modify: `src/service/meeting_bot/summary_trigger_service.py`
- Modify: `src/workers/deal_worker/services/meeting_insight_services/meeting_summary_service.py`
- Test: `tests/service/crm/test_deal_meeting_job_service.py`
- Test: `tests/service/meeting_bot/test_meeting_summary_trigger_service.py`

- [ ] **Step 1: Write failing tests**

Add regressions proving that Recall creates one deal-independent job for an event with multiple associations and that manual summary creation uses an event-scoped locked lookup rather than `get_active_job(deal_id, ...)`.

```python
result = await service._create_meeting_summary_jobs(event_id)
assert result["created"] == 1
assert session.added[0].deal_id is None

await service.create_meeting_job(deal_id, event_id, DealJobType.MEETING_SUMMARY)
locked_lookup.assert_awaited_once_with(session, organisation_id, event_id)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/meeting_bot/test_meeting_summary_trigger_service.py tests/service/crm/test_deal_meeting_job_service.py -q`

Expected: failures showing Recall creates one job per association and manual creation still calls the deal-scoped unlocked lookup.

- [ ] **Step 3: Implement the shared transaction boundary**

Add one existing-module helper and call it from Recall, API/manual, and scheduled creation:

```python
async def lock_and_get_active_meeting_summary_job(
    session: AsyncSession,
    organisation_id: UUID,
    event_id: UUID,
) -> DealJobs | None:
    await session.execute(select(func.pg_advisory_xact_lock(...)))
    return (await session.execute(select(DealJobs).where(...))).scalars().first()
```

Recall creates one `deal_id=None` summary job. A manual request may reuse an active job only when it belongs to the same route deal; otherwise it returns a conflict instead of creating duplicate work or returning an unusable SSE job.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 command and require zero failures.

### Task 2: Canonical generation and per-deal fanout

**Files:**
- Modify: `src/workers/deal_worker/tasks/meeting_insight_tasks/meeting_summary_task.py`
- Modify: `src/workers/deal_worker/services/meeting_insight_services/meeting_summary_service.py`
- Test: `tests/workers/deal_worker/tasks/meeting_insight_tasks/test_meeting_summary_task.py`

- [ ] **Step 1: Write failing tests**

Add regressions proving the worker lock key contains organisation and event rather than job ID, a deal-linked job calls `generate_meeting_level_summary`, and the canonical save updates every active association before enqueue/commit.

```python
assert acquire_lock.call_args.args[0] == (
    f"task_lock:meeting_summary_event:{organisation_id}:{event_id}"
)
service.generate_meeting_level_summary.assert_awaited_once()
service.generate.assert_not_called()
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/workers/deal_worker/tasks/meeting_insight_tasks/test_meeting_summary_task.py -q`

Expected: failures showing the job-ID lock and deal-scoped generator are still used.

- [ ] **Step 3: Implement canonical generation**

Acquire the Redis lock only after loading the durable event ID, call the meeting-level generator for every summary job, copy the result to all active event associations in the same transaction as the canonical Meeting row and automation event, and dispatch the existing insight pipeline once for each associated deal not already owned by the scheduled chain.

- [ ] **Step 4: Verify GREEN**

Run the Task 2 command and require zero failures.

### Task 3: Align personal meeting authorization

**Files:**
- Modify: `src/workers/chat_worker/services/agent_run_execution_service.py`
- Test: `tests/workers/chat_worker/services/test_agent_run_execution_service.py`

- [ ] **Step 1: Write failing tests**

Add a personal meeting regression where Alice is the resolved organiser-owner even though another user's integration supplied the event. Assert Alice is authorized, a different principal is rejected, and the shared resolver is called exactly once.

```python
context_loader.return_value = MeetingEventContext(owner_id=alice_id)
await AgentRunExecutionService._ensure_subject_authority(session, alice_envelope)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/workers/chat_worker/services/test_agent_run_execution_service.py -q`

Expected: failure because runtime still joins integration ownership and never invokes the producer's owner resolver.

- [ ] **Step 3: Reuse the producer rule**

Load the live tenant-scoped `Meeting`, fail closed if absent, and for personal runs compare `load_meeting_event_context(...).owner_id` with the principal. Use `.scalars().first()` so multiple calendar user-state rows cannot raise `MultipleResultsFound`.

- [ ] **Step 4: Verify GREEN**

Run the Task 3 command and require zero failures.

### Task 4: Release verification

**Files:**
- Verify only; leave all changes unstaged for review.

- [ ] Run the focused meeting and runtime suite.
- [ ] Run Ruff on touched Python files.
- [ ] Run `.agents/skills/writing-python-comments/scripts/check_comment_smells.py` on the touched backend files.
- [ ] Run the broader automation/producer/meeting regression slice.
- [ ] Run `git diff --check` and `git diff --cached --check` separately.
- [ ] Inspect `git status --short` and report the exact current scope without committing, pushing, publishing, or deploying.
