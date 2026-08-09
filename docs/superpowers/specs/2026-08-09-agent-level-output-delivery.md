# Agent-Level Output Delivery Design

**Status:** Proposed; backend implementation and deployment not started

**Date:** 2026-08-09

**Applies to:** Agent definitions, output-delivery configuration, automation activation,
agent-run audit snapshots, successful-run notification intent, API contracts, migrations,
and the later Agent Studio frontend change

**Supersedes:** Only the trigger-owned configuration decision in
`2026-08-07-agent-output-transactional-email-notification.md`. The existing platform sender,
canonical output rendering, `NotificationDelivery` ledger, retry fencing, recovery, run-history
status, and connected-mailbox separation remain authoritative.

## 1. Decision

Output delivery is an **agent-level decision**, not a trigger decision.

- An agent defines **what runs** and **how its output is formatted**.
- A trigger defines **when it runs**, **under whose authority**, **which records it may use**, and
  **who may discover the resulting run**.
- Output delivery defines **which external channels receive successful output**.
- Run history is always available and is not a selectable external channel.

The V1 external channel is platform email. An empty channel list means the result remains only in
CopilotGTM run history. Enabling email applies to every future run created for the agent, regardless
of which trigger activates it.

```text
UserAgent
  output format: text | json
  output delivery: [] | [email_notification]
       |
       | current decision copied at activation
       v
AgentRun.output_delivery_config (immutable)
       |
       | successful completion
       v
NotificationDelivery rows -> existing delivery worker -> platform email
```

This design does not move a trigger field visually while leaving the backend trigger-owned. It
makes the domain boundary real in persistence, APIs, validation, runtime behavior, and audit data.

### 1.1 Development-stage replacement policy

This functionality is still being built in Dev. There is no requirement to preserve existing
trigger-owned delivery choices, existing agent runs, or compatibility with the current Agent Studio
frontend contract.

Therefore this change is a clean replacement:

- delete the trigger-owned delivery API, model, validation, hash, snapshot, presentation, tests,
  and UI contract;
- do not add a trigger V2, legacy adapter, dual read, dual write, compatibility flag, or data
  backfill;
- do not keep a neutral trigger delivery field as a migration salt;
- reset disposable Agent Studio automation data when the schema changes;
- retain only the delivery ledger/worker code that belongs to the new agent-level design.

Database schema definitions still have to match the code, but that is schema construction—not a
promise to migrate development records into the new semantics.

## 2. Why the current model is wrong

The current implementation stores `delivery_config` on `automation_triggers`, includes it in the
trigger request and trigger `config_hash`, copies it into `AgentRun.trigger_snapshot`, and creates
email intents from that trigger snapshot after success.

That creates four product problems:

1. Two triggers for one agent can silently disagree about where the same agent's output goes.
2. The trigger editor owns a decision unrelated to event matching or execution authority.
3. The agent detail page can only aggregate delivery indirectly from triggers.
4. An agent without a trigger has no output-delivery decision even though delivery belongs to the
   reusable agent.

A frontend-only relocation would hide these inconsistencies rather than fix them.

## 3. Scope and non-goals

### 3.1 In scope

- one agent-level output-delivery decision;
- Run history as the permanent source of truth;
- platform email as the first optional external destination;
- a separate optimistic-concurrency token for delivery edits;
- an immutable delivery snapshot on each new run;
- removal of delivery from public trigger contracts and trigger presentation;
- clean development schema replacement with no legacy data preservation;
- preservation of the existing delivery ledger, worker, retry, recovery, rendering, and run status;
- backend tests and documentation before frontend implementation.

### 3.2 Explicitly out of scope

- arbitrary email addresses, external contacts, Cc, Bcc, or distribution lists;
- custom subjects, templates, bodies, or model-authored delivery instructions;
- sending output through a connected Gmail or Outlook mailbox;
- notifications for failed, cancelled, or validation-failed runs;
- Slack, Teams, WhatsApp, webhook, or file-export adapters in this change;
- changing trigger authority, scope, audience, matching, scheduling, or webhook behavior;
- a new agent execution path or a second model call;
- HubSpot or Salesforce work;
- frontend implementation in the backend-first phase.

