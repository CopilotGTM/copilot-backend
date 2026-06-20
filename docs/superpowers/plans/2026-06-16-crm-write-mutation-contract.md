# CRM Write Mutation Contract

## Goal

Make CRM writes deterministic, auditable, and triggerable from one production-level
contract, regardless of whether the change starts from frontend API, agent tool,
interactive card, or future workflow automation.

The immediate user-facing examples are deal creation and deal updates:

1. A user creates a deal from the frontend.
2. A user updates one or many deal values from the frontend.
3. A user asks the agent to update deal values after approval.
4. A bot form submission moves a deal stage.
5. A future automation updates a deal after explicit approval.

All paths must create the same canonical mutation record so downstream
workflow triggers can reliably answer: what changed, who/what changed it, from
which channel, which triggerable business changes occurred inside the mutation,
and has trigger dispatch already run?

## Current State

The code already has useful building blocks, but the write contract is split:

| Surface | Current file | Current behavior |
| --- | --- | --- |
| Frontend deal PATCH | `src/api/routers/crm/deals_handler.py` | Calls `DealWriteService.update_deal()` then commits in the handler. |
| Frontend stage move | `src/api/routers/crm/deals_handler.py` | Calls `DealWriteService.move_stage()` then commits in the handler. |
| Frontend close/reopen/owner/account/contact | `src/api/routers/crm/deals_handler.py` | Calls separate `DealWriteService` methods, each with its own metadata shape. |
| Agent scalar updates | `src/agents/tools/crm/update_deal_fields.py` | Applies policy, then calls `DealWriteService.update_deal()` and commits. |
| Agent lifecycle updates | `src/agents/tools/crm/update_deal_lifecycle.py` | Applies policy, then calls `move_stage()`, `close_deal()`, or `reopen_deal()`. |
| Agent stage form | `src/agents/tools/crm/update_deal_stage.py` | Sends a card only; mutation happens later in the form handler. |
| Bot stage form submit | `src/service/bots/form_handlers/update_deal_stage_handler.py` | Resolves user/org, calls `DealWriteService.move_stage()`, commits. |
| Provider sync writers | `src/service/integrations/crm/records/*/deals/*writer.py` | Write normalized provider state directly and maintain provider stage history. |

`DealWriteService` is the best current write boundary. It already enforces:

- tenant ownership through `organisation_id`
- internal CRM read-only protection through `_load_internal_deal()`
- stage validation through `_load_stage()` and `_resolve_stage_for_state()`
- stage history rows through `_record_stage_history()`
- narrow transactions controlled by the caller

The missing production contract is not another prompt rule. The missing contract
is a durable mutation envelope emitted by every write path.

## Confidence And Loophole Review

This design is directionally right, but not "100% sure" unless the loopholes
below are handled in implementation. The first version was too clean around
dispatch and idempotency; production needs sharper rules.

