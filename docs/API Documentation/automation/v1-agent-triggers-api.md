# Agent Triggers API V1

The trigger API lets an authenticated user configure when one of their agents should run. Supported
CRM events activate matching triggers and dispatch durable runs. Schedule definitions are stored
and validated, but their due-schedule dispatcher is not implemented in this worktree yet.

## Supported Triggers

- `schedule`: one-time, daily, weekdays, weekly, biweekly, or monthly in an IANA timezone.
- `crm.deal.created`: a new owned deal, optionally narrowed to selected pipelines.
- `crm.deal.stage_changed`: any stage movement, won/lost outcomes, or selected stages from one
  organization-owned pipeline.
- `crm.contact.created` and `crm.contact.deleted`: lifecycle events for an owned contact.
- `crm.account.created` and `crm.account.deleted`: lifecycle events for an owned account.

Email, meeting, Twitter/X, and webhook triggers are intentionally absent until those domains emit a
durable semantic event the trigger matcher can consume.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/users/v1/automation-trigger-catalog` | Supported contracts and CRM options |
| `GET` | `/users/v1/agents/{agent_id}/triggers` | List the user's non-archived triggers |
| `POST` | `/users/v1/agents/{agent_id}/triggers` | Create and pin a trigger |
| `GET` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}` | Get a trigger |
| `PUT` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}` | Fully replace user-editable fields |
| `POST` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}/refresh-target` | Repin to current agent revision |
| `DELETE` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}` | Soft archive a trigger |

All resource queries bind both the authenticated organization and user. Foreign IDs return the
same `404` as missing IDs.

## Schedule Example

```json
{
  "name": "Morning pipeline review",
  "status": "active",
  "trigger_type": "schedule",
  "source_domain": null,
  "event_type": null,
  "trigger_config": {
    "timezone": "Asia/Kolkata",
    "starts_at": "2026-07-14T09:00:00+05:30",
    "recurrence": "weekdays"
  },
  "filter_config": {},
  "scope_config": {"entity": "deal", "statuses": ["open"]},
  "task_config": {"goal": "Prioritize deals that need action today."}
}
```

The backend validates the timezone and aware start time, then owns `next_trigger_at` in UTC. A past
one-time schedule is rejected. A recurring schedule advances to its first future occurrence.

## CRM Deal Stage Example

```json
{
  "name": "Review deals entering negotiation",
  "status": "active",
  "trigger_type": "event",
  "source_domain": "crm",
  "event_type": "crm.deal.stage_changed",
  "trigger_config": {},
  "filter_config": {
    "schema_key": "crm.deal.stage_changed.v1",
    "pipeline_ids": ["11111111-1111-1111-1111-111111111111"],
    "stage_change_mode": "specific_stage",
    "from_stage_ids": [],
    "to_stage_ids": ["22222222-2222-2222-2222-222222222222"],
    "outcome": "any"
  },
  "scope_config": {},
  "task_config": {"goal": "Prepare the rep for the negotiation."}
}
```

`any_change` cannot contain stages or an outcome. `outcome` requires `won` or `lost` and cannot
contain stages. `specific_stage` requires exactly one pipeline and at least one from/to stage. CRM
IDs are checked against non-deleted metadata in the authenticated organization.

## Other CRM Event Configurations

Deal creation supports an optional pipeline filter:

```json
{
  "event_type": "crm.deal.created",
  "filter_config": {
    "schema_key": "crm.deal.created.v1",
    "pipeline_ids": []
  }
}
```

Contact and account lifecycle events have no additional V1 predicates. Their `filter_config`
contains only the matching versioned schema key, for example:

```json
{
  "event_type": "crm.contact.created",
  "filter_config": {"schema_key": "crm.contact.created.v1"}
}
```

The same shape applies to `crm.contact.deleted`, `crm.account.created`, and `crm.account.deleted`,
using `<event_type>.v1` as the schema key. Clients should render definitions and defaults from
`GET /users/v1/automation-trigger-catalog` rather than hard-coding this list.

## Target Pinning And Updates

Creation stores a server-owned execution snapshot and `target_revision`. Editing the agent does not
silently change existing triggers. Responses expose `target_update_available`; the explicit
`refresh-target` route is the only way to repin.

`PUT`, refresh, and delete require the last-read timezone-aware `expected_updated_at`. A stale value
returns `409`, preventing two browser tabs from silently overwriting each other. `PUT` is a complete
replacement; `DELETE` is the only way to archive.

## Server-Owned Fields

Clients cannot set organization/owner identity, target identity or snapshot, owner scope, schema
version, delivery config, canonical hash, schedule projection, or operational counters. JSON config
is bounded and credential-like keys are rejected.

Expected failures are `400` for invalid trigger configuration, `404` for inaccessible resources,
`409` for stale writes/duplicates/invalid activation, `422` for malformed HTTP bodies, and `429`
for rate limiting.

## Execution Boundary

Saving an active trigger does not itself create an `agent_runs` row, publish Redis work, or run the
agent. A supported source event creates the run.

## Event Activation

A supported CRM write stores its immutable mutation event and one automation dispatch in the same
organization transaction. After that transaction commits, the producer immediately publishes the
dispatch identity to the chat worker with bounded exponential retries. Trigger matching remains
asynchronous, keeping trigger queries and Redis work out of the CRM transaction.

For each dispatch, activation:

1. Loads the exact source event and owner-bearing entity inside the same organization.
2. Uses the entity's canonical `owner_id` to select that user's active matching triggers.
3. Excludes triggers whose current target agent is paused or archived.
4. Applies the event-specific pipeline or stage filters when present.
5. Creates one pending `agent_runs` row per match using the trigger's pinned agent snapshot.
6. Marks the dispatch processed in the same commit as the new runs, then publishes the runs with
   bounded exponential retries.

Zero matches is a successful terminal activation. One source event may create several runs. Dispatch
and run uniqueness make broker redelivery safe, while terminal failures stop endlessly reprocessing
malformed events. A future low-frequency recovery relay will repair dispatches left pending when a
producer process exits or exhausts all direct broker publication retries.

Each run uses
`trigger:{trigger_id}:{source_domain}:{event_type}:{source_event_id}` as its idempotency key. The run
contains exact trigger, agent, input, and subject snapshots. Subject IDs are opaque strings so CRM
UUIDs and future provider IDs use the same contract; `deal_id` is present only for deal-scoped runs.

The execution task atomically claims a committed run, releases the tenant session before model/tool
work, resolves only pinned capabilities, and stores a fenced terminal result. If post-commit Redis
publication fails, `chat.recover_agent_runs` republishes the durable pending run.

Run state and captured metadata are available through `GET /users/v1/agent-runs` and
`GET /users/v1/agent-runs/{run_id}`.
