# HubSpot Tasks Webhooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HubSpot task webhook support by routing generic HubSpot `object.*` task events into the existing single-task fetch and `crm_activities` persistence path.

**Architecture:** HubSpot sends task webhooks as generic object events with `subscriptionType = object.*` and `objectTypeId = 0-27`. The backend should normalize those provider events into internal `task.*` events before dispatch, coalesce non-delete bursts by task object id, fetch the latest task snapshot including associations, and persist through `HubSpotTaskRecordSyncService.persist_raw_task(...)`. Deletes stay immediate and idempotently soft-delete only the matched local task activity.

**Tech Stack:** Python 3.11, FastAPI webhook ingress, Redis dedupe/debounce, Celery integrations worker, SQLModel/PostgreSQL tenant schemas, HubSpot CRM v3 Tasks API.

---

## Current Repo Grounding

The task sync path already has the main pieces needed for webhook-driven single-record updates:

- `src/workers/integrations_worker/providers/crm/hubspot/tasks_fetcher.py`
  - Fetches HubSpot task pages from `/crm/v3/objects/tasks`.
  - Already requests `hs_task_subject`, `hs_task_body`, `hs_task_status`, `hs_timestamp`, `hubspot_owner_id`, `hs_createdate`, and `hs_lastmodifieddate`.
  - Already requests `deals`, `contacts`, and `companies` associations.
- `src/workers/integrations_worker/providers/crm/hubspot/single_task_fetcher.py`
  - Fetches one task by id.
  - Returns `None` on HubSpot 404.
  - Raises for non-404 provider errors, which is correct because only 404 should tombstone local data.
- `src/service/integrations/crm/records/hubspot/tasks/task_sync_service.py`
  - Exposes `persist_raw_task(...)` for one raw provider task snapshot.
  - Uses fresh-session boundaries: credentials are loaded before provider calls, and writes happen in org-scoped sessions.
- `src/service/integrations/crm/records/hubspot/tasks/task_writer.py`
  - `apply_single(...)` upserts one task and does not deactivate unrelated tasks.
  - Stale-write protection uses `source_modified_at`, derived from `hs_lastmodifieddate`.
  - Existing rows are scoped by `organisation_id`, `integration_id`, and task/todo activity type.
- `src/service/integrations/webhooks/hubspot/ingestion_service.py`
  - Already owns webhook filtering, dedupe, same-object coalescing, Redis debounce, and queue fanout.
  - Notes webhook support just added the generic-object normalization pattern that tasks should reuse.

## HubSpot Facts

- HubSpot Tasks object type id is `0-27`.
- Task webhooks should use expanded/generic webhook support.
- Generic task webhook input events use `object.creation`, `object.propertyChange`, `object.deletion`, and `object.associationChange`.
- The task body/status/due-date fields are provider properties fetched from the Tasks API.

Sources:

- HubSpot Tasks API guide: `https://developers.hubspot.com/docs/api-reference/crm-tasks-v3/guide`
- HubSpot object type ids: `https://developers.hubspot.com/docs/api-reference/latest/crm/using-object-apis`

## Production Rules

- Tasks are HubSpot CRM objects with object type id `0-27`.
- HubSpot task webhook input events are generic `object.*` events, not legacy `task.*` events.
- Internal code may use synthetic `task.*` subscription types after normalization.
- The original provider payload must remain available in `event.raw` for logging, dedupe evidence, and debugging.
- Non-task generic object events must not be claimed by task normalization.
- Webhook-driven task upserts must fetch a fresh full task snapshot from HubSpot instead of patching the changed field from the webhook payload.
- The single-task fetch must request task properties and associations needed by the writer.
- Deletions must not be delayed by debounce.
- Deletions must be idempotent: missing or already-inactive local rows still count as successfully handled.
- A HubSpot 404 during task upsert should soft-delete the local task.
- Provider errors other than 404 must not soft-delete local data.
- Stale-write protection should continue using `hs_lastmodifieddate` through `source_modified_at`.

## HubSpot App Configuration

Configure the HubSpot app using expanded or generic webhook support for the Task object.

Subscribe to:

- Created
- Deleted
- Property changed
- Association changed