| Loophole | Why it matters | Required fix |
| --- | --- | --- |
| Same-value writes can still emit events | `move_stage()` currently applies stage state and records history without first proving the new stage differs from the old stage. A duplicate form submit could create duplicate history and duplicate workflow triggers. | Every mutation must compute a diff before writing trigger events. Same-value writes return `no_op=True`, do not create workflow-triggering events, and should not create a new `CRMDealStageHistory` row. |
| Stale forms can overwrite newer state | The stage form captures available stages but not an expected current stage/version. A user can submit an old card after the deal has already moved. | Stage form metadata should include `expected_stage_id` or `expected_updated_at`. `apply_deal_mutation()` should reject or require confirmation when the current state no longer matches. |
| Event idempotency can alias different payloads | A unique `(organisation_id, idempotency_key)` prevents duplicate keys but does not prove the replay payload is identical. | Store `payload_hash`. If the same idempotency key is reused with a different payload hash, reject with an idempotency conflict. |
| Concurrent writers can create conflicting history | Two requests can read the same old stage and both write different stage histories unless the deal row is locked. | Use row-level locking for mutating deal loads, or add optimistic `expected_revision` checks before writing. Stage/history tests must cover concurrent submissions. |
| One event may trigger multiple workflows | A single parent `dispatch_status=dispatched` on `crm_mutation_events` cannot track partial success when one event fans out to several workflow runs. | Add a child dispatch ledger such as `crm_mutation_event_dispatches` keyed by `(event_id, trigger_key)`, with per-trigger status and idempotency. |
| Multi-field updates can become ambiguous | A single frontend save can update five fields, but only one or two fields may have workflow triggers. If `event_type` is overloaded, it is unclear whether the event is the user action or a triggerable business change. | Use `event_type` for the user/API action, such as `crm.deal.updated`, and `semantic_event_types` for triggerable changes inside it, such as `crm.deal.stage_changed`. |
| Dispatch can race across workers | A periodic retry task and request fast-path dispatcher can both try to evaluate the same mutation. | Claim child workflow runtime/evaluation rows with `SELECT ... FOR UPDATE SKIP LOCKED` or an atomic status transition. Do not store this claim state on `crm_mutation_events`. |
| "Dispatched" can mean only "enqueued" | Celery enqueue success is not the same as workflow completion. | Dispatch/enqueue status and actual workflow/job completion must live on workflow runtime/job rows, not on the parent CRM mutation row. |
| Provider sync event volume can explode | Initial syncs or incremental backfills may update thousands of deals. Emitting full outbox events for every provider field update can flood trigger queues. | Keep provider-sync recording off in Phase 1. Later, batch/coalesce provider events and default them to `ignored` unless a workflow explicitly opts in. |
| Future external CRM writeback needs source mapping | Local mutation events only know local UUIDs. Writing back to HubSpot/Salesforce requires provider object IDs and capability checks. | Include `integration_id`, `provider`, and optional `source_object_mapping_id`/external IDs in events when available, resolved through `crm_source_object_mappings`. |
| Changed-field JSON may leak or break serialization | Values can be datetimes, Decimals, UUIDs, long text, or sensitive fields. | Normalize values to JSON-safe scalars and define redaction/length limits before storing `changed_fields`. |
| Workflow-triggered writes can loop | A stage-change workflow can update the same deal and trigger itself again. | Keep direct workflow run/source event IDs out of `crm_mutation_events` for now. Before enabling `source=workflow`, add explicit causality in workflow runtime tables or clearly named fields such as `caused_by_workflow_run_id`, `caused_by_source_domain`, and `caused_by_source_event_id`, plus deterministic idempotency keys and loop guards. |
| Commit-after-service remains a sharp edge | The service inserts outbox rows, but callers still own commit. A future caller could forget to dispatch after commit. | The durable outbox makes dispatch recoverable, but handlers should use a common post-commit helper. Tests should assert no direct Celery enqueue happens inside the DB transaction. |
| Deal creation can look like many field updates | A created deal has many initial values, but treating every non-null field as a workflow update can create noisy and wrong triggers. | Use `event_type=crm.deal.created` and semantic types only for creation-specific triggers unless a product rule explicitly allows "created with stage X" or "created with amount over Y". |
| Field triggerability can drift across code paths | If each writer decides independently which fields are triggerable, frontend and agent updates will produce different workflow behavior. | Centralize canonical field diffing and semantic event derivation in `DealWriteService` or a small CRM mutation policy module. |
| Partial updates can hide intent | A PATCH payload may omit fields, explicitly set a field to null, or include unchanged values. Those are different cases for audit and workflow matching. | Compare persisted old values to normalized new values. Store only real changes. Preserve null changes as `{old: value, new: null}`. Do not emit events for omitted or same-value fields. |
| Org migrations are broader than one table | This is schema-per-tenant; adding an org table requires org migration coverage across all org schemas. | Add an org migration and a migration-head test. Do not add only a model without tenant migration coverage. |

## Problems To Fix

1. **No single mutation result**

   Each method returns `CRMDeal`, but downstream systems need a structured result:
   changed fields, old values, new values, actor, source, request id, idempotency key,
   and whether the write was a no-op.

2. **No durable trigger boundary**

   Stage history records exist, but they are stage-specific. They do not cover
   amount, owner, account, close/reopen, primary contact, or custom field updates.
   They also are not an outbox with dispatch status, retry count, or idempotency.

3. **Post-write workflows would be duplicated**

   If workflow triggering is added directly inside frontend handlers, agent tools,
   and bot handlers, the same stage-change logic will drift again.

4. **Commit ownership is inconsistent for future side effects**

   Current handlers commit after service mutation. If a workflow is dispatched
   before commit, a worker can observe missing or rolled-back data. If it is only
   dispatched after commit without a persisted event, crashes can lose triggers.

5. **External provider sync and internal app writes need different semantics**

   Provider sync is source-of-truth ingestion. Internal app writes are user/app-owned
   CRM writes. Both may produce useful change events, but only internal app writes
   should attempt outbound CRM-write workflows unless a provider-writeback layer is
   explicitly added.

## Target Contract

Add one canonical mutation method:

```python
result = await deal_write_service.apply_deal_mutation(command)
```

Where `command` is a typed envelope:

```python
@dataclass(frozen=True)
class CRMDealMutationCommand:
    organisation_id: UUID
    deal_id: UUID
    actor_user_id: UUID | None
    source: Literal["frontend", "agent", "bot_form", "workflow", "provider_sync", "import"]
    expected_stage_id: UUID | None = None
    expected_updated_at: datetime | None = None
    operation: Literal[
        "update_fields",
        "move_stage",
        "close",
        "reopen",
        "assign_owner",
        "update_account",
        "update_primary_contact",
        "delete",
    ]
    payload: dict[str, Any]
    idempotency_key: str | None = None
    payload_hash: str | None = None
    correlation_id: str | None = None
    approval_state_id: UUID | None = None
```

