# Agent Automation: Canonical Design

**Status:** Implemented in this worktree; live worker execution intentionally not run

**Scope:** User-owned agents, tools/skills, triggers, durable runs, and the first Celery worker

**Out of scope:** Workflow definitions/runtime, user-uploaded executable code, schedules, delivery channels, and organisation-wide triggers

This is the canonical architecture document for agent automation. The API documents remain the
HTTP contract and the files under `docs/superpowers/plans` remain implementation history. This
document replaces the four earlier agent/trigger design specs, which mixed superseded product
decisions with different implementation stages.

## Decision

Keep the existing three-table model:

1. `user_agents` stores a user's editable agent definition and selected code-owned capabilities.
2. `automation_triggers` stores a user's activation rule and a pinned agent snapshot.
3. `agent_runs` is both the durable job record and the execution audit.

Platform base agents are code-owned metadata in V1, not a fourth table. `Use Template` copies one
exact metadata version into `user_agents`; templates never participate directly in triggers or
runs.

V1 has two independent creation paths: a user can create a private agent directly, or copy a shared
platform template into a private agent. Template creation and publishing are separate future
features and are not prerequisites for creating or automating an agent.

Do not add a separate job table, tool table, skill table, service per trigger type, or dedicated
worker deployment yet. A pending `agent_runs` row already provides the durable boundary Celery
needs. Celery transports a run identifier; PostgreSQL owns execution truth.

## Product Boundary

- An agent answers **what should run**: prompt, model, tools, skills, limits, and output contract.
- A trigger answers **when it should run**: event filter, task input, and pinned target revision.
- An agent run answers **what happened**: immutable inputs, claim attempts, output, errors, cost,
  and timing.
- A reminder remains a static reminder. It no longer owns agent execution.
- A workflow will later be a separate target type. It can reuse `automation_triggers`, but it will
  have its own definition, version, and runtime tables/services.
- Every V1 agent and trigger is owned by one user. Sharing the same agent template does not share
  that user's triggers.
- Template metadata has stable IDs and versions. User copies retain them in the existing
  `source_agent_id` and `source_version` provenance fields.

## What Exists Today

The current worktree already contains:

- the three tables and stable relational constraints;
- user-scoped agent create/list/get/replace/archive APIs;
- trigger catalog and user-scoped trigger create/list/get/replace/refresh/archive APIs;
- CRM deal-stage matching that creates idempotent pending runs in the CRM transaction;
- pinned agent, trigger, and input snapshots on every run;
- fail-closed mapping from a persisted run to a runtime envelope;
- an atomic run claim service with a 30-minute stale-claim lease;
- capability catalog and atomic tool/skill set APIs;
- a snapshot-driven Agno runtime that loads only pinned tools and skills;
- fenced success, failure, validation-failure, and retry transitions;
- `chat.execute_agent_run` plus a 30-second durable recovery publisher;
- owner-scoped run list and detail APIs.
- code-owned template catalog/detail/use APIs and one read-only Deal Stage Health Review template.

The implemented path is:

`pending AgentRun -> recovery dispatch -> atomic claim -> dynamic agent execution -> fenced completion`

The two creation paths feeding the same execution model are:

```text
Create Agent ---------------------------> private UserAgent -> optional user trigger -> AgentRun
code metadata -> Use Template ----------> private UserAgent -> optional user trigger -> AgentRun
```

The next proof point is a live CRM deal-stage walking skeleton after the code is reviewed and the
database/worker environment is intentionally started.

## Capability Management

### Meaning of "add a tool or skill"

V1 users select capabilities already shipped with the application. They do not upload Python,
scripts, packages, credentials, or arbitrary executable skills. Tool authorization and tenant
access checks still happen at execution time; storing a tool key is an allowlist, not an
authorization grant.

The code remains the source of truth:

- tools come from `ToolName` and the existing tool registry;
- skills are discoverable local skill directories containing `SKILL.md`;
- the API catalog adds presentation and compatibility metadata around those keys.

No capability database tables are needed until capabilities become tenant-installed or carry
tenant-specific configuration.

