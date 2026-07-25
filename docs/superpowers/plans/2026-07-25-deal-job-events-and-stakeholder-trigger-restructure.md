# Deal job events and stakeholder trigger contract restructure

## Status

Reviewed, corrected, implemented, and validated on 2026-07-25.

## Implementation result

- `StakeholderChangeType` now lives in
  `src/service/crm/participants/contracts.py`.
- `DealJobRedisPublisher` now lives in
  `src/service/crm/jobs/deal_job_event_publisher.py`.
- All repository consumers use the new paths and both old worker files are
  deleted.
- Focused regression validation passed: 152 tests, 24 runtime module imports,
  Ruff, Python compilation, comment checks on the two owned modules,
  trailing-whitespace checks, and `git diff --check`.
- A wider consumer run also exposed three pre-existing meeting-test drift
  failures: the tests still patch `get_organisation_name`, while the current
  services use `get_organisation_agent_context`, and one meeting-prep fake lacks
  the `execute` method that the current helper requires. The move changes only
  the publisher import in those modules, so meeting behavior and those stale
  tests remain outside this plan.

## Scope

This plan covers exactly two ownership moves:

1. `src/workers/deal_worker/services/deal_change_detector.py`;
2. `src/workers/deal_worker/services/deal_job_publisher.py`.

It also covers the import-only consumer edits, focused tests, and documentation
updates required to make those two moves complete.

It does not restructure assets, meeting services, transcript services, deal
insights, contact research, account research, claim services, stale-job cleanup,
Celery tasks, or any other worker service. Some of those files import the
publisher and therefore need a one-line import update; their responsibilities
and behavior remain out of scope.

## Decision

Move the stakeholder synchronization trigger enum to the participant domain and
move the deal-job Redis publisher beside the existing deal-job service and SSE
reader.

Keep all public values, frame payloads, Redis stream keys, call order, and
runtime behavior unchanged.

The target dependency direction is:

```text
API / agent tool / worker task
              |
              +--> crm/participants/contracts.py
              |
              +--> crm/jobs/deal_job_event_publisher.py
                              |
                              v
                       Redis Stream
                              |
                              v
                       DealJobSSEService
                              |
                              v
                           frontend
```

Neither shared contract should require an API, agent, or CRM service to import
from `src.workers`.

## What the two files currently do

### `deal_change_detector.py`

Despite its name, this file does not detect changes. It contains only
`StakeholderChangeType`, a string enum:

```text
NEW_DEAL
PARTICIPANT_ADDED
PARTICIPANT_UPDATED
PARTICIPANT_REMOVED
MANUAL_REFRESH
```

Producers put one of these values into the existing stakeholder-sync task
payload. The task uses the value to decide whether communication hydration is
required.

This is a participant/stakeholder contract, not worker implementation.

### `deal_job_publisher.py`

`DealJobRedisPublisher` writes server-side job frames into the Redis Stream
identified by organisation and job ID.

It adds the existing metadata:

- `sequence`;
- `timestamp`;
- `job_type`;
- `job_id`.

It publishes the existing frame types:

- `progress.update`;
- `answer.replace`;
- `error`;
- `finalize`;
- `import.deal.started`;
- `import.deal.progress`;
- `import.deal.completed`;
- `import.deal.failed`;
- `import.summary.tick`.

The existing `DealJobSSEService` reads the same Redis Stream and sends those
frames to the frontend. The publisher is therefore the write side of the
deal-job event boundary, not deal-worker business logic. It is an application
service over the existing `src.infra.messaging` Redis primitives; the move does
not create a second transport abstraction.

## Why the move is justified

The current enum forces API routers and an agent tool to import a Celery worker
package merely to obtain string constants.

Current non-worker consumers include:

- `src/api/routers/crm/contacts_handler.py`;
- `src/api/routers/crm/deal_participants_handler.py`;
- `src/agents/tools/crm/create_contact.py`.

The current publisher is also referenced as a type by agent workflows. Those
agents should depend on a shared job-event service, not a specific worker
package.

The destination packages already exist and own the relevant concepts:

- `src/service/crm/participants` owns stakeholder and participant contracts;
- `src/service/crm/jobs` owns durable deal jobs and the SSE reader.

No new abstraction layer is needed.

## Target folder structure

```text
src/
├── service/
│   └── crm/
│       ├── participants/
│       │   └── contracts.py
│       │
│       └── jobs/
│           ├── __init__.py
│           ├── deal_job_service.py
│           ├── deal_job_event_publisher.py
│           ├── deal_job_sse_service.py
│           └── deal_meeting_job_service.py
│
└── workers/
    └── deal_worker/
        ├── services/
        │   └── ... all unrelated worker services remain unchanged
        └── tasks/
            └── ... all existing task files remain unchanged
```

## Exact file changes

### Stakeholder trigger contract