The result should include:

```python
@dataclass(frozen=True)
class CRMDealMutationResult:
    mutation_id: UUID
    deal_id: UUID
    operation: str
    changed_fields: dict[str, CRMFieldChange]
    no_op: bool
    event_ids: list[UUID]
```

`CRMFieldChange` should capture old/new values using JSON-safe values:

```python
@dataclass(frozen=True)
class CRMFieldChange:
    old: Any
    new: Any
```

This method should be the only public deal mutation entry point used by:

- API handlers
- agent tools
- bot form handlers
- workflow automation

Existing methods such as `move_stage()`, `close_deal()`, and `update_deal()` can
remain temporarily, but they should become private helpers or thin compatibility
wrappers that call `apply_deal_mutation()`.

## Durable Event Model

Add an org-scoped outbox table, tentatively named `crm_mutation_events`.

Minimum columns:

| Column | Purpose |
| --- | --- |
| `event_id` | Primary key. |
| `organisation_id` | Tenant boundary and query partition. |
| `event_type` | Stable action-level event, e.g. `crm.deal.updated`, `crm.deal.stage_changed`, `crm.deal.closed`. |
| `semantic_event_types` | Triggerable business changes found inside the action, e.g. `["crm.deal.stage_changed", "crm.deal.amount_changed"]`. |
| `mutation_group_id` | Groups field changes caused by one user/app action. |
| `entity_type` | Start with `deal`; allows future contact/account/task events. |
| `entity_id` | Local UUID of the changed entity. |
| `deal_id` | Nullable direct deal shortcut for deal history/workflow matching. |
| `operation` | `move_stage`, `update_fields`, `close`, etc. |
| `occurred_at` | Business time when the mutation happened. |
| `changed_fields` | JSONB map of field -> `{old, new}`. |
| `summary` | Optional audit/timeline display summary, not source-of-truth. |
| `integration_id` | CRM integration for the entity, when available. |
| `provider` | `copilotgtm`, `hubspot`, `salesforce`, etc. |
| `source_object_mapping_id` | Optional mapping row for provider-backed objects. |
| `actor_user_id` | User responsible, or automated sentinel/null depending on source. |
| `source` | `frontend`, `agent`, `bot_form`, `workflow`, `provider_sync`, `import`. |
| `source_label` | Optional display label for source. |
| `approval_state_id` | Optional link to approval/confirmation state. |
| `correlation_id` | Request/job correlation. |
| `idempotency_key` | Unique key for replay-safe writes. |
| `payload_hash` | Hash of canonical command payload for idempotency conflict checks. |
| `created_at` / `updated_at` | Audit and operational ordering. |

Recommended indexes:

```sql
CREATE INDEX ix_crm_mutation_events_semantic_event_types
ON crm_mutation_events USING GIN (semantic_event_types);

CREATE INDEX ix_crm_mutation_events_entity
ON crm_mutation_events (organisation_id, entity_type, entity_id, created_at);

CREATE INDEX ix_crm_mutation_events_deal
ON crm_mutation_events (organisation_id, deal_id, occurred_at);

CREATE UNIQUE INDEX ux_crm_mutation_events_idempotency
ON crm_mutation_events (organisation_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

Add a child dispatch table before real workflow fanout:

| Column | Purpose |
| --- | --- |
| `dispatch_id` | Primary key. |
| `event_id` | Parent mutation event. |
| `organisation_id` | Tenant boundary. |
| `trigger_key` | Stable workflow/rule key. |
| `matched_event_type` | Semantic event type that matched, e.g. `crm.deal.stage_changed`. |
| `matched_field` | Canonical changed field that matched, e.g. `stage_id`. |
| `status` | `pending`, `enqueued`, `failed`, `ignored`. |
| `workflow_run_id` / `job_id` | Downstream workflow or job identity. |
| `idempotency_key` | Replay-safe key for this event + trigger. |
| `attempt_count` / `last_error` | Retry accounting. |

Recommended uniqueness:

```sql
CREATE UNIQUE INDEX ux_crm_mutation_event_dispatch_trigger
ON crm_mutation_event_dispatches (event_id, trigger_key);
```

This table is not a replacement for `CRMDealStageHistory`. Stage history remains
the domain history for reporting and timeline backfill. `crm_mutation_events` is
the durable CRM write contract for audit and future workflow trigger evaluation.
Workflow claim/retry/error status belongs in child workflow runtime tables, not
on the parent CRM mutation row.

## Deal Lifecycle Mutation Scope

The mutation contract should cover the whole internal deal lifecycle, not just
stage movement.

Use one event row for one user/API/business action:

| Action | Event type | Operation | Changed fields | Semantic event types |
| --- | --- | --- | --- | --- |
| Create deal | `crm.deal.created` | `create` | Initial visible values, with `old=null` and `new=value`. | Usually `crm.deal.created`; optionally creation-specific semantic types such as `crm.deal.created_with_stage` later. |
| Update scalar fields | `crm.deal.updated` | `update_fields` | Only fields whose normalized value changed. | Only configured triggerable fields, e.g. `crm.deal.amount_changed`. |
| Move stage only | `crm.deal.stage_changed` | `move_stage` | `stage_id`, `stage_name`, and derived status fields if changed. | `crm.deal.stage_changed`. |
| Update stage plus other fields in one save | `crm.deal.updated` | `update_fields` | One combined changed-field map. | `crm.deal.stage_changed` plus any other triggerable changed fields. |
| Close won/lost | `crm.deal.closed` | `close` | `deal_status`, `stage_id`, `close_date`, `closed_reason`, derived timestamps. | `crm.deal.closed`, plus `crm.deal.stage_changed` if the close operation moved stage. |
| Reopen deal | `crm.deal.reopened` | `reopen` | `deal_status`, stage/status fields, close metadata cleared. | `crm.deal.reopened`, plus stage semantic type if stage changed. |
| Assign owner | `crm.deal.owner_changed` or `crm.deal.updated` | `assign_owner` or `update_fields` | `owner_id`, optional owner display fields. | `crm.deal.owner_changed`. |
| Update account link | `crm.deal.account_updated` | `update_account` | Deal company link/display fields plus namespaced `account.*` audit fields. | Empty list in this phase; audit-only. |
| Update primary contact link | `crm.deal.primary_contact_updated` | `update_primary_contact` | `primary_contact_id`, `primary_contact_name`, `primary_contact_email`. | Empty list in this phase; audit-only. |
| Delete/archive deal | `crm.deal.deleted` or `crm.deal.archived` | `delete` / `archive` | Lifecycle/delete marker fields. | `crm.deal.deleted` or `crm.deal.archived` only if workflows need it. |

Important distinction:

```text
event_type = what business action happened
semantic_event_types = which triggerable business changes are inside that action
changed_fields = exact old/new audit evidence
```

For creation, do not blindly convert every initial value into an update trigger.
A newly created deal with `stage_id=Proposal` is not the same business event as
an existing deal moving from Discovery to Proposal. If the product wants a
workflow for that, define it as a creation trigger condition:

```text
When crm.deal.created and initial stage = Proposal
```

not as a fake stage-change event.

## Stage Update Semantics

Stage updates should produce both:

1. A `CRMDealStageHistory` row for stage reporting/history.
2. A `crm_mutation_events` row for trigger dispatch.

But only when the stage actually changes. If `old_stage_id == new_stage_id`,
the mutation result should be a no-op and no stage history or workflow event
should be created.

The event should include at least:

```json
{
  "stage_id": {
    "old": "previous-stage-uuid",
    "new": "new-stage-uuid"
  },
  "stage_name": {
    "old": "Discovery",
    "new": "Proposal"
  },
  "deal_status": {
    "old": "open",
    "new": "open"
  }
}
```

Close/reopen operations should be represented as lifecycle mutations, not just
field patches. For example, closing as lost may change `stage_id`, `deal_status`,
`close_date`, `closed_lost_at`, and `closed_reason`; all of those field changes
belong to one mutation event.

## Multi-Field Update Semantics

Frontend, agent tools, and bot handlers should not split one user save into
several field-by-field API calls just because some fields may trigger workflows.
That creates worse audit history, more partial-save risk, and more race
conditions.

The production rule is:

```text
One user/API action = one crm_mutation_events row.
One crm_mutation_events row = zero, one, or many workflow dispatches.
```

Example: a user updates five fields in one save:

```json
{
  "event_id": "evt_001",
  "event_type": "crm.deal.updated",
  "semantic_event_types": [
    "crm.deal.stage_changed",
    "crm.deal.amount_changed"
  ],
  "mutation_group_id": "grp_001",
  "entity_type": "deal",
  "deal_id": "deal_123",
  "operation": "update_fields",
  "changed_fields": {
    "stage_id": {
      "old": "stage_discovery",
      "new": "stage_proposal"
    },
    "expected_close_date": {
      "old": "2026-07-01",
      "new": "2026-08-01"
    },
    "amount": {
      "old": 50000,
      "new": 65000
    },
    "priority": {
      "old": "medium",
      "new": "high"
    },
    "description": {
      "old": "Old notes",
      "new": "Updated notes"
    }
  }
}
```

The full record is still one CRM history entry: "user updated five fields." The
workflow engine only reacts to configured semantic changes:

```text
stage_id changed -> semantic event crm.deal.stage_changed -> evaluate stage workflows
amount changed -> semantic event crm.deal.amount_changed -> evaluate amount workflows
expected_close_date changed -> no semantic trigger configured -> audit only
priority changed -> no semantic trigger configured -> audit only
description changed -> no semantic trigger configured -> audit only
```

If two workflow triggers match, create two child dispatch rows from the same
mutation event:

```json
[
  {
    "dispatch_id": "disp_001",
    "event_id": "evt_001",
    "matched_event_type": "crm.deal.stage_changed",
    "matched_field": "stage_id",
    "workflow_trigger_id": "trigger_stage_proposal",
    "workflow_run_id": "run_001",
    "status": "enqueued"
  },
  {
    "dispatch_id": "disp_002",
    "event_id": "evt_001",
    "matched_event_type": "crm.deal.amount_changed",
    "matched_field": "amount",
    "workflow_trigger_id": "trigger_large_amount",
    "workflow_run_id": "run_002",
    "status": "enqueued"
  }
]
```

Only create separate `crm_mutation_events` rows when the backend performs
separate business operations, such as "close deal", "assign owner", and "create
task" as distinct actions. A single deal update request with many changed fields
should remain one mutation event.

## Workflow Trigger Boundary

Do not dispatch workflows directly from `DealWriteService`.

For the concrete API-to-worker runtime sequence, see
`docs/superpowers/plans/2026-06-17-crm-mutation-workflow-runtime-flow.md`.

Instead:

1. `DealWriteService.apply_deal_mutation()` writes the CRM rows and inserts one
   or more `crm_mutation_events` rows in the same DB transaction.
2. The API/tool/form handler commits.
3. A dispatcher runs after commit and claims pending events with row locking or
   an atomic claim update.
4. The dispatcher evaluates workflow rules and creates per-trigger dispatch rows.
5. The dispatcher enqueues tasks idempotently per dispatch row.
6. Dispatcher records trigger evaluation, claim/retry/error state, and workflow
   run IDs in child workflow runtime rows. The parent CRM mutation row remains
   immutable audit truth.

This is the transactional outbox pattern. It prevents both lost triggers and
workers reading uncommitted state.

Initial dispatcher options:

- For API handlers and bot handlers, call `CRMWriteEventDispatcher.dispatch_pending_for_entity()`
  after commit as a best-effort fast path. Failure must not roll back the CRM write.
- Also run a periodic retry task over the future workflow runtime/evaluation table
  so crashes after commit do not lose workflow execution.

## Workflow Rule Evaluation

Create a small service boundary:

```python
class CRMWriteWorkflowTriggerService:
    async def evaluate(self, event: CRMMutationEvent) -> list[WorkflowDispatch]:
        ...
