# AI-Native Triggers: Final Design

**Status:** Final implementation contract; documentation only

**Read time:** 14–16 minutes

**Decision date:** 2026-08-05

## Outcome

Agent Studio will use one backend-owned trigger registry, one immutable
AutomationEventEnvelope, and one typed matcher for every source. CRM, email, meetings, persisted AI
signals, manual actions, schedules, and authenticated webhooks all enter the same durable
dispatch-to-run path.

The core rule is:

    durable fact
      → canonical event envelope in the existing transactional outbox
      → active trigger candidates
      → watched-field and typed-filter match
      → idempotent agent run using the pinned agent snapshot

The redesign keeps the parts that already work: organization scoping, post-commit delivery,
replay-safe dispatch rows, pinned agent revisions, and idempotent agent runs. It replaces the
hard-coded CRM catalog, per-event validator classes, and CRM-only activation branches.

Agent Studio is dev-only and its trigger data is disposable. Implement this as a clean contract
replacement and recreate dev trigger rows. Do not add backward-compatibility adapters or maintain
two trigger formats.

## Vocabulary and ownership

| Term | Meaning | Owner |
| --- | --- | --- |
| Event key | A durable producer fact, such as crm.deal.updated or meeting.transcript.ready | Backend producer registry |
| Trigger key | A user-selectable definition or preset; it subscribes to one or more event keys | Backend trigger registry |
| Binding | One saved trigger configured for an agent | Trigger service |
| Envelope | Immutable event-time input shared by all sources | Producer and event service |
| Watch | Cheap update gate: did any/all selected fields actually change? | Generic matcher |
| Filter | Typed predicate over approved envelope paths | Generic matcher |
| Principal | Identity and credentials used when the agent runs | Trigger binding and access layer |
| Subject scope | Which business records the principal may trigger on | Trigger binding and matcher |
| Audience | Who can see results or receive delivery | Run/delivery layer |

Event keys describe facts. Trigger keys describe user intent. For example,
crm.deal.amount_changed is a trigger preset, not a second event emitted alongside
crm.deal.updated.

## One event model for every source

The following Pydantic-shaped contract is normative. The notation is compact; implementation may
split the nested objects into normal Pydantic models.

    AutomationEventEnvelope
      schema_version: 1
      event_id: UUID
      organisation_id: UUID
      event_key: string
      route:
        mode: "match" | "direct"
        trigger_id: UUID | null
        config_hash: string | null
      occurred_at: UTC datetime
      source:
        domain: string
        system: string
        operation: "create" | "update" | "delete" | "signal" |
                   "invoke" | "schedule" | "webhook"
        mode: "interactive" | "incremental_sync" | "initial_sync" |
              "backfill" | "system"
        source_event_id: string
      actor:
        type: "user" | "integration" | "agent" | "system" | "external"
        id: string | null
      subject:
        type: string | null
        id: string | null
      record: map[string, JsonValue]
      changes: map[string, {before: JsonValue, after: JsonValue}]
      attributes: map[string, JsonValue]
      references: map[string, JsonValue]
      correlation_id: string | null
      caused_by_run_id: UUID | null
      causation_depth: integer >= 0
      idempotency_key: string

JsonValue is strict JSON: null, boolean, integer, finite number, string, an array of JsonValue, or
an object of JsonValue. Domain values use the canonical encodings below; arbitrary Python objects
are rejected by the existing JSON-safe boundary.

event_id is the server-generated dispatch identity. source.source_event_id identifies the durable
domain occurrence; clients and external webhook bodies never choose either tenant or event_id.

Envelope sections have distinct jobs:

- record is the safe event-time snapshot used for current-value filters. It is the post-write
  snapshot for create/update and the pre-delete snapshot for delete.
- changes contains only fields that changed in this single update. Both before and after are
  always present, including when either value is null.
- attributes contains stable facts specific to the event, such as AI-signal severity or email
  direction.
- references contains identifiers for rich or sensitive content. Full email bodies, transcripts,
  credentials, tokens, and large evidence blobs are never copied into the envelope.
- subject identifies zero or one primary business object. Global invocations have null type/id;
  otherwise both are required. A bulk manual action or record-scoped schedule fans out to one
  event and one run per subject. This keeps access, retry, output, and idempotency isolated.