## 4. Domain invariants

The implementation must preserve all of these rules.

1. Run history always contains the canonical persisted output. It is not stored in the external
   channel list and cannot be removed.
2. Output delivery is owned by the authenticated owner of the private `UserAgent`.
3. Updating output delivery does not change the executable agent definition, increment the agent
   execution revision, or make pinned triggers report `target_update_available`.
4. A delivery edit affects only runs activated after that edit commits.
5. A run stores the complete normalized delivery decision used when it was activated. Later agent
   edits never reinterpret that run.
6. Recipient authorization remains run-scoped. Enabling an external channel never grants access to
   a run or adds someone to its audience.
7. For a personal run, the authorized recipient is the active execution principal. For an
   organisation run, recipients are the internal user IDs already pinned in the run audience.
8. An organisation run with no authorized recipients produces a durable skipped delivery outcome;
   it does not fall back to the agent owner, trigger creator, all admins, or an external address.
9. Recipient membership and active/approved status are revalidated before provider I/O.
10. Only a fenced successful completion may create delivery intents. Delivery failure never changes
    a successful run to failed and never reruns the agent.
11. Sender, template, subject, rendering, recipient addresses, and provider credentials remain
    server-owned.
12. Every tenant-table read and write repeats `organisation_id`; queued IDs are never authorization.

## 5. Canonical configuration contract

Use a small closed contract:

```json
{
  "channels": []
}
```

or:

```json
{
  "channels": ["email_notification"]
}
```

Rules:

- `channels` is required in write requests;
- values are unique;
- V1 accepts only `email_notification`;
- unknown channels, duplicate values, arbitrary objects, recipients, sender fields, and credentials
  are rejected;
- the server stores a canonical order so equality and audit output are deterministic;
- an empty list means no external delivery; it does not mean output is discarded.

The list is the smallest shape that matches the product's add/remove-destination UI and permits a
future channel without replacing a `none | email` discriminator. It is not a generic provider
configuration framework.

## 6. Persistence design

Do not add a new table. The decision has one owner and one current value, so columns on
`user_agents` plus an immutable snapshot on `agent_runs` are sufficient.

### 6.1 `user_agents`

Add:

| Column | Type | Null | Default | Purpose |
| --- | --- | --- | --- | --- |
| `output_delivery_config` | `JSONB` | No | `{"channels": []}` | Current normalized agent-level decision |
| `output_delivery_updated_at` | timezone-aware timestamp | No | `now()` | Delivery-specific concurrency token |

Do not reuse `revision` as the delivery concurrency token. `revision` pins executable agent
snapshots to triggers. Incrementing it for a delivery-only edit would falsely mark every trigger as
out of date.

The ordinary `updated_at` may still change because the row changed, but `revision` and the trigger
target snapshot remain unchanged.

### 6.2 `agent_runs`

Add:

| Column | Type | Null | Default | Purpose |
| --- | --- | --- | --- | --- |
| `output_delivery_config` | `JSONB` | No | `{"channels": []}` | Immutable activation-time delivery decision |

Do not store this decision inside `trigger_snapshot` or the pinned executable `agent_snapshot`.
It has a separate lifecycle from both:

- `trigger_snapshot` records why and under what authority the run exists;
- `agent_snapshot` records the executable revision;
- `output_delivery_config` records where successful output was intended to go.

### 6.3 `automation_triggers.delivery_config`

Delete this column from the current schema. Also delete the corresponding model field, request and
response schemas, validation, serialization, config hashing, snapshots, fixtures, and tests. There
is no runtime fallback and no retained legacy value.

### 6.4 `notification_deliveries`

No schema change is required. Continue using one row per channel/recipient with the existing unique
identity:

```text
(organisation_id, notification_type, source_type, source_id, channel, target_key)
```