```

V1 should support deterministic rule checks only:

- entity type is `deal`
- event type is an action-level type such as `crm.deal.updated`
- semantic event types include triggerable changes such as `crm.deal.stage_changed`
- changed field contains `stage_id`, `deal_status`, `owner_id`, `amount`, etc.
- source filters: `frontend`, `agent`, `bot_form`, `workflow`
- no LLM call during event evaluation
- explicit rule id / trigger key for idempotent per-workflow dispatch

Example stage trigger:

```python
if "crm.deal.stage_changed" in event.semantic_event_types:
    trigger_workflows_for_stage_change(
        organisation_id=event.organisation_id,
        deal_id=event.deal_id,
        old_stage_id=event.changed_fields["stage_id"]["old"],
        new_stage_id=event.changed_fields["stage_id"]["new"],
        source=event.source,
    )
```

The workflow engine/table can come later. The important production boundary now
is that the CRM write event is durable and uniform.

## Source Rules

| Source | Should emit mutation event? | Should trigger workflows by default? |
| --- | --- | --- |
| `frontend` | Yes | Yes |
| `agent` | Yes | Yes after policy/approval passes |
| `bot_form` | Yes | Yes |
| `workflow` | Yes | Yes, but with loop-prevention metadata |
| `import` | Yes | Usually no, unless explicitly configured |
| `provider_sync` | Yes for audit/backfill if cheap; no outbound writeback by default |

Correction after loophole review: `provider_sync` should not emit default pending
workflow events in the first implementation. Provider writers can record audit
events later only after batching/coalescing and opt-in trigger semantics exist.

Loop prevention is mandatory before workflow-triggered writes are enabled:

- store direct workflow fanout in `crm_mutation_event_dispatches`, not on `crm_mutation_events`
- add explicit causality later for workflow-caused CRM writes, such as
  `caused_by_workflow_run_id`, `caused_by_source_domain`, and
  `caused_by_source_event_id`
- include idempotency keys for workflow writes
- block rules that would write the same field back to the same value

## Validation Rules

`apply_deal_mutation()` must preserve current safety rules:

- Always load deal by `organisation_id` + `deal_id`.
- Lock the deal row or verify an expected revision/current value for mutating writes.
- Internal app writes can mutate only internal CRM deals.
- External provider deals stay read-only for frontend/agent/bot writes.
- Stage must belong to the deal's pipeline and integration.
- Close-won/close-lost stage state must match the requested outcome.
- `close_lost` requires `closed_reason`.
- Custom fields must use `CRMCustomFieldDefinition` validation.
- Idempotency key replays must compare `payload_hash`.
- No-op writes should not emit workflow-triggering events.
- Changed-field values must be JSON-safe and redacted where needed.

Provider sync can use a lower-level recorder because it already writes external
source-of-truth snapshots. Do not force provider sync through internal CRM write
authorization checks.

## Rewritten Implementation Plan: Deal Creation Through Any Deal Value Update

### Phase 1: Complete the mutation event schema contract

Goal: make the table shape correct before more write paths depend on it.

1. Add `semantic_event_types` to `crm_mutation_events`.
2. Add a GIN index for semantic event matching.
3. Keep `event_type` as the action-level event.
4. Keep `changed_fields` as strict JSONB old/new audit evidence.
5. Keep workflow dispatch claim/retry/error state out of the parent CRM mutation row.
6. Keep `payload_hash` and unique nullable `idempotency_key`.
7. Add migration-head coverage for the final table shape.

Verification:

```bash
uv run pytest tests/migrations/test_org_migration_head_contract.py -q
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
```

### Phase 2: Centralize diff and semantic derivation

Goal: every writer produces the same event shape for the same business change.

1. Add one helper that compares persisted deal values to normalized incoming values.
   - UUID fields compare UUID values, not string-vs-UUID representation.
   - Datetime fields compare canonical timezone-aware ISO values, not `Z` vs `+00:00` spelling.
   - `deal_status` compares the canonical enum value.
2. Store only real changes in `changed_fields`.
3. Preserve explicit null changes.
4. Ignore omitted fields and same-value writes.
5. Diff only an explicit allowlist of auditable CRM deal fields; never use dynamic
   object attribute probing as the field contract.
6. Add one helper that maps canonical changed fields to semantic event types.
7. Start with a small triggerable field registry:
   - `stage_id -> crm.deal.stage_changed`
   - `owner_id -> crm.deal.owner_changed`
   - `amount -> crm.deal.amount_changed`
   - `deal_status -> crm.deal.status_changed`
8. Do not make every field triggerable. Non-trigger fields still remain in
   audit history through `changed_fields`.

Verification:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
uv run pytest tests/service/crm/test_deal_write_service.py -k mutation -q
```