Route validation is strict:

- match forbids trigger_id and config_hash. It looks up active bindings by organization and
  event_key.
- direct requires both trigger_id and config_hash. Manual, schedule, and webhook sources already
  know the binding they invoke.

Every producer must also satisfy these invariants:

1. Write the envelope into the generalized automation_event_dispatches row in the same
   transaction as the durable fact, or at the first durable boundary when the upstream system owns
   the transaction.
2. Derive organisation_id from authenticated or tenant-scoped state, never from an untrusted
   payload.
3. Make idempotency_key unique inside the organization. A retry returns the existing dispatch.
4. Emit one event for one business fact and at most one subject. One multi-field update is one
   event, not one event per changed field.
5. Keep the envelope replay-stable and small. Enforce a 64 KiB serialized limit and load rich
   context later through tenant-scoped tools.
6. Reject unknown event keys and envelope fields that do not validate against that event's
   registry contract.

There is no upserted event. A producer that performs an upsert knows whether it inserted or changed
the record and emits created or updated. A UI option named “created or updated” is a trigger
definition subscribing to both keys.

## Record lifecycle semantics

All mutable record-like resources use the same three base facts:

| Fact | record | changes |
| --- | --- | --- |
| resource.created | Safe post-create snapshot | Empty |
| resource.updated | Safe post-update snapshot | Exact before/after values for changed fields |
| resource.deleted | Safe pre-delete snapshot | Empty |

Semantic facts that cannot be represented by a record mutation remain first-class event keys:
meeting.transcript.ready, crm.task.overdue, ai.deal_risk.detected, and
agent.approval.granted are examples.

### Deal created

The producer emits crm.deal.created once after the deal and its mutation ledger entry are durable.
The record contains all catalog-approved filter fields, including standard and custom properties.

The envelope below is abridged; mandatory identity, source, actor, and causation fields are omitted
only for readability.

    {
      "event_key": "crm.deal.created",
      "route": {"mode": "match", "trigger_id": null, "config_hash": null},
      "subject": {"type": "deal", "id": "deal-uuid"},
      "record": {
        "id": "deal-uuid",
        "name": "Acme expansion",
        "pipeline_id": "pipeline-uuid",
        "stage_id": "stage-uuid",
        "deal_status": "open",
        "amount": "125000.00",
        "currency": "USD",
        "owner_id": "user-uuid",
        "created_by_user_id": "user-uuid",
        "expected_close_date": "2026-09-30T00:00:00Z",
        "custom_properties": {"region": "APAC"}
      },
      "changes": {}
    }

A trigger for “new APAC deal worth at least 100,000” subscribes to crm.deal.created and filters
record.amount plus record.custom_properties.region. No special producer is required.

### Deal updated

The existing deal write service already computes the exact changed fields and suppresses no-op
updates. Adapt that mutation event once; this envelope is also abridged:

    {
      "event_key": "crm.deal.updated",
      "subject": {"type": "deal", "id": "deal-uuid"},
      "record": {
        "id": "deal-uuid",
        "pipeline_id": "pipeline-uuid",
        "stage_id": "proposal-stage-uuid",
        "deal_status": "open",
        "amount": "125000.00",
        "owner_id": "user-uuid",
        "created_by_user_id": "user-uuid"
      },
      "changes": {
        "stage_id": {
          "before": "qualification-stage-uuid",
          "after": "proposal-stage-uuid"
        },
        "amount": {"before": "90000.00", "after": "125000.00"}
      }
    }

The CRM adapter normalizes every mutation of an existing deal—including current updated,
stage_changed, owner_changed, account_changed, primary_contact_changed, closed, and reopened
operations—to crm.deal.updated. It dispatches the mutation's full changed_fields once. The current
semantic_event_types list may remain for audit or non-automation callers after those callers are
checked, but it is no longer automation fanout.

The user-facing presets compile as follows:

| User choice | Event keys | Required watch | Fixed/user filter |
| --- | --- | --- | --- |
| Deal updated | crm.deal.updated | User-selected fields or none | Optional current/before/after values |
| Stage changed | crm.deal.updated | stage_id | Optional changes.stage_id.before/after |
| Entered stage X | crm.deal.updated | stage_id | changes.stage_id.after equals X |
| Deal won/lost/reopened | crm.deal.updated | deal_status | changes.deal_status.after equals the selected value |
| Amount changed | crm.deal.updated | amount | Optional before/after numeric predicates |
| Amount crossed 100,000 | crm.deal.updated | amount | changes.amount.before less than 100000 AND after at least 100000 |
| Close date changed | crm.deal.updated | expected_close_date | Optional before/after date predicates |
| Owner/account/contact changed | crm.deal.updated | the relevant ID field | Optional destination ID |
| Created or updated | crm.deal.created and crm.deal.updated | Optional on updates | record-value filters |

One update that changes both stage and amount still produces one envelope. One saved binding
creates at most one run for that envelope even when several of its conditions match.

## Typed values, watches, and filters

The backend CRM schema service remains the source for standard/custom deal fields, types, option
IDs, and display metadata. The trigger registry exposes only safe paths supported by both a
producer and the matcher.

| Domain value | Envelope encoding | Allowed operators |
| --- | --- | --- |
| Text, URL, email | String | eq, neq, in, not_in, contains, starts_with, is_empty, not_empty |
| UUID, relation, enum/select | Stable string ID/key | eq, neq, in, not_in, is_empty, not_empty |
| Integer | JSON integer | eq, neq, gt, gte, lt, lte, between, is_empty, not_empty |
| Number, currency, percentage | Decimal string | eq, neq, gt, gte, lt, lte, between, is_empty, not_empty |
| Boolean | JSON boolean | eq, neq |
| Date | ISO YYYY-MM-DD string | eq, before, on_or_before, after, on_or_after, between, is_empty |
| Datetime | UTC RFC 3339 string | eq, before, after, between, is_empty |
| Multi-select/list | Array of canonical scalar values | contains, contains_any, contains_all, is_empty, not_empty |

Currency comparisons parse the decimal string as Decimal; they never use binary floating point.
Currency code is a separate path when a comparison requires it. Datetimes compare as UTC instants.
Enum and relation filters persist stable IDs, not mutable labels. Option IDs are checked against
the current organization at save and activation. Text matching is Unicode case-insensitive unless
the catalog marks a field as exact; email addresses and domains are normalized to lowercase.
between is inclusive and always takes a typed two-value array. Formula custom fields use their
declared evaluated type; an untyped formula is not filterable.

A saved matcher has a watch and one filter tree:

    {
      "watch": {"mode": "any", "paths": ["amount", "stage_id"]},
      "filter": {
        "all": [
          {"path": "changes.amount.after", "operator": "gte", "value": "100000.00"},
          {"path": "record.stage_id", "operator": "in", "value": ["stage-uuid"]},
          {
            "any": [
              {"path": "record.owner_id", "operator": "eq", "value": "user-uuid"},
              {"path": "record.custom_properties.region", "operator": "eq", "value": "APAC"}
            ]
          }
        ]
      }
    }

Rules:

- Watch paths are record field names and apply only to updated events. Mode is any or all.
- Filter paths begin with record, changes, attributes, references, actor, or source and must appear
  in the selected trigger definition's path catalog.
- Paths are opaque registry IDs even when they use dotted display notation. Clients do not invent
  or split them; the matcher resolves the exact registered path, including custom-field keys.
- The trigger preset's fixed watch/filter is ANDed with the user's filter and cannot be removed by
  the client.
- Trees support all/any, at most two group levels, and at most 20 leaf conditions.
- No regex, JSONPath, executable code, database query, or prompt-based predicate is supported.
- A missing path is not null and never matches, including is_empty. For a known optional record
  field the producer includes the path with null. is_empty means null, empty string, or empty list.
- Unknown paths/operators, invalid values, or a producer/schema mismatch fail closed. Saving is
  rejected; an already-active binding is paused with a visible reason rather than becoming broad.
- Custom-field deletion or incompatible type changes pause affected bindings. A label-only change
  does not, because paths use stable keys.
- In a created-or-updated definition, watches gate update events only; a create event proceeds to
  the current-value filter.
- Matching is pure over the envelope. It performs no LLM call and loads no live business record,
  so retry uses the original event-time truth.

## Trigger registry and saved binding