For an enabled channel with no authorized run recipients, create one already-terminal `SKIPPED`
audit row using a reserved, code-owned target key such as `policy:no_authorized_recipient` and safe
error code `no_authorized_recipient`. Do not publish that row to the delivery worker. This makes the
configured-but-inapplicable outcome visible without inventing an email address.

## 7. API design

### 7.1 Agent detail

Add a nested state to `AgentDetail` only; the agent list summary does not need it.

```json
{
  "output_delivery": {
    "channels": ["email_notification"],
    "updated_at": "2026-08-09T10:30:00Z"
  }
}
```

Agent creation and template materialization default to `channels: []`. Templates do not prescribe
personal notification preferences.

The complete agent-definition PUT and the capabilities PUT do not accept or overwrite
`output_delivery`. This prevents a stale general editor payload from losing a newer delivery edit.

### 7.2 Replace output delivery

Add one owner-scoped subresource to the existing agent router:

```http
PUT /users/v1/agents/{agent_id}/output-delivery
Content-Type: application/json
```

```json
{
  "channels": ["email_notification"],
  "expected_updated_at": "2026-08-09T10:30:00Z"
}
```

Success returns the normalized nested state:

```json
{
  "status": "SUCCESS",
  "message": "Agent output delivery updated successfully",
  "data": {
    "channels": ["email_notification"],
    "updated_at": "2026-08-09T10:31:12Z"
  }
}
```

Behavior:

- owner and organisation come only from authentication;
- foreign, archived, and unknown agents return the same `404 Agent not found`;
- a stale delivery timestamp returns `409 Agent output delivery changed since it was loaded;
  refresh and retry`;
- success changes `output_delivery_config` and `output_delivery_updated_at` atomically;
- success does not increment `UserAgent.revision`;
- repeated submission with the current timestamp is a normal write and returns a new timestamp;
- no provider work or Celery publication occurs during configuration.

A separate endpoint is justified here because the resource has separate concurrency and does not
belong to the execution-definition replacement contract. No separate GET endpoint is needed;
`GET /users/v1/agents/{agent_id}` returns the current state.

### 7.3 Capability catalog

Move selectable delivery metadata out of `AutomationTriggerCatalog` and into
`AgentCapabilityCatalog`:

```json
{
  "output_delivery_channels": [
    {
      "key": "email_notification",
      "label": "Email notification",
      "description": "Email successful output to the authorized recipients of each run.",
      "sender_mode": "platform",
      "body_source": "agent_output_markdown"
    }
  ]
}
```

`Run history` is not returned as a delivery channel because it is always present.

### 7.4 Trigger contract removal

Remove from public trigger contracts:

- request field `delivery`;
- detail field `delivery`;
- presentation fields `delivery_mode`, `delivery_summary`, `delivery_status`, and
  `delivery_reason`;
- catalog field `delivery_channels`.

Old trigger payloads containing `delivery` fail validation because request schemas remain
`extra="forbid"`. This is intentional contract replacement, not a V2 fork or silent ignore. The
frontend must be updated with the backend contract before this development feature is considered
usable again.

### 7.5 Run detail

Expose the immutable normalized `output_delivery` decision on run detail for audit and support.
Continue deriving actual delivery status and per-target retry capability from
`NotificationDelivery`; configured intent and provider outcome are different facts.

No output-only endpoint is added.

## 8. Runtime flow

### 8.1 Activation

Change the current active-agent readiness query from a set of active IDs into one batched mapping:

```text
agent_id -> current output_delivery_config
```

The query remains tenant-scoped and still excludes inactive agents. It must not cause one query per
trigger.

When a candidate matches:

1. use the trigger's pinned `target_snapshot` and `target_revision` for execution;
2. use the current agent-level `output_delivery_config` from the batched readiness query;
3. normalize/validate it defensively;
4. write it to `AgentRun.output_delivery_config` in the same transaction as the run;
5. do not copy legacy trigger delivery into `trigger_snapshot`.

