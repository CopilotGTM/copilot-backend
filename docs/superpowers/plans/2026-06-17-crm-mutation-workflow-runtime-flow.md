# CRM Mutation Workflow Runtime Flow

## Purpose

Show what happens after a CRM write, from deal creation through any deal value
update. This flow applies whether the write starts from frontend, agent, bot
form, or a future workflow, as long as the caller uses the canonical CRM mutation
write path.

The core rule is:

```text
One user/API/business action = one crm_mutation_events row.
One crm_mutation_events row = zero, one, or many workflow dispatch rows.
```

## Example: Frontend Creates A Deal

User action:

```text
User creates Deal A with name, amount, expected close date, account, contacts,
and initial stage.
```

Runtime flow:

```text
Frontend
  -> POST /users/crm/deals
  -> API handler
  -> DealWriteService.apply_deal_mutation(operation=create)
  -> crm_deals is inserted
  -> related deal association rows are inserted when needed
  -> crm_mutation_events is inserted once
  -> DB transaction commits
  -> API returns success to frontend
  -> dispatcher task is queued after commit
  -> dispatcher claims the creation event
  -> dispatcher matches crm.deal.created workflows
  -> dispatcher creates per-workflow dispatch rows
  -> dispatcher queues workflow run jobs
```

Creation event shape:

```text
event_type = crm.deal.created
operation = create
semantic_event_types = [crm.deal.created]
changed_fields.name.old = null
changed_fields.name.new = Acme Expansion
changed_fields.stage_id.old = null
changed_fields.stage_id.new = Discovery
changed_fields.amount.old = null
changed_fields.amount.new = 50000
```

Creation should not fake a stage-change event. A workflow like "new deal created
in Proposal" should be modeled as:

```text
When crm.deal.created and initial stage = Proposal
```

not:

```text
When crm.deal.stage_changed from null to Proposal
```

## Example: Frontend Moves Deal Stage

User action:

```text
User moves Deal A from Discovery to Proposal.
```

Runtime flow:

```text
Frontend
  -> PATCH /users/crm/deals/{deal_id}/stage
  -> API handler
  -> DealWriteService.apply_deal_mutation()
  -> crm_deals is updated
  -> crm_deal_stage_history is inserted
  -> crm_mutation_events is inserted
  -> DB transaction commits
  -> API returns success to frontend
  -> dispatcher task is queued after commit
  -> dispatcher claims crm_mutation_events row
  -> dispatcher matches workflow triggers
  -> dispatcher creates per-workflow dispatch rows
  -> dispatcher queues workflow run jobs
  -> workflow worker executes each workflow job
```

The frontend response should mean:

```text
The CRM write is committed and workflow dispatch has been accepted or made
recoverable through the outbox.
```

It should not mean:

```text
Every downstream workflow step has already completed.
```

## Example: Frontend Updates Five Deal Fields

User action:

```text
User updates stage, expected close date, amount, priority, and description,
then clicks Save once.
```

Runtime flow:

```text
Frontend
  -> PATCH /users/crm/deals/{deal_id}
  -> API handler
  -> DealWriteService.apply_deal_mutation()
  -> crm_deals is updated once
  -> crm_mutation_events is inserted once
  -> DB transaction commits
  -> API returns success to frontend
  -> dispatcher claims the one mutation event
  -> dispatcher evaluates semantic_event_types and changed_fields
  -> dispatcher creates workflow dispatch rows only for matched triggers
```

The frontend should not split this into five API calls. The user performed one
business action, so the audit ledger should show one mutation with five changed
fields.

## Example: Frontend Updates Only Audit-Only Fields

User action:

```text
User updates expected close date and description, then clicks Save once.
```

Runtime flow:

```text
Frontend
  -> PATCH /users/crm/deals/{deal_id}
  -> API handler
  -> DealWriteService.apply_deal_mutation(operation=update_fields)
  -> crm_deals is updated once
  -> crm_mutation_events is inserted once
  -> DB transaction commits
  -> API returns success to frontend
  -> dispatcher claims the one mutation event
  -> dispatcher finds no triggerable semantic event types
  -> parent event is marked ignored/dispatched-with-no-matches
```

Audit-only event shape:

```text
event_type = crm.deal.updated
operation = update_fields
semantic_event_types = []
changed_fields.expected_close_date.old = 2026-07-01
changed_fields.expected_close_date.new = 2026-08-01
changed_fields.description.old = Old notes
changed_fields.description.new = Updated notes
```

This event is still valuable CRM history even when no workflow runs.

## Synchronous Request Boundary

The API request should do only the durable CRM write:

1. Validate user/org/deal access, or create permissions for deal creation.
2. Validate the payload against the operation-specific rules.
3. Call `DealWriteService.apply_deal_mutation()`.
4. Insert or update `crm_deals`.
5. Insert operation-specific domain rows when needed, such as
   `crm_deal_stage_history` for a real stage move.
6. Insert one `crm_mutation_events` row with:
   - `event_type = crm.deal.created` for creation
   - `event_type = crm.deal.stage_changed` for a stage-only action
   - `event_type = crm.deal.updated` for a multi-field update action
   - `semantic_event_types` for triggerable changes inside the action
   - operation such as `create`, `move_stage`, `update_fields`, `close`, or `reopen`
   - `deal_id = ...`
   - `changed_fields` containing exact old/new values