Property-change allow-list:

- `hs_task_subject`
- `hs_task_body`
- `hs_task_status`
- `hs_task_priority`
- `hs_task_type`
- `hs_timestamp`
- `hubspot_owner_id`

Do not subscribe to:

- `hs_lastmodifieddate`

Keep `hs_lastmodifieddate` as a fetched provider property for stale-snapshot ordering.

Deployment check before rollout:

- Confirm the HubSpot app has the required task/activity read scope for fetching tasks.
- If the app scope changes, existing installs may need reauthorization before webhooks can fetch task snapshots successfully.
- If webhook delivery succeeds but task fetches return 403, treat that as app authorization/scope misconfiguration, not as data deletion.

## File Map

- Modify: `src/service/integrations/webhooks/hubspot/tasks/subscriptions.py`
  - Add task object type id, generic object subscription map, synthetic internal task event names, and task property allow-list.
- Create: `src/service/integrations/webhooks/hubspot/tasks/handler.py`
  - Handle task create/update/association-change/delete events.
- Modify: `src/service/integrations/webhooks/hubspot/subscriptions.py`
  - Route normalized `task.*` events to `HubSpotTaskWebhookHandler`.
- Modify: `src/service/integrations/webhooks/hubspot/ingestion_service.py`
  - Normalize generic task events, apply task property allow-list, add task event families to batch coalescing and Redis refresh debounce, and keep deletions immediate.
- Modify: `src/workers/integrations_worker/providers/crm/hubspot/tasks_fetcher.py`
  - Confirm or add task properties needed by the webhook plan.
- Modify: `src/service/integrations/crm/records/hubspot/tasks/task_normalizer.py`
  - Confirm priority/type/reminder fields are preserved in `properties`; only map canonical fields if product needs them.
- Modify: `docs/API Documentation/integrations-api.md`
  - Document supported HubSpot task webhook behavior and operational setup.
- Add tests under `tests/api/routers/integrations/`, `tests/service/integrations/webhooks/hubspot/`, and existing task sync/fetcher test locations.

## Internal Event Contract

Normalize provider events with:

```python
subscriptionType in {
    "object.creation",
    "object.propertyChange",
    "object.deletion",
    "object.associationChange",
}
objectTypeId == "0-27"
```

into internal subscription types:

```python
object.creation -> task.creation
object.propertyChange -> task.propertyChange
object.deletion -> task.deletion
object.associationChange -> task.associationChange
```

Keep the original provider payload in `event.raw`, for example:

```python
{
    "subscriptionType": "object.propertyChange",
    "objectTypeId": "0-27",
    "objectId": "123456789",
    "portalId": 12345,
    "propertyName": "hs_task_status",
    "changeSource": "...",
    "eventId": ...,
    "occurredAt": ...
}
```

The queued task kwargs should contain:

```python
{
    "subscription_type": "task.propertyChange",
    "portal_id": "12345",
    "object_id": "123456789",
    "event_id": "...",
    "occurred_at": "...",
    "event_payload": original_provider_payload,
}
```

Association-change payloads may include related-object fields such as `toObjectTypeId`, `toObjectId`, `associationTypeId`, and `associationRemoved`. The handler should not infer final task state from those fields. It should fetch the latest task snapshot and let the existing task writer refresh associations from the provider snapshot.

## Debounce and Dedupe Contract

Dedupe exact webhook event replays using the existing HubSpot webhook dedupe path.

Coalesce refresh events using an object-family-specific key. The current route-level implementation derives object family from the internal subscription type, so task events should naturally use:

```text
webhook:hubspot:refresh:task:{portal_id}:{object_id}
```

Do not use only `object_id`; HubSpot object ids can collide across object families.

Coalesced task events:

- `task.creation`
- `task.propertyChange`
- `task.associationChange`

Immediate task events:

- `task.deletion`

When a delete is received:

- Release any pending refresh claim for the same task key.
- Queue the deletion immediately.
- Acknowledge duplicate deletions without queueing duplicate work.

## Task 1: Add HubSpot Task Webhook Subscriptions

**Files:**