This intentionally allows an output-delivery edit to take effect without refreshing every trigger,
while the executable agent definition remains pinned.

### 8.2 Successful completion

The fenced completion transaction remains:

```text
persist successful output
  -> inspect AgentRun.output_delivery_config
  -> create/reuse PENDING delivery rows, or one SKIPPED policy row
  -> append any automation completion event
  -> update trigger health counters
  -> commit
  -> publish only PENDING delivery IDs
```

The lifecycle service must never reread the current agent or trigger decision after activation.
Doing so would reinterpret an in-flight run.

### 8.3 Recipient derivation

Recipient selection remains server-owned and visibility-safe:

| Run authority/audience | Recipient IDs |
| --- | --- |
| Personal / principal | `AgentRun.execution_principal_id` |
| Organisation / explicit users | Pinned `AgentRun.audience_config.user_ids` |
| Organisation / subject participants | Event-time IDs pinned into `AgentRun.audience_config` |
| Organisation / none or empty resolved audience | No recipients; create skipped policy outcome |

Deduplicate and deterministically order UUIDs before inserting rows. At send time, resolve the
current workspace email only after revalidating the user is active, approved, and in the same
organisation.

The agent owner and trigger creator are not fallback recipients. This preserves the rule that
delivery cannot broaden run discovery.

### 8.4 Worker and retries

Keep the current worker contract and ledger state machine:

- task payload contains only organisation and delivery IDs;
- worker loads a successful tenant run and fenced delivery row;
- worker verifies `run.output_delivery_config` contains the row's channel;
- worker renders persisted text/JSON output without a model call;
- platform sender remains separate from connected Gmail/Outlook;
- provider uncertainty becomes `NEEDS_ATTENTION` rather than blind resend;
- recovery republishes pending or stale claimed rows;
- retry never reruns the agent.

Existing sent, pending, failed, skipped, and needs-attention rows are not altered by later
configuration changes.

## 9. Concurrency and timing semantics

These cases must be explicit in API copy and tests:

| Race | Required result |
| --- | --- |
| Two tabs edit delivery | First matching timestamp wins; second gets `409` |
| Delivery changes after event but before activation | Activation uses the committed value it reads when creating the run |
| Delivery changes after run creation but before completion | Run keeps its activation-time snapshot |
| Delivery is disabled while a delivery is pending | Existing durable row still sends/retries |
| Agent definition is edited | Trigger execution remains pinned; delivery state is unchanged |
| Trigger is edited or refreshed | Agent delivery state is unchanged |
| Agent is paused after a run starts | Existing run completes and follows its snapshot; no new run should activate |
| Agent is archived after a run starts | Existing durable run and delivery audit remain; no new activation |

Frontend copy should say: **Changes apply to runs started after you save.**

## 10. Development schema replacement

There is no data migration from trigger-owned delivery to agent-owned delivery. Current Agent
Studio data is disposable and the two contracts have different meaning, so guessing a new
agent-level decision from old per-trigger rows would be misleading.

### 10.1 Migration source of truth

The current development migration for this feature is
`088_agent_output_notifications`. Update that migration rather than adding `089`/`090` compatibility
revisions solely for unfinished behavior.

The final migration chain must:

1. create `user_agents.output_delivery_config` as non-null JSONB with
   `{"channels": []}` as its server default;
2. create `user_agents.output_delivery_updated_at` as a non-null timezone-aware timestamp with
   `now()` as its server default;
3. create `agent_runs.output_delivery_config` as non-null JSONB with
   `{"channels": []}` as its server default;
4. never create `automation_triggers.delivery_config` anywhere in the disposable development
   migration chain;
5. leave `notification_deliveries` and its output-email claim/recovery additions intact;
6. make downgrade structurally reverse those schema changes for local migration testing only.

If keeping the migration chain internally consistent requires updating the development table
definitions in `078_agent_automation_tables` or `087_ai_native_automation`, update them too. The
postcondition is authoritative: a database built from zero must match the final SQLModel metadata
without ever exposing trigger-owned delivery to application code.