The backend registry is ordinary typed Python data, not a plugin framework or database-managed
rules engine. It has two maps so shared event contracts are not copied into every preset:

- Event definitions, keyed by event_key, own the producer envelope schema, subject requirement,
  event-time field schema, allowed source modes, and producer availability.
- Trigger definitions, keyed by trigger_key, subscribe to event definitions and own picker
  presentation, configuration, preset compilation, and allowed execution modes.

Each trigger definition contains:

    trigger_key
    family: "event" | "manual" | "schedule" | "webhook"
    category, resource, event labels and description
    match_event_keys
    subject type and whether a subject is required
    fixed watch/filter preset
    event-field restrictions, if narrower than the subscribed event definitions
    allowed principal and subject-scope modes
    configuration schema
    output schema
    producer availability

The same AutomationRegistry object serves producer validation, the catalog endpoint, request
validation, normalized persistence, activation, and frontend rendering. The organization CRM
schema service merges its standard/custom field definitions into the registered CRM event schemas;
it does not create another matcher. A trigger definition is visible only when every event producer
it requires is live.

The client saves decisions, not correlated internal fields:

    {
      "name": "Review enterprise deals crossing 100k",
      "trigger_key": "crm.deal.amount_crossed",
      "status": "active",
      "config": {"direction": "up", "threshold": "100000.00"},
      "watch": null,
      "filter": {
        "all": [
          {"path": "record.pipeline_id", "operator": "eq", "value": "pipeline-uuid"}
        ]
      },
      "principal_mode": "personal",
      "scope": {"type": "owned_or_created_by_principal"},
      "audience": {"mode": "principal"},
      "task": {"goal": "Check risk, next steps, and stakeholder coverage."}
    }

The backend validates the typed threshold and derives match_event_keys, the fixed amount
watch/crossing filter, schema version, execution principal ID, server-owned target snapshot, and
config hash. watch is accepted only for definitions such as generic record-updated that explicitly
allow user-selected watched fields; a preset-derived watch is null in the request and cannot be
overridden. The canonical config hash covers every run-affecting field: trigger/config/watch/filter,
principal/scope/audience, task/delivery, and pinned target revision.

Keep the existing automation_triggers table and useful status/counter/schedule/target fields, but
replace the ambiguous source contract with these server-owned projections:

- trigger_key and family;
- match_event_keys for indexed event candidate lookup;
- created_by_user_id, execution_principal_type, and execution_principal_id;
- trigger, watch, filter, subject-scope, audience, task, and delivery config;
- existing pinned target snapshot, config hash, lifecycle, scheduling, and observability fields.

Generalize automation_event_dispatches with event_key, route mode/direct trigger projection,
idempotency key, and the validated envelope JSON. AgentRun.input_snapshot stores the immutable
envelope plus the trigger and pinned-agent snapshots.

## Principal, subject scope, and audience are separate

Conflating these creates security bugs and duplicate work:

- A personal binding runs as its user principal and defaults to
  owned_or_created_by_principal. Provider credentials and CRM tool scope belong to that principal.
- An organization binding may be created only by an authorized admin, runs as a non-human service
  principal, and uses an explicitly granted organization scope.
- The actor that caused the event is evidence, not automatically the execution principal.
- Audience controls run visibility and notification fanout; it never grants data access.
- Every filterable subject-scope fact, such as owner_id or created_by_user_id, is present in the
  event-time record. Scope fails closed when a required fact is absent.

The first implementation has two valid execution combinations. personal means a user principal,
owned_or_created_by_principal scope, and principal audience. organisation means the
organisation_service principal, organisation scope (optionally narrowed by filters), and none,
explicit_users, or subject_participants audience. Only an authorized admin can create the second.
Explicit user IDs and subject participants are resolved and tenant-validated by the backend.
Filters may narrow these scopes but never broaden them. Each combination has a code-owned,
fail-closed tool/capability allowlist.

A shared meeting produces one meeting event, not one event per attendee. Each eligible binding may
run once. An organization binding performs one shared analysis and its audience fans the result
out. Personal bindings run only when the meeting falls inside that principal's declared scope.

## Matching and delivery behavior

For a match event:

1. Validate the envelope against the event definition.
2. Query active bindings by organisation_id and match_event_keys.
3. Enforce principal lifecycle, subject scope, import policy, and causation guards.
4. Apply fixed and user watches/filters.
5. Insert one AgentRun per matching binding with uniqueness on trigger_id plus event_id.
6. Publish the pending run after commit.

A binding-level validation or evaluation failure pauses/skips that binding with a reason and
continues other candidates; it does not poison the source event. An invalid producer envelope
fails the dispatch itself because no binding can safely interpret it.

For a direct event:

1. Load the exact trigger_id.
2. Require active status and the same config_hash carried by the event.
3. Enforce principal, subject, and causation rules.
4. Insert the idempotent run.

Before model execution, runtime revalidates the organization, active principal, subject access,
pinned target, and explicit tool/capability policy. A match at event time never bypasses the normal
runtime trust boundary.

Match events intentionally use the active binding at processing time; a trigger paused before
processing does not run, and an edit may change the result. We will not add historical trigger
version matching for unused dev data. Direct events carry config_hash because a queued schedule,
manual request, or webhook delivery must not execute a stale configuration.

Idempotency identities are source-specific but deterministic:

- CRM/email/meeting/AI: organization + event_key + durable source event ID;
- schedule: trigger ID + UTC occurrence + subject ID (or global sentinel) + config hash;
- manual: trigger ID + authenticated invocation ID + subject ID (or global sentinel);
- webhook: trigger ID + verified external dedupe key, or canonical payload hash inside a replay
  window;
- run: trigger ID + envelope event ID.

Delivery is at least once. Exactly-once external side effects remain the responsibility of the
tool/action idempotency boundary.

A record-scoped schedule claims one occurrence, selects a capped set through its declared subject
scope, and creates one direct envelope/run per subject. A bulk manual request uses the same fanout.
The occurrence/invocation is the parent audit identity carried as correlation_id; a failed subject
can retry without re-running successful siblings.

## Target trigger catalog

The catalog exposes only rows whose producer is implemented; this table defines the destination
taxonomy and prevents one-off naming.

| Category / resource | Base event keys or direct family | User-facing presets |
| --- | --- | --- |
| System / Manual | system.manual.invoked, direct | Global, single record, multi-select fanout (one run per record) |
| Time / Schedule | time.schedule.due, direct | One time, daily, weekdays, weekly, biweekly, monthly, custom supported cadence |
| CRM / Deal | created, updated, deleted | Created/updated, stage entered/exited, won/lost/reopened, amount/close date/owner/account/contact changed, threshold crossed |
| CRM / Contact | created, updated, deleted | Email/phone/owner/account/lifecycle changed, created or updated |
| CRM / Account | created, updated, deleted | Domain/owner/lifecycle/tier changed, created or updated |
| CRM / Lead | created, updated, deleted, converted | Source/status/score/owner changed, form submitted |
| CRM / Task | created, updated, deleted; overdue | Assigned, completed, reopened, due soon, overdue |
| CRM / Note | created, updated, deleted | Added to a deal/contact/account |
| Communication / Email | received, sent, replied | Sender/domain, recipient, attachment, thread, related record, intent metadata |
| Conversation / Meeting | scheduled, rescheduled, cancelled, ended, transcript ready, summary ready | Owner/team/attendee/related record/provider/language |
| Conversation / Message | received, sent | Channel/provider/sender/related record; future after identity rules exist |
| Integration / Webhook | integration.webhook.received, direct | Definition-specific typed payload |
| AI / Deal signal | detected, changed, resolved | Risk, blocker, health, next step, pricing intent, competitor |
| AI / Buyer signal | detected, changed, resolved | Intent, objection, sentiment, stakeholder role/gap |
| Agent / Run | completed, failed, cancelled | Selected agent/trigger/outcome |
| Agent / Approval | requested, granted, rejected, expired | Selected action/risk class/approver |

“Has attachment,” “stage is Proposal,” “confidence above 0.8,” and “sender domain is acme.com” are
filters or presets, not producer event keys.

## AI-native and operational safeguards

- AI signals are emitted only after inference, evidence, confidence, model/prompt identity, and
  lifecycle state are durable. A meaningful transition emits detected/changed/resolved.
- The matcher never calls an LLM. An inference failure is visible as an inference failure, not a
  silent “filter did not match.”