- Create or modify: `src/service/integrations/webhooks/hubspot/tasks/subscriptions.py`
- Test: `tests/service/integrations/webhooks/hubspot/tasks/test_subscriptions.py`

- [ ] **Step 1: Add task constants**

Add constants equivalent to:

```python
HUBSPOT_TASK_OBJECT_TYPE_ID = "0-27"

TASK_CREATION = "task.creation"
TASK_PROPERTY_CHANGE = "task.propertyChange"
TASK_DELETION = "task.deletion"
TASK_ASSOCIATION_CHANGE = "task.associationChange"

HUBSPOT_GENERIC_TASK_SUBSCRIPTION_MAP = {
    HUBSPOT_GENERIC_CREATION: TASK_CREATION,
    HUBSPOT_GENERIC_PROPERTY_CHANGE: TASK_PROPERTY_CHANGE,
    HUBSPOT_GENERIC_DELETION: TASK_DELETION,
    HUBSPOT_GENERIC_ASSOCIATION_CHANGE: TASK_ASSOCIATION_CHANGE,
}

TASK_PROPERTY_CHANGE_PROPERTIES = (
    "hs_task_subject",
    "hs_task_body",
    "hs_task_status",
    "hs_task_priority",
    "hs_task_type",
    "hs_timestamp",
    "hubspot_owner_id",
)
TASK_PROPERTY_CHANGE_PROPERTY_SET = frozenset(TASK_PROPERTY_CHANGE_PROPERTIES)

TASK_SUBSCRIPTIONS = (
    TASK_CREATION,
    TASK_PROPERTY_CHANGE,
    TASK_DELETION,
    TASK_ASSOCIATION_CHANGE,
)
```

Reuse generic object constants from the notes subscriptions module or move those generic constants into a shared module. Do not duplicate conflicting definitions.

- [ ] **Step 2: Add subscription tests**

Cover:

- Task object type id is `0-27`.
- Generic `object.*` events map to internal `task.*` events.
- `hs_lastmodifieddate` is not in the task property-change allow-list.
- `hs_timestamp` is in the allow-list.

Run:

```bash
uv run pytest tests/service/integrations/webhooks/hubspot/tasks/test_subscriptions.py -v
```

Expected:

```text
passed
```

## Task 2: Normalize Generic Task Events in Ingestion

**Files:**

- Modify: `src/service/integrations/webhooks/hubspot/ingestion_service.py`
- Test: `tests/api/routers/integrations/test_hubspot_webhook_routes.py`

- [ ] **Step 1: Extend generic object normalization**

Update the existing generic-object normalization helper so:

- `objectTypeId == "0-46"` still maps to notes.
- `objectTypeId == "0-27"` maps to tasks.
- Unsupported generic object types fall through as unsupported generic events and are acknowledged without queueing.
- The original provider payload remains in `event.raw`.

- [ ] **Step 2: Add task property allow-list filtering**

For normalized `task.propertyChange` events:

- Dispatch only if `propertyName` is in `TASK_PROPERTY_CHANGE_PROPERTY_SET`.
- Acknowledge unsupported task property changes without queueing.
- Log at debug/info level with property name and object id.

- [ ] **Step 3: Add route tests**

Add tests to `tests/api/routers/integrations/test_hubspot_webhook_routes.py`:

- Generic `object.creation` + `objectTypeId = 0-27` queues `task.creation`.
- Generic `object.propertyChange` for `hs_task_status` queues `task.propertyChange`.
- Generic `object.propertyChange` for `hs_lastmodifieddate` is acknowledged and not queued.
- Generic `object.creation` with a non-task/non-note object type is acknowledged and not queued.
- The queued event keeps the original provider payload in `event_payload`.

Run:

```bash
uv run pytest tests/api/routers/integrations/test_hubspot_webhook_routes.py -v
```

Expected:

```text
passed
```

## Task 3: Add Task Debounce and Delete Bypass

**Files:**

- Modify: `src/service/integrations/webhooks/hubspot/ingestion_service.py`
- Test: `tests/api/routers/integrations/test_hubspot_webhook_routes.py`

- [ ] **Step 1: Extend event family sets**

Add task events to:

- `_CREATION_SUBSCRIPTION_TYPES`
- `_DELETION_SUBSCRIPTION_TYPES`
- `_PROPERTY_CHANGE_SUBSCRIPTION_TYPES`
- `_ASSOCIATION_CHANGE_SUBSCRIPTION_TYPES`
- `_COALESCED_SUBSCRIPTION_TYPES`
- `_DELAYED_REFRESH_SUBSCRIPTION_TYPES`

- [ ] **Step 2: Keep task deletions immediate**

For `task.deletion`:

- Do not debounce.
- Release pending refresh for the same task key.
- Queue immediately unless the exact event is duplicate.

- [ ] **Step 3: Add debounce tests**

Cover:

- Task create plus task property-change burst coalesces to one queued refresh.
- Task association-change plus property-change burst coalesces to one queued refresh.
- Task events do not coalesce with note/deal/company/contact events that share the same numeric object id.
- Duplicate task webhook events are acknowledged without queueing.
- Task deletion is queued immediately and has no countdown.

Run:

```bash
uv run pytest tests/api/routers/integrations/test_hubspot_webhook_routes.py -v
```

Expected:

```text
passed
```

## Task 4: Add HubSpot Task Webhook Handler

**Files:**

- Create: `src/service/integrations/webhooks/hubspot/tasks/handler.py`
- Modify: `src/service/integrations/webhooks/hubspot/subscriptions.py`
- Test: `tests/service/integrations/webhooks/hubspot/tasks/test_handler.py`
- Test: `tests/service/integrations/webhooks/hubspot/test_dispatcher.py`

- [ ] **Step 1: Implement handler routing**

Create `HubSpotTaskWebhookHandler` with:

- `task.creation` -> `_handle_upsert`
- `task.propertyChange` -> `_handle_upsert`
- `task.associationChange` -> `_handle_upsert`
- `task.deletion` -> `_handle_deletion`

- [ ] **Step 2: Implement upsert handling**

For upsert events:

- Resolve portal id to org/integration targets with `iter_hubspot_integrations_for_portal(...)`.
- Load HubSpot token context through `HubSpotTaskRecordSyncService._load_context(...)`.
- Fetch current task snapshot using `HubSpotSingleTaskFetcher`.
- If fetch returns a raw task, call `HubSpotTaskRecordSyncService.persist_raw_task(...)`.
- If fetch returns `None`, soft-delete the local task.
- Do not catch 401, 403, 429, timeout, or server errors as deletion.

- [ ] **Step 3: Implement deletion handling**

For delete events:

- Do not call HubSpot.
- Soft-delete only rows matching:
  - `organisation_id`
  - `integration_id`
  - `external_id`
  - `activity_type in (TASK, TODO)`
  - `is_deleted = false`
- Treat no row found as success.
- Treat already-inactive row as success.

- [ ] **Step 4: Wire subscriptions**

Add `WebhookSubscription(...)` entries for:

- `TASK_CREATION`
- `TASK_PROPERTY_CHANGE`
- `TASK_ASSOCIATION_CHANGE`
- `TASK_DELETION`

- [ ] **Step 5: Add handler and dispatcher tests**

Cover:

- Dispatcher recognizes each `task.*` event.
- Dispatcher does not route generic `object.*` directly.
- Task creation fetches one task and persists it.
- Task property change fetches one task and persists it.
- Task association change fetches one task and persists it.
- 404 during upsert soft-deletes the matching local task.
- 401/403/429/timeout during upsert does not soft-delete the local task.
- Deletion soft-deletes only matching task/todo activity rows.
- Deletion does not soft-delete notes or other activities with the same external id.
- Repeated deletion succeeds when the row is already inactive or absent.

Run:

```bash
uv run pytest \
  tests/service/integrations/webhooks/hubspot/test_dispatcher.py \
  tests/service/integrations/webhooks/hubspot/tasks/test_handler.py \
  -v
```

Expected:

```text
passed
```

## Task 5: Confirm Single-Task Fetch and Writer Contracts

**Files:**

