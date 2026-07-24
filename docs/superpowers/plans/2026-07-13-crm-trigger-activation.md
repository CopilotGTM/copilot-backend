# CRM Trigger Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one persisted `crm.deal.stage_changed` mutation event into replay-safe pending
`agent_runs` for matching owner-scoped triggers, without scheduling or executing agents.

**Architecture:** A system-facing activation service loads one organization-owned CRM event and
deal, asks an activation access layer for active triggers whose current agents remain active,
applies the persisted deal-stage filter, and inserts one run per match with PostgreSQL conflict
handling on the existing idempotency index. The deal mutation facade calls this service inside the
same transaction. A separate pure mapper validates a persisted run and produces the pinned runtime
envelope a later worker will consume.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy async, PostgreSQL `ON CONFLICT`, dataclasses,
pytest, Ruff, MyPy.

---

### Task 1: Lock Matching And Runtime Contracts

**Files:**
- Create: `src/service/automation/triggers/activation.py`
- Create: `src/service/automation/triggers/runtime.py`
- Create: `tests/service/automation/triggers/test_activation_matcher.py`
- Create: `tests/service/automation/triggers/test_runtime_mapper.py`

- [ ] Write matcher tests for pipeline narrowing, `any_change`, `outcome`, and from/to
  `specific_stage` behavior using the normalized event/deal snapshot.
- [ ] Run the tests and verify RED because activation contracts do not exist.
- [ ] Add immutable activation/result and runtime-envelope dataclasses plus the smallest pure
  matcher and mapper implementations.
- [ ] Require agent id/revision consistency between the run columns and pinned snapshot; reject
  malformed snapshots instead of silently falling back to the current agent.
- [ ] Rerun until GREEN.

### Task 2: Add Organization-Scoped Activation Persistence

**Files:**
- Create: `src/service/automation/triggers/activation_access.py`
- Create: `tests/service/automation/triggers/test_activation_access.py`

- [ ] Write SQL-boundary tests proving event/deal/trigger reads repeat `organisation_id`, event
  candidates require active trigger + active current agent + owner identity, and run creation uses
  the existing organization/idempotency unique index.
- [ ] Run the tests and verify RED from the missing access layer.
- [ ] Implement event/deal loading, candidate selection, idempotent `INSERT ... ON CONFLICT DO
  NOTHING`, and existing-run lookup without commits.
- [ ] Rerun until GREEN.

### Task 3: Implement CRM Event Activation

**Files:**
- Modify: `src/service/automation/triggers/activation.py`
- Create: `tests/service/automation/triggers/test_activation_service.py`

- [ ] Write service tests proving unsupported/no-op events create no runs, owner/filter matches
  create complete pending runs, replay returns the same run, and no commit or queue publish occurs.
- [ ] Run the tests and verify RED from the missing orchestration.
- [ ] Load the event and deal through the access layer, match only
  `crm.deal.stage_changed`, build stable `trigger:{trigger_id}:crm-event:{event_id}` keys, and copy
  exact trigger/agent/input snapshots into each run.
- [ ] Log created/replayed/skipped counts without recording payload contents or credentials.
- [ ] Rerun until GREEN.

### Task 4: Connect Deal Mutation Recording

**Files:**
- Modify: `src/service/crm/deals/mutation_event_service.py`
- Modify: `tests/service/crm/test_deal_mutation_event_service.py`

- [ ] Write a failing test proving a persisted deal-stage semantic event calls activation with the
  returned event id in the same session, while a no-op or unrelated semantic event does not.
- [ ] Run the focused test and verify RED.
- [ ] Invoke activation after the immutable mutation event is flushed and return the original
  mutation result. Keep failure transaction-coupled so event and pending runs cannot diverge in
  this first slice; document the ceiling and future outbox-consumer upgrade path.
- [ ] Rerun until GREEN.

### Task 5: Document And Verify

**Files:**
- Modify: `docs/API Documentation/automation/v1-agent-triggers-api.md`
- Modify: `docs/superpowers/specs/2026-07-14-agent-automation-design.md`

- [ ] Document activation ownership, matching, idempotency, transaction coupling, runtime-envelope
  boundary, and explicit absence of scheduler/broker/worker execution.
- [ ] Run all trigger activation, existing trigger definition, deal mutation, and agent validator
  tests.
- [ ] Run Ruff check/format, MyPy, the Python comment checker, Alembic heads, and
  `git diff --check`.
- [ ] Confirm no scheduler, Redis/Celery publish, worker execution, delivery, new migration,
  commit, push, or deployment change exists.

## Self-Review

- The existing generic trigger and run tables are reused; no schema or dependency is added.
- The CRM mutation row remains the durable source event and the run idempotency index makes replay
  safe for every matching trigger.
- Owner scope uses the established shared app-user/CRM-user UUID identity and never actor identity.
- Active triggers whose current agent is paused/archived are excluded, while execution still uses
  the trigger's pinned snapshot rather than the current definition.
- Runtime execution is not claimed: the mapper produces a validated envelope, but the current chat
  runner is not modified until its prompt/tool/model override contract is designed separately.