7. Commit.
8. Return the updated deal to the frontend.

No workflow should run inside this DB transaction.

## Asynchronous Dispatch Boundary

After commit, dispatch can happen in two ways:

1. Fast path: the API handler queues one dispatcher task for the committed event.
2. Recovery path: a scheduled retry task scans future workflow runtime/evaluation
   rows, or reconciles recent CRM mutation events into those rows.

Both paths call the same dispatcher service. The dispatcher must claim child
runtime rows with row locking or an atomic status update so two workers do not
dispatch the same workflow trigger.

Claim flow:

```text
pending -> dispatching -> enqueued
pending -> dispatching -> failed
pending -> dispatching -> ignored
```

`enqueued` means trigger evaluation and workflow enqueue completed. It does not
mean the workflow business actions completed.

## Workflow Matching

The dispatcher reads the event:

```text
event_type = crm.deal.updated
semantic_event_types = [crm.deal.stage_changed, crm.deal.amount_changed]
organisation_id = org-1
deal_id = deal-1
changed_fields.stage_id.old = Discovery
changed_fields.stage_id.new = Proposal
changed_fields.amount.old = 50000
changed_fields.amount.new = 65000
source = frontend
```

Then it queries the workflow trigger tables, for example:

```text
Find enabled workflow triggers where:
  organisation_id = org-1
  trigger_event_type is in event.semantic_event_types
  conditions match changed_fields
  source filter allows frontend
```

For each matched trigger, create one child dispatch row:

```text
crm_mutation_event_dispatches
  event_id = mutation-event-1
  workflow_trigger_id = trigger-1
  matched_event_type = crm.deal.stage_changed
  matched_field = stage_id
  workflow_run_id = workflow-run-1
  status = pending/enqueued
  idempotency_key = mutation-event-1:trigger-1
```

This child table is required because one stage change can match zero, one, or
many workflows.

For a creation event, the matcher reads:

```text
event_type = crm.deal.created
semantic_event_types = [crm.deal.created]
changed_fields.stage_id.old = null
changed_fields.stage_id.new = Proposal
```

Then it evaluates creation triggers, not stage-change triggers.

## Worker Count For One CRM Mutation

For one CRM mutation event, the worker count is:

```text
API worker/process:
  1 request handler inserts/updates CRM rows and inserts the mutation event.

Celery dispatcher task:
  1 task claims the mutation event and evaluates workflow triggers.

Workflow execution tasks:
  N tasks, where N = number of matched workflow triggers.
```

Examples:

```text
No matching workflows:
  API request + 1 dispatcher task + 0 workflow tasks

One matching workflow:
  API request + 1 dispatcher task + 1 workflow task

Three matching workflows:
  API request + 1 dispatcher task + 3 workflow tasks
```

Initially these tasks can run on `deal_worker` because this is CRM/deal-owned
work. If workflow execution becomes broad or heavy, move workflow execution to a
dedicated workflow worker while keeping CRM mutation creation in the API/service
layer.

## Agent And Bot Form Writes

Agent and bot form writes should enter the same flow at the service boundary:

```text
Agent tool or bot form handler
  -> DealWriteService.apply_deal_mutation()
  -> same crm_deals update
  -> same crm_deal_stage_history insert
  -> same crm_mutation_events insert
  -> same dispatcher
  -> same workflow trigger matcher
```

Only `source`, `actor_user_id`, `approval_state_id`, `correlation_id`, and
future workflow causality metadata differ.

## Failure Behavior

If the CRM write fails:

```text
No commit.
No mutation event.
No workflow dispatch.
Frontend receives an error.
```

If the CRM write commits but dispatcher enqueue fails:

```text
CRM state is still correct.
crm_mutation_events remains pending.
Retry scanner picks it up later.
Frontend should not roll back the CRM write.
```

If one of three workflows fails to enqueue:

```text
The parent event should not hide partial failure.
Successful child dispatch rows stay enqueued.
Failed child dispatch rows keep their own failed status and retry metadata.
```

This is why `crm_mutation_event_dispatches` is needed before real workflow
fanout.

## Loop Prevention

If a workflow updates the same deal later:

```text
source = workflow
caused_by_workflow_run_id = current workflow run
caused_by_source_domain = crm | meeting | email | form
caused_by_source_event_id = original source event
idempotency_key = workflow-run-id:operation:deal-id:field
```

The trigger matcher must block or ignore rules that would immediately re-trigger
the same workflow on the same field with the same value.

## Minimal Production V1

The first production-valid workflow slice should support:

1. Deal creation emits `crm.deal.created`.
2. Stage change emits `crm.deal.stage_changed`.
3. Multi-field updates emit one `crm.deal.updated` event.
4. Audit-only updates do not create workflow runs.
5. Dispatcher claims one pending event safely.
6. Trigger matcher uses `semantic_event_types` and `changed_fields`.
7. One child dispatch row is created per matched workflow.
8. One workflow job is enqueued per child dispatch row.
9. Retry task can recover pending/failed dispatches.
10. No workflow runs inside the CRM write transaction.