- Modify if needed: `src/workers/integrations_worker/providers/crm/hubspot/tasks_fetcher.py`
- Modify if needed: `src/service/integrations/crm/records/hubspot/tasks/task_normalizer.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_single_activity_fetchers.py`
- Test: `tests/service/integrations/crm/records/hubspot/tasks/test_task_writer.py`

- [ ] **Step 1: Confirm fetched task properties**

`HUBSPOT_TASK_PROPERTIES` should include:

```python
[
    "hs_task_subject",
    "hs_task_body",
    "hs_task_status",
    "hs_task_priority",
    "hs_task_type",
    "hs_timestamp",
    "hubspot_owner_id",
    "hs_createdate",
    "hs_lastmodifieddate",
]
```


- [ ] **Step 2: Confirm associations**

`HubSpotSingleTaskFetcher` should keep requesting:

- Deals
- Contacts
- Companies

- [ ] **Step 3: Confirm writer behavior**

Existing `HubSpotTaskWriter.apply_single(...)` should already:

- Upsert only the fetched task.
- Not deactivate unrelated tasks.
- Reactivate inactive tasks on upsert.
- Refresh deal/contact/company links from fetched associations.
- Skip stale snapshots using `source_modified_at`.

Add focused tests only for missing coverage, not duplicate coverage.

Run:

```bash
uv run pytest \
  tests/workers/integrations_worker/providers/crm/hubspot/test_single_activity_fetchers.py \
  tests/service/integrations/crm/records/hubspot/tasks/test_task_writer.py \
  -v
```

Expected:

```text
passed
```

## Task 6: Document API and Operational Behavior

**Files:**

- Modify: `docs/API Documentation/integrations-api.md`

- [ ] **Step 1: Add HubSpot task webhook section**

Document:

- HubSpot sends tasks as generic `object.*` events.
- The backend supports task object type id `0-27`.
- Supported task event types are creation, deletion, property change, and association change.
- `hs_lastmodifieddate` is fetched but not subscribed to.
- Deletion is immediate and idempotent.
- Create/property/association events fetch a full task snapshot before writing.

- [ ] **Step 2: Add deployment checklist**

Document:

- Enable expanded/generic webhook support for Task in HubSpot.
- Verify task/activity read scopes.
- Reauthorize installs if scopes changed.
- Keep polling/reconciliation enabled as safety net for missed webhooks.

## Task 7: Run Focused Verification

**Files:**

- No code changes.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest \
  tests/api/routers/integrations/test_hubspot_webhook_routes.py \
  tests/service/integrations/webhooks/hubspot/test_dispatcher.py \
  tests/service/integrations/webhooks/hubspot/tasks/test_handler.py \
  tests/workers/integrations_worker/providers/crm/hubspot/test_single_activity_fetchers.py \
  tests/service/integrations/crm/records/hubspot/tasks/test_task_writer.py \
  -v
```

Expected:

```text
passed
```

- [ ] **Step 2: Run lint and type checks before PR**

Only run these when preparing to push or create a PR:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

Expected:

```text
All checks passed
Success: no issues found
```

## Rollout Notes

- Existing polling/reconciliation remains the safety net for missed HubSpot task webhooks.
- No task merge or restore handling is included in this pass.
- Unsupported generic object events should be observable in logs/metrics but should not fail the webhook request.
- If HubSpot app scopes change, code deployment alone is not enough; existing installations may need reauthorization.
- If app scopes do not change, webhook subscription configuration can be rolled out without requiring users to reconnect.

## Review Checklist

- [ ] Generic `object.*` task events normalize into internal `task.*` events before dispatcher routing.
- [ ] Original provider payload is retained in raw event data.
- [ ] Generic note/task/object normalization does not misclassify unsupported object types.
- [ ] Task property-change allow-list is enforced.
- [ ] Task refresh debounce key includes object family.
- [ ] Task deletion bypasses debounce.
- [ ] Task deletion is idempotent.
- [ ] Upsert 404 soft-deletes the local task.
- [ ] Upsert 401/403/429/timeouts do not soft-delete.
- [ ] Single-task fetch requests task associations.
- [ ] Single-task writer does not deactivate unrelated tasks.
- [ ] Stale-write guard uses provider modification timestamp.
- [ ] All database writes preserve organisation and integration isolation.