### Phase 3: Route deal creation through the mutation contract

Goal: a created deal becomes auditable and triggerable without pretending every
initial value is a field-change workflow.

Status on 2026-06-17: backend creation now emits a `crm.deal.created`
`crm_mutation_events` row from `DealWriteService.create_deal()`. The event uses
`semantic_event_types=["crm.deal.created"]`, records initial auditable deal
values as `{old: null, new: value}`, and intentionally does not emit
`crm.deal.stage_changed` for the initial stage. General scalar/custom update
routing is covered in Phase 4. Lifecycle routing and dispatcher fanout remain
later phases.

1. Add a creation command path:
   - `operation=create`
   - `event_type=crm.deal.created`
   - `semantic_event_types=["crm.deal.created"]`
2. Insert `crm_deals` and `crm_mutation_events` in the same DB transaction.
3. For `changed_fields`, store initial values as `old=null`, `new=value`.
4. Do not emit `crm.deal.stage_changed` during creation.
5. If stage history is currently expected for initial stage, keep that as
   stage-history semantics, not workflow stage-change semantics.
6. Keep the existing `create_deal()` public method as a wrapper so handlers and
   tools do not all change at once.

Example creation event:

```json
{
  "event_type": "crm.deal.created",
  "operation": "create",
  "semantic_event_types": ["crm.deal.created"],
  "changed_fields": {
    "name": {"old": null, "new": "Acme Expansion"},
    "stage_id": {"old": null, "new": "stage_discovery"},
    "amount": {"old": null, "new": 50000},
    "expected_close_date": {"old": null, "new": "2026-08-01"}
  }
}
```

Verification:

```bash
uv run pytest tests/service/crm/test_deal_write_service.py -k create -q
uv run pytest tests/agents/tools/crm/test_create_deal_direct.py -q
```

### Phase 4: Route any deal value update through the mutation contract

Goal: one save with one or many changed values creates one mutation event and
zero, one, or many workflow dispatch candidates.

Status on 2026-06-17: `DealWriteService.update_deal()` now computes a
normalized diff before applying writes and emits one `crm.deal.updated`
`crm_mutation_events` row for real scalar/custom-field changes. Same-value
updates return the loaded deal without touching timestamps and without creating
an event. Custom field changes are recorded as `custom.<field_key>`.
`semantic_event_types` is derived centrally from triggerable changed fields;
audit-only updates are recorded with `semantic_event_types=[]` and
no parent dispatch status. Stage/lifecycle/owner/delete wrappers are still
separate follow-up phases; account/contact wrappers are covered in Phase 5B/5C.

For multi-field updates:

1. Compute one `changed_fields` map for all fields changed by the request.
2. Create one `crm_mutation_events` row for the full user/API action.
3. Set `event_type = crm.deal.updated` for ordinary scalar updates.
4. Set `semantic_event_types` only for fields with workflow semantics, for
   example `crm.deal.stage_changed`, `crm.deal.owner_changed`, or
   `crm.deal.amount_changed`.
5. Do not create extra mutation rows for non-trigger fields.
6. If there are no real changes, return a no-op result and create no event.
7. If only non-trigger fields changed, still create one audit event with
   `semantic_event_types=[]`. Future workflow evaluation can ignore it without
   mutating the parent CRM event.

Verification:

```bash
uv run pytest tests/service/crm/test_deal_write_service.py tests/api/routers/crm/test_deals_handler.py -q
uv run pytest tests/agents/tools/crm/test_update_deal_fields.py tests/agents/tools/crm/test_update_deal_account.py tests/agents/tools/crm/test_update_deal_lifecycle.py -q
```

### Phase 5: Route lifecycle operations through the mutation contract

Goal: close, reopen, owner change, account/contact changes, and delete/archive
produce domain-level events instead of disconnected field patches.

Status on 2026-06-17: Phase 5A is implemented for close/reopen lifecycle
operations. `DealWriteService.close_deal()` now emits one
`crm.deal.closed` mutation event for a real close action, and
`DealWriteService.reopen_deal()` emits one `crm.deal.reopened` mutation event
for a real reopen action. Both paths compute normalized `changed_fields` before
writing, include all changed lifecycle fields in the same event, add lifecycle
semantic types plus changed-field semantic types such as
`crm.deal.stage_changed` and `crm.deal.status_changed`, and skip both stage
history and mutation events for no-op duplicate lifecycle writes. Duplicate
close/reopen retries are detected before stage resolution when the existing
lifecycle state already satisfies the request, so a harmless retry does not fail
just because stage configuration changed later. Reopen also clears close
timestamps as lifecycle metadata so an open deal does not keep actual-close
timestamps.

Status on 2026-06-17: Account/contact audit events are implemented.
`DealWriteService.update_account()` emits one audit-only
`crm.deal.account_updated` event when the deal company link/display snapshot or
namespaced account fields change. `DealWriteService.update_primary_contact()`
emits one audit-only `crm.deal.primary_contact_updated` event when the primary
contact link/display snapshot changes. Both use `semantic_event_types=[]` and
no parent dispatch state, so they are historical/audit events but not workflow
triggers in this phase. Same-snapshot account/contact writes create no mutation
event, and primary-contact same-snapshot writes skip participant replacement
when the active primary-contact link already exists. Owner/delete events are
implemented. `assign_owner()` emits triggerable `crm.deal.owner_changed` events
only when `owner_id` changes, and emits audit-only
`crm.deal.owner_snapshot_refreshed` events for same-owner display refreshes.
`delete_deal()` emits triggerable `crm.deal.deleted` events, is idempotent for
already-deleted retries, and does not expose `crm.deal.status_changed`
semantics even though `deal_status` is retained in `changed_fields` for audit.

Status on 2026-06-17: service cleanup is implemented for mutation policy code.
Pure CRM mutation contracts, canonical field lists, changed-field normalization,
semantic event derivation, and payload hashing now live in
`src/service/crm/internal/deal_mutation_event_policy.py`. `DealWriteService`
keeps the write facade, tenant/internal-CRM checks, DB writes, and compatibility
wrappers for existing callers/tests.

1. Convert `move_stage()` to emit:
   - `event_type=crm.deal.stage_changed`
   - `operation=move_stage`
   - `semantic_event_types=["crm.deal.stage_changed"]`
2. Convert `close_deal()` to emit one lifecycle mutation event containing all
   close-related field changes.
3. Convert `reopen_deal()` to emit one lifecycle mutation event containing all
   reopen-related field changes.
4. Convert account/contact methods to audit-only mutation events before enabling
   workflow triggers for those surfaces.
5. Convert owner/delete methods only after account/contact audit behavior is
   stable.
6. Keep existing public wrappers while moving internal implementation to
   `apply_deal_mutation()`.

