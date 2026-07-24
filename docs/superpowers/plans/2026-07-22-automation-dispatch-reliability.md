# Automation Dispatch Reliability Follow-up

## Status

Deferred. The releaseable Phase 1 is defined in
`2026-07-22-automation-event-activation-worker.md` and intentionally has no
`chat.recover_automation_events` periodic task.

## Phase 1 boundary

- The source mutation and `AutomationEventDispatch` commit together.
- A post-commit publisher sends `chat.activate_automation_event` directly with
  bounded 1, 2, and 4 second retries.
- Trigger activation is idempotent and commits `AgentRun` rows before publishing
  execution work.
- Existing `chat.recover_agent_runs` repairs the later AgentRun-to-Celery gap.
- If every direct activation publication attempt fails, the dispatch remains
  pending for manual inspection; Phase 1 does not automatically republish it.

This is an explicit release trade-off, not an exactly-once guarantee. It is
sufficient to exercise and release the agent flow while the broker is healthy.

## Future production hardening

Add this only when automatic first-handoff recovery is required:

1. A low-frequency, bounded relay scans stale non-terminal dispatches.
2. The relay republishes only organization and dispatch IDs.
3. Duplicate delivery remains safe through existing dispatch and AgentRun
   idempotency constraints.
4. Metrics and alerts expose oldest pending age, terminal failures, and relay
   publication failures.
5. Operators can replay terminal failures deliberately.

Do not add enqueue metadata merely to mirror another job model. Add only fields
required by the recovery query or an actual operational debugging requirement.

## Migration safety

Migration `080` is currently intended for the Phase 1 dispatch table. Amend it
only while deployment history proves it is unshipped; otherwise add a new
migration instead of rewriting deployed history.