Move the existing `StakeholderChangeType` definition into:

```text
src/service/crm/participants/contracts.py
```

Then delete:

```text
src/workers/deal_worker/services/deal_change_detector.py
```

Keep the enum class name and all five string values unchanged. Do not rename the
task payload field `change_type`.

Update these production imports:

- `src/api/routers/crm/contacts_handler.py`;
- `src/api/routers/crm/deal_participants_handler.py`;
- `src/agents/tools/crm/create_contact.py`;
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py`.

### Deal-job event publisher

Move:

```text
src/workers/deal_worker/services/deal_job_publisher.py
```

to:

```text
src/service/crm/jobs/deal_job_event_publisher.py
```

Keep the class name `DealJobRedisPublisher`. Renaming it would touch every
consumer without improving the ownership boundary.

Update these runtime consumers:

- `src/workers/deal_worker/tasks/deal_mappers/visible_job_tracking.py`;
- `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py`;
- `src/workers/deal_worker/tasks/deal_insight_tasks/deal_summary_task.py`;
- `src/workers/deal_worker/tasks/deal_insight_tasks/deal_update_task.py`;
- `src/workers/deal_worker/tasks/meeting_insight_tasks/meeting_prep_task.py`;
- `src/workers/deal_worker/tasks/meeting_insight_tasks/meeting_summary_task.py`;
- `src/workers/deal_worker/tasks/meeting_insight_tasks/transcript_upload_task.py`;
- `src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py`;
- `src/workers/deal_worker/services/deal_insight_services/deal_summary_service.py`;
- `src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py`;
- `src/workers/deal_worker/services/meeting_insight_services/meeting_prep_service.py`;
- `src/workers/deal_worker/services/meeting_insight_services/meeting_summary_service.py`;
- `src/workers/deal_worker/services/meeting_insight_services/transcript_upload_service.py`.

Update these type-checking-only consumers:

- `src/agents/workflows/briefing/agent.py`;
- `src/agents/workflows/deal_summary/agent.py`;
- `src/agents/workflows/deal_tasks/agent.py`;
- `src/agents/workflows/risk_blocker_extraction/agent.py`;
- `src/agents/workflows/stakeholder_analysis/agent.py`;
- `src/agents/workflows/summary/agent.py`.

Also update:

- `src/workers/deal_worker/README.md`;
- `docs/Technical Documentation/deal/deal-summary-tasks.md`;
- tests that patch `DealJobRedisPublisher` through a consumer module.

Delete the old worker publisher only after repository searches find no old
imports.

Do not add compatibility import wrappers. All consumers are internal and can be
updated in one atomic commit.

## Responsibility boundaries after the move

### `StakeholderChangeType`

Owns only the stable reason values passed between producers and the stakeholder
task.

It does not:

- detect database changes;
- publish tasks;
- decide stakeholder eligibility;
- perform hydration;
- create frontend events.

The existing task remains responsible for interpreting the value.

### `DealJobRedisPublisher`

Owns only synchronous writes of existing deal-job frames to the existing Redis
Stream.

It continues to:

- derive the stream key with `channel_for_deal_job`;
- assign monotonically increasing in-process sequence numbers;
- add timestamp, job type, and job ID;
- apply the existing Redis stream maximum length;
- log when `xadd_json_frame` reports failure;
- provide the existing convenience methods for progress, result, error,
  finalize, and deal-import frames.

It does not:

- create or update `DealJobs` rows;
- decide when a job starts, completes, or fails;
- own Celery retries;
- expose the SSE HTTP endpoint;
- perform frontend authorization;
- persist a second event history.

`DealJobService` remains the durable job creation/publication boundary.
`DealJobSSEService` remains the authorized event reader. Worker trackers and
tasks decide when to invoke the publisher.

## Runtime behavior after the move

### Stakeholder change

```text
participant added/removed through API or agent
              |
              v
StakeholderChangeType value added to existing task payload
              |
              v
existing stakeholder Celery task
              |
              v
existing synchronization and hydration behavior
```

Only the Python import path changes.

### Deal job progress

```text
worker task or workflow reaches a progress boundary
              |
              v
DealJobRedisPublisher writes the same frame to the same Redis Stream
              |
              v
DealJobSSEService reads and authorizes the same stream
              |
              v