### 10.2 Existing Dev database

Because an edited migration will not rerun against a database that already records its revision,
the implementation must choose one explicit Dev operation:

- rebuild the disposable Dev tenant schema from the corrected migration chain; or
- clear/recreate only the Agent Studio automation tables and restamp/reapply the corrected
  development migrations using the repository's supported migration tooling.

Do not write a backfill, rehash script, dual-read bridge, or compatibility revision. Existing
triggers, runs, event dispatches, and notification rows for this unfinished feature may be deleted.
The exact destructive Dev operation must be resolved and printed before execution and requires
fresh approval; this design document does not perform it.

### 10.3 Trigger hash consequence

Delete delivery from the canonical trigger hash immediately. Any current hashes and pending direct
events that reference them are discarded with the disposable automation data. There is no
collision migration, neutral hash salt, event drain, or legacy rehash path.

Fresh triggers use the corrected hash over only target, trigger, authority, audience, and task
decisions.

### 10.4 No API versioning

Replace the current V1 development contract in place:

- agent detail gains output delivery;
- the agent output-delivery PUT is added;
- trigger requests/responses/catalog lose delivery;
- old frontend fixtures and requests are updated, not supported in parallel.

Do not create V2 routes, compatibility query parameters, feature flags, or adapters for the stale
shape.

## 11. Implementation and Dev verification sequence

1. Merge the reviewed design document.
2. Update the development migration chain and SQLModel metadata to the final clean schema.
3. Implement the agent output-delivery schema, service, persistence, catalog, and PUT subresource.
4. Delete trigger-owned delivery schemas, validation, serialization, hashing, snapshots, fixtures,
   and tests.
5. Snapshot current agent delivery onto new runs at activation and update successful completion and
   worker validation to read the dedicated run field.
6. Build an empty isolated tenant schema from the complete migration chain and compare it with model
   expectations.
7. Run focused backend contract, service, activation, lifecycle, worker, and migration tests.
8. With fresh approval, reset only the necessary disposable Dev automation data/schema and deploy
   the backend plus chat worker.
9. Verify the backend API and walking skeleton before changing the frontend.
10. Update the frontend to use the agent-level contract and delete its trigger delivery code.
11. Run frontend unit/E2E tests and then a Dev UI/API walking skeleton.

Production mutation, release-branch pushes, pipeline approval, schema reset, and live email sends
are separate operational approvals and are not authorized by this document.

## 12. Backend file change inventory

### 12.1 Agent definition ownership

| File | Required change |
| --- | --- |
| `src/domain/models/organisation_tables/agent_models.py` | Add current agent config/token and immutable run config; delete trigger delivery completely |
| `src/api/schemas/agent_models.py` | Add closed channel schemas, nested detail state, and update request/response |
| `src/service/agents/validator.py` | Normalize and reject unknown/duplicate delivery channels |
| `src/service/agents/access.py` | Add conditional delivery-only update scoped by organisation, owner, agent, and timestamp without revision increment |
| `src/service/agents/definition_service.py` | Default create/template copies to history-only and implement the owner-scoped delivery update/conflict path |
| `src/service/agents/serializers.py` | Include nested delivery state in `AgentDetail`; keep it out of executable snapshots |
| `src/api/routers/agents/definitions_handler.py` | Add the rate-limited PUT subresource with commit/rollback behavior |
| `src/api/schemas/agent_models.py` and `src/service/agents/capability_catalog.py` | Move delivery channel metadata to the agent capability catalog |
| `src/api/routers/agents/capabilities_handler.py` | Update the catalog description/response contract |

### 12.2 Trigger contract removal