- correlation_id, caused_by_run_id, and causation_depth travel through agent-originated writes.
  The same trigger cannot run twice in one causation chain. The first implementation permits at
  most three automated hops and does not make that ceiling user-configurable.
- Initial sync and backfill events are ignored by default. An admin may opt a binding in only with
  preview, rate limits, and an explicit run cap. Interactive writes and incremental sync use normal
  behavior.
- Schedule config uses IANA timezones. Store each occurrence as UTC while calculating recurrence
  in local time so daylight-saving changes preserve the user's wall-clock intent.
- Webhooks require an unguessable per-trigger endpoint, secret/signature verification, timestamp
  replay window, schema/size limits, rate limits, secret rotation, and tenant identity from the
  route—not the body.
- Candidate, matched, skipped reason, run ID, replay result, source correlation, latency, and
  eventual token/cost data are recorded for each activation.
- Event ordering is not assumed across producers. If a trigger requires ordered state transitions,
  that ordering must be materialized by its producer before the event is emitted.

## Design discussion: resolved questions

**Why not keep stage_changed, amount_changed, and every other semantic mutation as separate
producer events?**

They duplicate the same write, can create duplicate runs, and force every new field through
producer and matcher code. Base lifecycle events plus watched fields and before/after filters
express them once. Separate keys are reserved for facts not reducible to a mutation.

**Can users trigger on a deal reaching one specific value?**

Yes. Use record.field for current state, changes.field.before for the previous value, and
changes.field.after for the destination. Crossing a threshold needs both before and after; merely
being above it uses record.

**What happens when a field is explicitly cleared?**

The changes entry is present with after: null. That is distinct from a missing path, which fails
closed.

**What if the record is deleted before the worker processes it?**

Deletion filters use the pre-delete record snapshot. The agent receives the snapshot and references;
tools may correctly report that the live record no longer exists.

**Do filters read the latest CRM row?**

No. They use the event-time envelope. Loading the latest row makes retries nondeterministic and can
miss rapid transitions.

**What about computed/custom fields?**

The schema service provides stable keys and types. The producer records their event-time values.
Deleted or incompatibly changed definitions pause affected bindings.

**Will initial integration sync launch thousands of agents?**

No. initial_sync and backfill are excluded unless an admin explicitly opts in under capped
execution.

**Who owns an event visible to several users?**

Nobody is inferred from attendees. Principal, subject scope, and audience decide eligibility and
delivery. A shared fact is emitted once.

**How are loops stopped without blocking useful agent chaining?**

Causation metadata, per-chain trigger uniqueness, depth/rate caps, and idempotent actions allow
bounded chaining while stopping self-amplification.

**Why direct routing for manual, schedule, and webhook?**

Those sources exist because one saved binding was invoked. Broadcasting them by event key would
scan unrelated bindings and makes stale schedule/webhook configuration harder to reject.

**Why not put a whole manual selection into one bulk envelope?**

The current agent runtime, access policy, retries, and run audit are subject-oriented. Fanout reuses
those guarantees and avoids inventing iterator semantics. Add an atomic batch subject only after a
real workflow requires one.

**Can one new source require a new matcher branch?**

No. It may add a producer adapter and registry contract. If activation needs an
if source_domain branch, the implementation violates this design.

## Cross-functional review loop

This is a structured design audit, not a claim of human sign-off. Each reviewer lens raised a
blocking concern; the final column is the implementation gate that satisfies it.

| Lens | Concern raised | Resolution / gate |
| --- | --- | --- |
| Product | Specific-value triggers must be understandable without exposing event internals | Presets compile to watches/filters; advanced mode uses the same paths |
| Frontend | New resources should not require bespoke forms | Catalog provides hierarchy, fields, options, operators, dependencies, and defaults |
| Backend | Source expansion must not grow activation branches | One envelope, registry, candidate query, and matcher; source-specific code ends at the adapter |
| Data | Rapid updates, nulls, deletes, decimals, and custom fields must replay exactly | Event-time snapshots, exact before/after values, canonical types, schema-key validation |
| Security/privacy | Tenant broadcast, forged webhook tenants, and copied sensitive text are unacceptable | Explicit principal/scope, server-derived tenant, signed webhooks, content by reference |
| Reliability/operations | Retries, stale schedules, imports, and loops can create duplicate/costly runs | Layered idempotency, config hash, import caps, causation guards, structured skip telemetry |
| AI/platform | Matcher-time inference is costly and nondeterministic | Persist AI insight/evidence first; emit only a stable state transition |
| Admin/governance | Organization automation cannot silently borrow a human's credentials | Authorized creation, service principal, explicit org scope and audience |

