# Automation Event Activation Worker Plan

## Goal

Move trigger matching out of CRM writes while delivering contact, account, and deal events to
user-configured agents immediately after the source transaction commits.

## Production flow

```text
CRM write transaction
  -> CRM mutation event
  -> AutomationEventDispatch
  -> commit
  -> direct chat.activate_automation_event publication (1s, 2s, 4s retries)
  -> zero matches: dispatch succeeds
  -> one or more matches: one AgentRun per trigger
  -> direct chat.execute_agent_run publication (1s, 2s, 4s retries)
  -> claim, execute pinned user agent, persist fenced result
```

The source mutation and dispatch row share one transaction. A SQLAlchemy post-commit hook publishes
only the organization and dispatch IDs, so a worker never observes rolled-back source data. Queue
redelivery is safe because dispatch source identity and agent-run idempotency keys are unique.

## Two durable job records

- `AutomationEventDispatch` tracks whether one source semantic event was evaluated for triggers.
- `AgentRun` tracks one matched agent's claim, retries, output, cost, and terminal status.

One dispatch can produce zero, one, or many runs. Zero matches is a successful dispatch rather than
an empty agent run.

## Implementation

1. Migration `080` adds the organization-scoped dispatch table and pending index.
2. CRM mutation recording inserts supported dispatches in the source transaction.
3. `events/delivery.py` stages identities on the session and publishes after commit only.
4. The activation task uses exponential Celery retries for source or matching failures.
5. Activation loads the persisted event, selects active owner-scoped triggers, and creates
   idempotent runs from pinned snapshots.
6. Run publication uses the same bounded exponential broker retry policy.
7. The existing run executor claims each run and executes `UserConfiguredAgent` with immutable
   organization, owner, subject, trigger, and agent snapshots.

## Deliberate follow-up

Polling is not part of this slice. The code contains an owned TODO to add a low-frequency recovery
relay for dispatches left pending when the producer exits or exhausts every direct broker retry.
That relay must remain a repair path, not the normal delivery path.

## Verification contract

- No activation task is published before the source transaction commits.
- Rollback drops staged delivery.
- Direct broker publication retries after 1, 2, and 4 seconds.
- No `chat.recover_automation_events` route or beat schedule exists.
- Zero matches marks the dispatch processed without creating a run.
- Multiple matching triggers create independently idempotent runs.
- Contact/account runs execute without fabricated deal context.
- Dispatch and run SQL always repeat the organization boundary.