| File | Required change |
| --- | --- |
| `src/api/schemas/automation_trigger_models.py` | Remove trigger delivery decisions, presentation fields, and trigger-catalog channels |
| `src/service/automation/triggers/catalog.py` | Stop returning output-delivery channels |
| `src/service/automation/triggers/validator.py` | Stop validating delivery as part of a trigger |
| `src/service/automation/triggers/service.py` | Delete delivery persistence and remove it from trigger hashing immediately |
| `src/service/automation/triggers/serializers.py` | Remove delivery from trigger detail/summary presentation |
| `src/service/automation/triggers/access.py` | Remove `delivery_config` from mutable trigger columns |
| `src/service/automation/triggers/activation.py` | Copy current agent delivery to the dedicated run column; remove delivery from trigger snapshots |
| `src/service/automation/triggers/activation_access.py` | Batch-load active agent IDs plus current delivery configs |

### 12.3 Run completion and delivery

| File | Required change |
| --- | --- |
| `src/service/automation/runs/lifecycle_service.py` | Read the dedicated run snapshot and create pending/skipped intents only after success |
| `src/service/automation/runs/access.py` | Derive/dedupe recipients; create the skipped policy row for an empty authorized audience; return only dispatchable IDs |
| `src/workers/chat_worker/services/agent_run_delivery_service.py` | Validate the dedicated run config instead of trigger snapshot; keep rendering/provider behavior unchanged |
| `src/service/automation/runs/read_service.py` | Expose audit decision where required and continue aggregating ledger outcomes |
| `src/api/schemas/agent_run_models.py` | Add the run-detail output-delivery snapshot contract |
| `src/infra/observability/metrics.py` | Rename/reclassify invalid-config metrics around the dedicated run snapshot, without adding IDs as labels |

`agent_run_execution_service.py`, `agent_run_tasks.py`, `delivery_access.py`, and the existing
notification ledger should require little or no implementation change. They still need regression
tests because they are part of the delivery path.

### 12.4 Migration and documentation

| File | Required change |
| --- | --- |
| `migrations/org_migrations/versions/3c7f79637711_088_agent_output_notifications.py` | Correct the development feature migration to add only agent/run config and retain notification fencing |
| `migrations/org_migrations/versions/3c7f79637721_078_agent_automation_tables.py` and `3c7f79637712_087_ai_native_automation.py` | Delete the trigger delivery column from both forward and downgrade table definitions so the clean development chain never creates it |
| `tests/migrations/org/test_088_agent_output_notifications.py` | Verify final columns/defaults/removal and notification-ledger behavior |
| `docs/API Documentation/agents/v1-agent-definitions-api.md` | Document detail state and PUT subresource |
| `docs/API Documentation/automation/v1-agent-triggers-api.md` | Remove trigger delivery contract/catalog/presentation |
| `docs/API Documentation/automation/v1-agent-runs-api.md` | Document immutable run delivery decision versus outcome |
| `docs/API Documentation/agents/v1-agent-frontend-integration-flow.md` | Move sequencing from trigger creation to agent detail configuration |
| `docs/superpowers/specs/2026-08-07-agent-output-transactional-email-notification.md` | Mark its trigger-owned sections superseded and link here; do not rewrite its valid worker/email guarantees |

### 12.5 Exhaustive stale-reference cleanup

The implementation is not complete until a repository search proves the following current
trigger-owned references are removed or moved:

| Current reference | Required disposition |
| --- | --- |
| `AutomationTrigger.delivery_config` and trigger-table schema assertions | Delete |
| `TriggerDeliveryDecision`, `TriggerDeliveryChannel`, and trigger request/detail fields | Delete; introduce agent-owned equivalents in `agent_models.py` |
| `ValidatedTriggerDefinition.delivery_config` | Delete |
| trigger catalog `delivery_channels` | Delete; move email metadata to agent capability catalog |
| trigger serializer delivery adapter and presentation helper | Delete |
| trigger service create/update/refresh/hash delivery arguments | Delete |
| trigger access mutable-column entry | Delete |
| activation `_trigger_snapshot(...)["delivery_config"]` | Delete; write `AgentRun.output_delivery_config` instead |
| lifecycle and delivery-worker reads from `run.trigger_snapshot` | Move to the dedicated run field |
| trigger tests in `test_catalog.py`, `test_validator.py`, `test_service.py`, and `test_serializers.py` | Delete trigger-delivery assertions; add agent/run assertions in the owning suites |
| run fixtures in `test_read_service.py`, `test_lifecycle_service.py`, execution-worker tests, delivery-worker tests, and output-delivery E2E | Move configuration from trigger snapshot to the run field |
| `tests/test_agent_automation_schema.py` and `tests/migrations/org/test_087_ai_native_automation.py` | Assert trigger-column absence and agent/run-column presence |
| `docs/API Documentation/automation/v1-agent-triggers-api.md` | Remove delivery from catalog, request, detail, presentation, examples, and polling explanation where trigger-owned wording exists |
| `docs/superpowers/plans/2026-07-12-agent-automation-tables.md` | Mark the trigger-delivery table decision superseded by this document |
| `docs/superpowers/plans/2026-08-08-agent-trigger-frontend-implementation.md` | Mark trigger delivery types/checklist superseded; later frontend plan must use the agent subresource |

Generic delivery aggregation in `read_service.py`, the reusable `NotificationDelivery` model,
delivery worker/recovery tasks, and their idempotency tests remain. Similarly named meeting-summary,
reminder, or WhatsApp delivery code is a different domain and must not be changed by a broad search
and replace.

## 13. Test plan

### 13.1 Agent contract and concurrency

- create and template-use default to empty external channels;
- agent detail returns normalized delivery state and timestamp;
- owner can enable and disable email;
- foreign owner and foreign organisation receive 404;
- archived agent receives 404;
- stale delivery timestamp receives 409;
- invalid/duplicate/arbitrary channel data receives 422 or the established domain error;
- delivery edit does not increment agent revision;
- delivery edit does not make triggers target-stale;
- ordinary agent replacement and capability replacement do not overwrite delivery.

Primary tests:

- `tests/api/routers/agents/test_agent_definition_contracts.py`
- `tests/api/routers/agents/test_agent_definitions_handler.py`
- `tests/service/agents/test_agent_definition_service.py`
- `tests/service/agents/test_agent_definition_access.py`
- `tests/service/agents/test_capability_catalog.py`

### 13.2 Trigger contract

- trigger create/update no longer accepts or returns `delivery`;
- catalog no longer contains `delivery_channels`;
- trigger cards/serializers no longer return delivery presentation;
- trigger duplicate/hash behavior excludes delivery completely;
- no stale delivery column, neutral salt, or runtime fallback remains;
- new direct schedule/manual/webhook events use the corrected hash.

Primary tests:

- `tests/api/routers/automation/test_trigger_contracts.py`
- `tests/service/automation/triggers/test_catalog.py`
- `tests/service/automation/triggers/test_validator.py`
- `tests/service/automation/triggers/test_service.py`
- `tests/service/automation/triggers/test_serializers.py`
- schedule/manual/webhook direct-route hash tests

### 13.3 Activation snapshot

- multiple matching triggers for the same agent use one batched current delivery lookup;
- email enabled after trigger creation affects the next run without trigger refresh;
- delivery change does not change the pinned executable agent revision;
- a run activated before disablement retains email in its snapshot;
- replay of the same trigger/event returns the existing run and does not reinterpret configuration;
- malformed persisted agent delivery fails closed and emits a safe activation outcome/log.

Primary tests:

- `tests/service/automation/triggers/test_activation_access.py`
- `tests/service/automation/triggers/test_activation_service.py`
- `tests/service/automation/triggers/test_runtime_mapper.py`

### 13.4 Completion, recipients, and worker

- history-only success creates no external delivery row;
- email-enabled personal success creates one row for the execution principal;
- organisation fan-out creates one row per deduplicated pinned audience user;
- empty organisation audience creates one skipped policy row and publishes no task;
- disabling delivery after activation does not suppress the run's pinned intent;
- enabling delivery after activation does not add an intent to that run;
- duplicate completion reuses rows;
- inactive/removed recipients skip without fallback;
- worker rejects a delivery whose channel is absent from the run snapshot;
- provider retry, uncertain outcome, manual repair, recovery, safe rendering, and idempotency remain
  green.