No design conflict remains between these lenses. Human product, security, and engineering owners
still approve the contract before implementation; the gates above make that approval concrete.

## Walking skeleton and implementation boundaries

Implement in this order:

1. Add the typed registry and make the existing catalog endpoint and create/update validator consume
   it. Port deal created and deal updated, including the specific-value examples above.
2. Generalize AutomationEventDispatch and adapt the current CRM mutation ledger to the envelope.
   Replace CRM-only activation with candidate lookup plus the generic watch/filter evaluator.
3. Prove a second and third domain: enqueue email.message.received for changed inbound rows in the
   same folder-write transaction before commit, and meeting.transcript.ready at the current
   idempotent durable Recall transcript boundary.
4. Add direct manual invocation, then the missing due-schedule dispatcher, then authenticated
   webhooks.
5. Add remaining CRM producers and persisted AI signals only when each underlying durable fact
   exists.

Likely code ownership:

| Concern | Existing path to reuse/change |
| --- | --- |
| Registry/catalog/API schemas | src/service/automation/triggers/catalog.py and src/api/schemas/automation_trigger_models.py |
| Validation/filter engine | src/service/automation/triggers/validator.py |
| Durable event/outbox | src/service/automation/events and AutomationEventDispatch |
| Generic activation/run creation | src/service/automation/triggers/activation.py |
| CRM adapter | src/service/crm/mutations and src/service/crm/deals/write_service.py |
| Email producer boundary | src/service/integrations/email_delta_sync_service.py |
| Transcript producer boundary | src/service/meeting_bot/summary_trigger_service.py |
| Existing replay worker | src/workers/chat_worker/tasks/automation_event_tasks.py |

The walking skeleton is complete only when focused runnable checks prove:

- a deal created filter matches a specific stage/custom value;
- a multi-field deal update matches before/after and threshold-crossing filters exactly once;
- no-op, non-watched, wrong-value, missing-path, wrong-tenant, and out-of-scope events create no run;
- replay of a dispatch returns the same dispatch/run and creates no duplicate;
- deleted records match from their pre-delete snapshot;
- custom-field removal pauses, rather than broadens, a binding;
- a stale direct event is rejected after trigger edit/pause;
- initial sync is excluded by default and capped when explicitly enabled;
- email and transcript events use the same matcher with no source-domain branch;
- agent-caused writes stop at the configured causation guard.

No full workflow engine, plugin system, event-stream replacement, matcher-time LLM, or historical
trigger-version subsystem belongs in this change.

## Research basis

Twenty's current design informed the small trigger-family set, progressive picker, watched fields,
typed filters, and output contracts:

- [Workflow trigger guide](https://docs.twenty.com/user-guide/workflows/capabilities/workflow-triggers)
- [Pinned trigger union](https://github.com/twentyhq/twenty/blob/0f35b5895a4b67a84ea92b36185220ac96e6b66e/packages/twenty-shared/src/workflow/schemas/workflow-trigger-schema.ts)
- [Pinned database-event contract](https://github.com/twentyhq/twenty/blob/0f35b5895a4b67a84ea92b36185220ac96e6b66e/packages/twenty-shared/src/workflow/schemas/database-event-trigger-schema.ts)
- [Pinned event listener](https://github.com/twentyhq/twenty/blob/0f35b5895a4b67a84ea92b36185220ac96e6b66e/packages/twenty-server/src/modules/workflow/workflow-trigger/automated-trigger/listeners/workflow-database-event-trigger.listener.ts)

CopilotGTM should not copy Twenty's database-only emphasis or weaken its existing durable
outbox/idempotency path. The decisions above come from tracing the current trigger catalog,
validator, dispatch service, activation service, CRM mutation ledger, deal schema/custom-field
types, email delta sync, and Recall transcript-ready flow in this worktree.
