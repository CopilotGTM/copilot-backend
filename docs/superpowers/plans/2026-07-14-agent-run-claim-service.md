# Agent Run Claim Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Atomically claim one pending or stale-running `AgentRun` and return its pinned runtime envelope without dispatching a queue task or executing an LLM.

**Architecture:** Add a generic run-claim persistence boundary under `service/automation/runs`. PostgreSQL selects the single concurrent winner through one organization-scoped conditional `UPDATE ... RETURNING`; a small service validates lease inputs, computes the stale cutoff, and maps the claimed row through the existing fail-closed runtime mapper. Transaction commit remains with the future worker so it can commit and release the database session before external agent execution.

**Tech Stack:** Python 3.12, SQLAlchemy/SQLModel async sessions, PostgreSQL, pytest

---

### Task 1: Lock the atomic SQL contract

**Files:**
- Create: `tests/service/automation/runs/test_claim_access.py`
- Create: `src/service/automation/runs/__init__.py`
- Create: `src/service/automation/runs/access.py`

- [ ] **Step 1: Write the failing access test**

Create a recording async session, call `AgentRunClaimAccess.claim_run`, compile the emitted statement with the PostgreSQL dialect, and assert that it:

- filters by `organisation_id` and `run_id`;
- accepts `pending` or a `running` row with a missing/expired claim;
- writes `running`, `claimed_at`, `claimed_by`, and `attempt_count + 1`;
- preserves the first `started_at` with `coalesce`;
- returns the claimed `AgentRun`.

- [ ] **Step 2: Run the access test and verify RED**

Run:

```powershell
uv run pytest tests/service/automation/runs/test_claim_access.py -q
```

Expected: collection fails because `src.service.automation.runs.access` does not exist.

- [ ] **Step 3: Implement the minimum access layer**

Implement:

```python
class AgentRunClaimAccess:
    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None: ...

    async def claim_run(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> AgentRun | None: ...
```

Use a single conditional `UPDATE ... RETURNING(AgentRun)` with no commit.

- [ ] **Step 4: Run the access test and verify GREEN**

Run the same focused pytest command and expect it to pass.

### Task 2: Add the claim service contract

**Files:**
- Create: `tests/service/automation/runs/test_claim_service.py`
- Create: `src/service/automation/runs/claim_service.py`

- [ ] **Step 1: Write failing service tests**

Cover these behaviors independently:

- a claimed row maps to `AgentRunRuntimeEnvelope` from its immutable snapshots;
- a concurrent/no-longer-claimable row returns `None`;
- whitespace worker identities are rejected before persistence;
- naive claim timestamps are rejected;
- the stale cutoff equals `claimed_at - lease_timeout`.

- [ ] **Step 2: Run the service test and verify RED**

Run:

```powershell
uv run pytest tests/service/automation/runs/test_claim_service.py -q
```

Expected: collection fails because `claim_service.py` does not exist.

- [ ] **Step 3: Implement the minimum claim service**

Implement `AgentRunClaimService.claim_run` with an injectable access object, a positive lease duration, timezone-aware timestamps, normalized worker identity, and delegation to `map_agent_run_runtime` only after the database claim succeeds.

- [ ] **Step 4: Run the service test and verify GREEN**

Run the same focused pytest command and expect all claim-service tests to pass.

### Task 3: Verify the bounded slice

**Files:**
- Verify: `src/service/automation/runs/access.py`
- Verify: `src/service/automation/runs/claim_service.py`
- Verify: `tests/service/automation/runs/test_claim_access.py`
- Verify: `tests/service/automation/runs/test_claim_service.py`

- [ ] **Step 1: Run automation regressions**

```powershell
uv run pytest tests/service/automation -q
```

- [ ] **Step 2: Run static checks**

```powershell
uv run ruff check src/service/automation/runs tests/service/automation/runs
uv run ruff format --check src/service/automation/runs tests/service/automation/runs
uv run mypy src/service/automation/runs
```

- [ ] **Step 3: Check comments and diff hygiene**

```powershell
uv run python .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/service/automation/runs
git diff --check
```

- [ ] **Step 4: Preserve the rollout boundary**

Do not add a Celery task, poller, runtime execution adapter, migration, commit, push, deployment, or live database change in this slice.