Primary tests:

- `tests/service/automation/runs/test_lifecycle_service.py`
- `tests/service/automation/runs/test_claim_access.py`
- `tests/service/automation/test_agent_output_delivery_e2e.py`
- `tests/workers/chat_worker/services/test_agent_run_execution_service.py`
- `tests/workers/chat_worker/services/test_agent_run_delivery_service.py`
- `tests/workers/chat_worker/tasks/test_agent_run_tasks.py`

### 13.5 Migration/schema construction

- a database built from zero has both agent-level columns and the run snapshot column;
- all new columns are non-null with canonical history-only defaults;
- `automation_triggers` has no `delivery_config` column;
- `notification_deliveries` still has claim, retry, provider, and recovery fields/indexes;
- migration upgrade/downgrade tests leave no orphan constraints or indexes;
- SQLModel metadata and the migrated schema agree;
- no test asserts or performs legacy delivery backfill.

Verification must inspect actual column nullability/defaults in an isolated tenant schema. A
source-text assertion alone is not sufficient evidence for this schema replacement.

## 14. Observability and operational checks

Keep existing delivery metrics and add only low-cardinality signals needed to prove the cutover:

- output-delivery configuration updates by result (`updated`, `conflict`, `invalid`);
- runs activated by channel (`history_only`, `email_notification`);
- skipped policy outcomes (`no_authorized_recipient`);
- schema-reset result and postcondition counts;
- any trigger-delivery runtime reference is a test/build failure, not a compatibility metric.

Logs may include organisation ID, agent ID, run ID, delivery ID, channel, safe status/error code,
and correlation ID. They must not contain model output, prompts, message bodies, credentials, or
full recipient addresses.

Existing alerts for oldest pending age, terminal failure rate, unknown provider outcomes, and
recovery backlog remain applicable.

## 15. Backend acceptance criteria

The backend phase is complete only when all of these are proven:

1. Agent detail exposes a separately versioned output-delivery decision.
2. The dedicated update changes delivery without incrementing agent revision.
3. Trigger create, update, detail, presentation, and catalog contain no public delivery decision.
4. Trigger-owned delivery is deleted from schema, code, API documentation, fixtures, and tests.
5. Every new run stores a dedicated immutable delivery snapshot from current agent state.
6. Fresh runs default safely and no existing run data is migrated into the new semantics.
7. Agent delivery edits affect future activations without trigger refresh.
8. History-only runs create no external pending intent.
9. Email-enabled successful runs create idempotent recipient rows from authorized run principals or
   audiences.
10. Empty authorized audiences produce a visible skipped outcome without fallback recipients.
11. Delivery retry/recovery never reruns the agent and never changes successful run status.
12. Trigger hashes exclude delivery and fresh direct schedule/manual/webhook events use the new
    canonical hash.
13. Migration postconditions pass on an isolated tenant schema.
14. Focused backend schema, service, activation, lifecycle, worker, and migration tests pass.
15. API documentation matches the implemented backend contract.
16. No frontend implementation, deployment, live email, or production mutation is claimed from
    backend unit/integration results alone.

## 16. Later frontend handoff

After the backend phase is implemented and tested:

- rename the agent detail section to **Output delivery** and add its `+` action;
- show **Run history** as always available and non-removable;
- add/remove channels from the agent-level subresource;
- show email recipient behavior as derived from each run's authority/audience;
- remove delivery UI, state, validation, reset behavior, and copy from the trigger builder;
- remove delivery summaries from trigger cards;
- update schemas, API fixtures, unit tests, and Agent V1 E2E tests;
- deploy only after the backend catalog and subresource are available.

The frontend must not emulate the new boundary by rewriting every trigger. It updates one
agent-level resource.