### Capability APIs

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/users/v1/agent-capability-catalog` | List selectable tool and skill metadata |
| `PUT` | `/users/v1/agents/{agent_id}/capabilities` | Atomically replace the selected tool and skill sets |

The focused update request is:

```json
{
  "expected_revision": 4,
  "allowed_tools": ["search_deals", "get_deal_details"],
  "allowed_skills": ["pipeline-review"]
}
```

The server validates the complete desired set, updates it using optimistic concurrency, increments
the agent revision once, and returns the full agent definition. One atomic set-replacement endpoint
is safer than separate add/remove endpoints: repeated requests are idempotent, the UI can save its
actual state, and concurrent editors cannot silently lose changes.

The existing full `PUT /users/v1/agents/{agent_id}` remains for the full editor. Both paths call the
same validator and revision-aware update operation.

Catalog items expose only metadata the editor currently uses:

- stable key;
- display label;
- category (`skill` for skills).

Add integration availability, side-effect labels, or runtime compatibility only when the editor
enforces those fields; speculative metadata would drift from the executable registry.

Changing an agent creates a new revision. Existing triggers continue using their pinned snapshot
until the user calls the existing `refresh-target` endpoint. This prevents an innocent capability
edit from silently changing live automation.

## Run Is the Job

`agent_runs` already stores the required V1 execution metadata:

- ownership and target: organisation, user, agent, trigger, and agent revision;
- identity and deduplication: run ID, idempotency key, source domain/type/event ID, and schedule;
- immutable execution input: agent, trigger, and input snapshots;
- lifecycle: status, attempt count, claim time/worker, and created/started/completed times;
- result: output text/JSON, error code/message, model, token usage, cost, and latency.

Do not add a generic `metadata` JSON column or a second Celery-job table before a concrete field is
missing. Delivery receipts can get a dedicated shape when delivery is implemented.

### State machine

```text
pending ----claim----> running ----complete----> succeeded
   ^                     |  \------------------> validation_failed
   |                     |  \------------------> failed
   |                     |  \------------------> delivery_failed (future)
   |                     \---------------------> cancelled
   \-------retryable failure / expired lease----
```

Each successful claim increments `attempt_count`. That attempt number is the claim's fencing token.
Completion must update only when all of these still match:

- organisation and run ID;
- status is `running`;
- `attempt_count` equals the claimed attempt;
- optionally `claimed_by` equals the worker ID.

This prevents a timed-out worker from overwriting a newer attempt's result. Celery delivery
identity alone is not sufficient because late acknowledgement and redelivery are expected.

## Celery Worker

### Initial deployment choice

Reuse the existing chat worker and `chat_messages` queue for the first walking skeleton. Add one
thin task, for example `chat.execute_agent_run`, with late acknowledgement and bounded retries.
Its payload contains only:

```json
{
  "organisation_id": "...",
  "run_id": "..."
}
```

Do not send prompts, tools, credentials, or snapshots through Redis. The worker loads the durable
row from the tenant database. Move agent automation to a separate queue/worker only after measured
load shows that automation harms interactive chat latency or needs different scaling/isolation.

### Execution flow

1. The trigger activation service creates or finds an idempotent pending `AgentRun` inside the
   business transaction.
2. The recovery publisher scans committed pending rows and publishes organisation and run IDs.
3. The execution task opens the tenant session and atomically claims the run.
4. The task maps the pinned snapshots to a runtime envelope, commits the claim, and releases the
   database session before any LLM or external-tool work.
5. A dynamic runtime builder creates the agent from the envelope:
   - resolve only `allowed_tools` through the existing tool registry;
   - load only `allowed_skills` rather than exposing the complete skills directory;
   - use the pinned prompt, model, model configuration, limits, and output contract;
   - pass the existing tenant/user `AgentContext` so tools retain their normal authorization.
6. The agent executes without a long-lived database transaction.
7. The task reacquires the tenant session and performs a fenced success or failure transition using
   the claimed attempt number.
8. The task commits, logs structured run/agent/trigger/attempt identifiers, and returns.

The current `GeneralAgent` cannot be called unchanged because it hardcodes the general prompt/tool
set and exposes all local skills. Reuse its proven context, model/result handling, and tool
implementations, but build the runtime from the persisted envelope.

### Dispatch durability

Publishing before the business commit risks running work that later rolls back. Publishing only
after commit has a smaller crash window: the row can commit and the process can die before publish.

Use the `AgentRun` row as a transactional outbox:

- a periodic recovery task scans eligible pending runs and republishes their IDs;
- atomic claim plus idempotency makes duplicate Celery messages harmless;
- the recovery scanner pages/batches and fans out only through the repository's approved tenant
  enumeration pattern.

This closes the commit/publish gap without another table or broker transaction. The first scanner
can be deliberately simple; add stronger scheduling or partitioning only when run volume proves it
necessary. A direct after-commit publish may later reduce latency, but it is an optimization rather
than a correctness requirement.

### Failure and retry policy

- Duplicate delivery or a currently leased run: return successfully without execution.
- Invalid/corrupt pinned snapshot: fenced transition to `validation_failed`; do not retry.
- Retryable provider/network failure: fenced release back to `pending`, record the last error, then
  ask Celery to retry with exponential backoff and jitter.
- Non-retryable execution failure or exhausted attempts: fenced transition to `failed`.
- Soft time limit: record/release if possible, then retry within the attempt limit.
- Hard process death: the stale-claim lease allows a later delivery to reclaim the run.

The task must never hold the claim transaction open while calling the model or a tool. Agent tools
that need the database continue opening their own scoped sessions through the existing context.

## Run APIs

The first operator/user surface should be read-only:

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/users/v1/agent-runs` | Cursor-page owned runs; filter by agent, trigger, and status |
| `GET` | `/users/v1/agent-runs/{run_id}` | Return snapshots, output, errors, usage, and timing for one owned run |