Verification:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
uv run pytest tests/agents/tools/crm/test_update_deal_lifecycle.py -q
```

### Phase 6: Add dispatcher and workflow trigger v1

TODO for future phase: intentionally not implemented during service cleanup.
Build this after workflow trigger definition/run tables are ready.

1. Add `CRMWriteEventDispatcher`.
2. Add `CRMWriteWorkflowTriggerService`.
3. Add `crm_mutation_event_dispatches` before enabling real fanout.
4. Add `matched_event_type` and `matched_field` to dispatch rows.
5. Add `SELECT ... FOR UPDATE SKIP LOCKED` or equivalent claim logic.
6. Add a Celery task to retry pending/failed events.
7. Wire API/tool/form handlers to dispatch after commit.
8. Add one stage-change trigger adapter with idempotency, even if it only logs or
   calls a placeholder workflow runner in v1.
9. Verify one mutation event can create zero, one, or many dispatch rows.

Verification:

```bash
uv run pytest tests/service/crm/test_crm_mutation_event_dispatcher.py -q
uv run pytest tests/workers/deal_worker -k crm_mutation -q
```

### Phase 7: Provider sync event recording

Add an opt-in recorder for HubSpot/Salesforce deal writers after their current
write result is computed. Provider sync events should be marked `source=provider_sync`
and default to `ignored` for workflow dispatch unless a workflow rule explicitly
allows provider-triggered automation. Do not enable this until batching/coalescing
exists; otherwise initial syncs can create a large outbox backlog with no product
value.

Verification:

```bash
uv run pytest tests/service/integrations/crm/records/hubspot/deals tests/service/integrations/crm/records/salesforce/deals -q
```

## Loophole Review For The Rewritten Plan

These are the main remaining ways the design can fail if implementation is
careless:

1. **Creation vs update confusion**

   A created deal with initial stage Proposal should not be treated as a stage
   changed from null to Proposal unless product explicitly wants that trigger.
   Keep creation workflows separate from update workflows.

2. **Triggerable-field explosion**

   Making every deal field a workflow semantic event will create noisy workflow
   configuration and expensive dispatch. Start with a small registry and add
   fields only when product exposes that trigger.

3. **Audit-only updates still need history**

   If a user updates only description or expected close date, the event may have
   no semantic workflow types. That is fine. It is still useful CRM history.

4. **One API save must stay atomic**

   Frontend and agents should not split five field changes into five API calls.
   The backend should write one deal update and one mutation event, then fan out
   only at the dispatch layer.

5. **Same-value and omitted-field handling**

   A PATCH field not present, a present field with the same value, and a present
   field set to null are different. The diff helper must handle all three.

6. **Workflow-triggered write loops**

   Workflow writes must use explicit causality fields or workflow runtime links,
   not ambiguous direct run IDs on the CRM event. Add deterministic idempotency
   keys before workflow-originated writes are allowed to trigger further
   workflows.

7. **Parent dispatch status is not enough**

   Parent `crm_mutation_events.dispatch_status` cannot represent three matched
   workflows where two enqueue and one fails. Child dispatch rows are mandatory
   before real fanout.

8. **Stale updates and concurrent updates**

   A stale frontend save or agent approval can overwrite newer deal state. The
   mutation command needs expected values/revision support before enabling broad
   edits from cards or long-running approvals.

9. **Provider sync volume**

   Do not route provider initial sync through pending workflow dispatch by
   default. Provider sync can be recorded later with coalescing and opt-in
   trigger semantics.

10. **Source-specific authorization**

   Frontend, agent, bot, workflow, import, and provider sync do not have the
   same authorization rules. They can share the event contract, but not blindly
   share every validation path.

## Testing Requirements

Minimum focused tests before shipping:

1. Frontend stage API writes one stage history row and one pending mutation event.
2. Agent lifecycle stage write produces the same mutation event shape as frontend.
3. Bot form stage write does not trust form org metadata and still emits event
   under the resolved user's organisation.
4. No-op stage write does not emit a workflow-triggering event.
5. Reusing an idempotency key with a different payload hash is rejected.
6. Concurrent stage updates cannot both create conflicting "from same old stage"
   histories.
7. A stale stage form with `expected_stage_id` mismatch is rejected or requires
   explicit reconfirmation.
8. Close-lost with stage change emits one lifecycle event containing all changed
   fields, not multiple disconnected field events.
9. Dispatcher marks events idempotently and does not double-enqueue the same
   workflow on retry.
10. Two dispatcher workers cannot claim and enqueue the same event/trigger.
11. External CRM deals remain read-only for frontend/agent/bot writes.

## Non-Goals

- Do not build a full workflow engine in the first slice.
- Do not move provider sync into the internal CRM write policy path.
- Do not trigger LLM workflows inside the DB transaction.
- Do not encode detailed CRM write policy in the GeneralAgent prompt.
- Do not replace `CRMDealStageHistory`; it still serves reporting/timeline needs.

## Recommended First Slice

Start with `move_stage()` only.

Reason:

- Stage movement is the user's explicit trigger example.
- Current stage mutation already has a narrow helper surface:
  `_apply_stage_update()` and `_record_stage_history()`.
- API, agent lifecycle, and bot form flows already converge on `move_stage()`.
- The event shape can be proven without changing scalar field semantics.

The first slice is only production-valid if it includes:

- no-op stage detection
- row lock or expected-stage guard
- idempotency payload hash
- one pending outbox event after a real stage change
- no dispatcher fanout until claim/idempotency tests exist

Once stage changes work end-to-end, expand the same contract to close/reopen and
safe scalar updates.