frontend receives the same SSE frame
```

There is no frontend or protocol change.

## Important compatibility rules

- Preserve every `StakeholderChangeType.value` exactly.
- Preserve the `change_type` task argument and payload shape.
- Preserve `DealJobRedisPublisher` constructor arguments.
- Preserve all publisher method signatures.
- Preserve all frame `type` strings.
- Preserve metadata fields and value types.
- Preserve sequence numbering beginning at zero per publisher instance.
- Preserve the Redis channel naming function.
- Preserve the default stream `maxlen=100`.
- Preserve current best-effort failure behavior in this relocation.
- Preserve the structured warning event name and fields. The Python logger name
  changes with the module path and is not a stable event contract.
- Preserve existing Celery task names, queues, routing keys, and retry rules.
- Preserve existing SSE routes and reconnect behavior.

Changing delivery guarantees, introducing an outbox, making Redis publication
asynchronous, or consolidating chat and deal frame contracts are separate
designs and are not part of this move.

## Implementation sequence

### Step 0: add publisher characterization tests

There is currently no direct test for `DealJobRedisPublisher`.

Add, following the existing flat CRM job-test layout:

```text
tests/service/crm/test_deal_job_event_publisher.py
```

Before moving the implementation, verify:

- the stream key comes from organisation and job ID;
- the first frame has sequence `0` and the second has sequence `1`;
- timestamp, job type, and job ID are added;
- the configured `maxlen` reaches `xadd_json_frame`;
- each convenience method produces its current frame shape;
- a `None` Redis result logs a warning without raising.

These tests characterize the current contract; they do not redesign it.

Also add a focused participant-contract test that locks all enum names and
values before deleting the old module:

```text
tests/service/crm/participants/test_contracts.py
```

### Step 1: move the stakeholder enum

- add `StakeholderChangeType` to participant contracts;
- update the four production imports;
- run participant router, agent-tool, and stakeholder-task tests;
- delete `deal_change_detector.py`;
- verify the old path has no references.

### Step 2: move the publisher

- move the publisher into `src/service/crm/jobs`;
- update all production and test imports;
- keep its class and constants unchanged;
- update documentation paths;
- delete the old publisher;
- run publisher, visible-job, deal-insight, and meeting-insight task tests.

### Step 3: structural verification

Run repository searches:

```text
rg "workers\.deal_worker\.services\.deal_change_detector" src tests docs \
  --glob "!docs/superpowers/plans/2026-07-25-deal-job-events-and-stakeholder-trigger-restructure.md"
rg "workers\.deal_worker\.services\.deal_job_publisher" src tests docs \
  --glob "!docs/superpowers/plans/2026-07-25-deal-job-events-and-stakeholder-trigger-restructure.md"
rg "from src\.workers\.deal_worker\.services" src/api src/agents
```

The first two searches exclude this historical plan and must return no matches.
The third must return no matches caused by these two moved files; unrelated
future service moves remain outside this plan.

## Required tests

### Stakeholder contract

- all five enum values remain exact;
- participant add still publishes `PARTICIPANT_ADDED`;
- participant removal still publishes `PARTICIPANT_REMOVED`;
- contact-to-deal linking still publishes `PARTICIPANT_ADDED`;
- agent contact creation with a deal link still publishes
  `PARTICIPANT_ADDED`;
- stakeholder task hydration decisions remain unchanged.

### Job event publisher

- direct publisher characterization tests pass from the new service path;
- `VisibleDealJobTracker` emits the same queued, active, complete, error, and
  finalize frames;
- meeting and insight tasks can construct the moved publisher;
- existing mocks patch the consumer modules successfully;
- completed and failed job reconnection behavior in `DealJobSSEService` remains
  unchanged.

### General checks

- focused tests pass;
- changed Python files pass Ruff;
- changed Python files compile;
- `git diff --check` passes;
- old import-path searches return no matches.

## Acceptance criteria

This plan is implemented only when:

- `StakeholderChangeType` lives in participant contracts;
- `DealJobRedisPublisher` lives in CRM job services;
- the two old worker files are deleted;
- APIs and agents no longer import either support type from the deal worker;
- enum values and task payloads are unchanged;
- Redis stream keys, frame shapes, sequencing, and SSE behavior are unchanged;
- no compatibility wrappers or new abstractions are added;
- the focused verification suite passes.

## Deployment and rollback

This is one atomic source change, but it does not require a simultaneous runtime
cutover. Producers serialize `StakeholderChangeType.value` as a plain string;
they do not enqueue the enum object. Publishers serialize plain JSON frames;
they do not send publisher instances or Python module paths. Old and new API and
worker images can therefore overlap during a normal rolling deployment.

No database migration, broker purge, Redis migration, frontend deployment, or
task-name transition is required.

Rollback is a code rollback. Existing `DealJobs` rows and Redis Stream entries
remain compatible because their schemas and identifiers do not change.

## Explicit non-goals

- no asset service changes;
- no meeting or transcript service moves;
- no deal insight service moves;
- no contact or account research service moves;
- no claim or stale-job cleanup changes;
- no worker or queue changes;
- no task payload changes;
- no frontend changes;
- no SSE route or frame redesign;
- no generic event bus;
- no outbox;
- no shared base publisher;
- no consolidation with chat streaming;
- no renaming of `DealJobRedisPublisher`;
- no cleanup of unrelated worker imports.