The list response should be small; full snapshots and large output belong in detail. Retry, cancel,
manual run, and delivery endpoints are deferred until their semantics are required. Internal
dispatch is not a public endpoint.

## Trigger Scaling Pattern

Hundreds of trigger types do not mean hundreds of services or tasks. Use the existing reference
pattern:

- a catalog declares trigger keys and their configuration schema;
- a validator checks common fields, then delegates only genuinely domain-specific rules;
- a matcher/activation adapter converts one source event into zero or more pending runs;
- every match converges on the same run creation and Celery execution path.

New trigger code should normally be one catalog entry, one validator/matcher branch or registered
handler, and focused tests. The Celery worker remains unaware of the trigger type because it
executes the pinned run contract.

## API and Transaction Ownership

- Routers own HTTP concerns and commit/rollback.
- Services validate and flush; they do not commit.
- Access modules own SQL and tenant/owner predicates.
- Celery tasks are thin orchestration boundaries; lifecycle and runtime logic remain testable
  services.
- All public reads and writes are organisation- and owner-scoped.
- Secrets are referenced/resolved at runtime and are never copied into snapshots or Celery payloads.

## Implementation Sequence

The implemented end-to-end sequence is:

1. Added the capability catalog and atomic capability-set endpoint using the existing agent validator.
2. Added fenced run completion/retry lifecycle operations and included the claim attempt in the runtime
   envelope.
3. Added the dynamic runtime builder and one `execute_agent_run` Celery task in the existing worker.
4. Added the post-commit pending-run recovery publisher.
5. Added run list/detail APIs so users can inspect execution and stored metadata.
6. Next: run one CRM deal-stage walking skeleton from event through stored result before adding another
   trigger type.
7. Added code-owned agent-template metadata and materialisation without adding template tables.

Schedules, delivery channels, workflows, organisation-wide triggers, custom executable
capabilities, and a dedicated automation worker remain explicit follow-up decisions.

## Acceptance Criteria for the First Walking Skeleton

- A user's active CRM deal-stage trigger creates exactly one pending run per source event.
- Only the trigger's pinned agent revision, tools, and skills are executed.
- The business transaction never publishes before commit.
- A committed-but-not-published run is eventually redispatched.
- Duplicate Celery deliveries cannot execute a currently leased run.
- A late worker cannot overwrite the result of a newer attempt.
- No database transaction remains open during model/tool execution.
- Success and failure metadata are stored on `agent_runs` and visible through owner-scoped APIs.
- Existing reminder execution is not used for the new path.

## Explicit Non-Decisions

The first release does not promise:

- user-authored Python, scripts, or uploaded skills;
- exactly-once model/tool side effects (the system provides idempotent job state and fenced writes,
  while individual external side effects still need their own idempotency keys);
- workflow execution;
- arbitrary cron/schedule execution;
- cross-user or organisation-admin triggers;
- a separate automation queue, worker deployment, or generic plugin marketplace.
- database-backed template administration, community publishing, or automatic template upgrades.

These are extensions of a working run pipeline, not prerequisites for proving it.
