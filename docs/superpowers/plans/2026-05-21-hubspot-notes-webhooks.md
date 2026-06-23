# HubSpot Notes Webhooks Implementation Plan

> Historical note: this plan predates the task/note source-table migration.
> Current HubSpot note writes should target `crm_notes` / `CRMNote` and update
> `crm_activities` only through feed projection.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HubSpot note webhook support by routing generic HubSpot `object.*` note events into the existing single-note fetch and `crm_activities` persistence path.

**Architecture:** HubSpot sends note webhooks as generic object events with `subscriptionType = object.*` and `objectTypeId = 0-46`. The backend should normalize those provider events into internal `note.*` events before dispatch, coalesce non-delete bursts by note object id, fetch the latest note snapshot including associations, and persist through the existing single-note sync service. Deletes stay immediate and idempotently soft-delete only the matched local note activity.

**Tech Stack:** Python 3.11, FastAPI webhook ingress, Redis dedupe/debounce, Celery integrations worker, SQLModel/PostgreSQL tenant schemas, HubSpot CRM v3 Notes API.

---

## Production Rules

- Notes are HubSpot CRM objects with object type id `0-46`.
- HubSpot note webhook input events are generic `object.*` events, not legacy `note.*` events.
- Internal code may use synthetic `note.*` subscription types after normalization.
- The original provider payload must remain available in `event.raw` for logging, dedupe evidence, and debugging.
- Non-note generic object events must be acknowledged but not dispatched unless already supported by the current webhook pipeline.
- Webhook-driven note upserts must fetch a fresh full note snapshot from HubSpot instead of patching the changed field from the webhook payload.
- The single-note fetch must request note properties and associations needed by the writer.
- Deletions must not be delayed by debounce.
- Deletions must be idempotent: missing or already-inactive local rows still count as successfully handled.
- A HubSpot 404 during note upsert should soft-delete the local note.
- Provider errors other than 404 must not soft-delete local data.
- Stale-write protection should use provider modification time, not the note timeline timestamp alone.

## HubSpot App Configuration

Configure the HubSpot app using expanded or generic webhook support for the Note object.

Subscribe to:

- Created
- Deleted
- Property changed
- Association changed

Property-change allow-list:

- `hs_note_body`
- `hubspot_owner_id`
- `hs_attachment_ids`
- `hs_timestamp`

Do not subscribe to:

- `hs_lastmodifieddate`

Keep `hs_lastmodifieddate` as a fetched provider property for stale-snapshot ordering.

Deployment check before rollout:

- Confirm the HubSpot app has the required note/activity read scope for fetching notes.
- If the app scope changes, existing installs may need reauthorization before webhooks can fetch note snapshots successfully.
- If webhook delivery succeeds but note fetches return 403, treat that as app authorization/scope misconfiguration, not a data deletion signal.

## File Map

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/webhook_constants.py`
  - Own HubSpot webhook subscription constants, note object type id, synthetic internal note event names, and note property allow-list.
- Modify: `src/api/routers/integrations/hubspot_webhook_routes.py`
  - Normalize generic note events before queueing/dispatch and keep original payload in raw event data.
- Modify: `src/service/integrations/hubspot_webhook_ingestion_service.py`
  - Apply dedupe, same-object coalescing, deletion bypass, and unsupported generic-event acknowledgement.
- Modify: `src/workers/integrations_worker/providers/crm/hubspot/dispatcher.py`
  - Route normalized `note.*` events to the note webhook handler.
- Create: `src/workers/integrations_worker/providers/crm/hubspot/note_webhook_handler.py`
  - Handle note create/update/association-change/delete events.
- Modify: `src/workers/integrations_worker/providers/crm/hubspot/single_note_fetcher.py`
  - Ensure one-note fetch requests required properties and associations.
- Modify: `src/workers/integrations_worker/providers/crm/hubspot/note_record_sync_service.py`
  - Ensure upsert reactivates inactive notes, refreshes associations, and stale snapshots are skipped safely.
- Modify or add tests under `tests/`
  - Cover normalization, ingestion, debounce, dispatcher routing, handler behavior, deletion idempotency, and writer regressions.
- Modify: `docs/API Documentation/integrations-api.md`
  - Document supported HubSpot note webhook behavior and operational setup.

## Internal Event Contract

Normalize provider events with:

```python
subscriptionType in {
    "object.creation",
    "object.propertyChange",
    "object.deletion",
    "object.associationChange",
}
objectTypeId == "0-46"
```

into internal subscription types:

```python
object.creation -> note.creation
object.propertyChange -> note.propertyChange
object.deletion -> note.deletion
object.associationChange -> note.associationChange
```

Keep these provider fields in raw payload:

```python
{
    "subscriptionType": "object.propertyChange",
    "objectTypeId": "0-46",
    "objectId": "123456789",
    "portalId": 12345,
    "propertyName": "hs_note_body",
    "changeSource": "...",
    "eventId": ...,
    "occurredAt": ...
}
```

Association-change payloads may also include:

```python
{
    "toObjectTypeId": "0-3",
    "toObjectId": "987654321",
    "associationTypeId": 214,
    "associationRemoved": false
}
```

The handler should not try to infer note state from association payload details. It should fetch the latest note snapshot and let the existing note writer replace association state from the provider snapshot.

## Debounce and Dedupe Contract

Dedupe exact webhook event replays using a key that includes provider event identity when present:

```text
hubspot:webhook:dedupe:{portal_id}:{event_id}
```

For events without a reliable event id, use a stable hash over provider, portal id, subscription type, object type id, object id, occurred timestamp, property name, and association payload fields.

Coalesce refresh events using a key that includes object namespace:

```text
hubspot:webhook:refresh:{integration_id}:note:{object_id}
```

Do not use only `object_id`; HubSpot object ids can collide across object families.

Coalesced note events:

- `note.creation`
- `note.propertyChange`
- `note.associationChange`

Immediate note events:

- `note.deletion`

When a delete is received:

- Cancel or release any pending refresh claim for the same note key.
- Queue the deletion immediately.
- Acknowledge duplicate deletions without queueing duplicate work.

## Task 1: Add HubSpot Note Webhook Constants

**Files:**

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/webhook_constants.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_webhook_constants.py`

- [ ] **Step 1: Add constants for generic object events and synthetic note events**

Add constants equivalent to:

```python
HUBSPOT_NOTE_OBJECT_TYPE_ID = "0-46"

HUBSPOT_GENERIC_CREATION = "object.creation"
HUBSPOT_GENERIC_PROPERTY_CHANGE = "object.propertyChange"
HUBSPOT_GENERIC_DELETION = "object.deletion"
HUBSPOT_GENERIC_ASSOCIATION_CHANGE = "object.associationChange"

HUBSPOT_NOTE_CREATION = "note.creation"
HUBSPOT_NOTE_PROPERTY_CHANGE = "note.propertyChange"
HUBSPOT_NOTE_DELETION = "note.deletion"
HUBSPOT_NOTE_ASSOCIATION_CHANGE = "note.associationChange"

HUBSPOT_GENERIC_NOTE_SUBSCRIPTION_MAP = {
    HUBSPOT_GENERIC_CREATION: HUBSPOT_NOTE_CREATION,
    HUBSPOT_GENERIC_PROPERTY_CHANGE: HUBSPOT_NOTE_PROPERTY_CHANGE,
    HUBSPOT_GENERIC_DELETION: HUBSPOT_NOTE_DELETION,
    HUBSPOT_GENERIC_ASSOCIATION_CHANGE: HUBSPOT_NOTE_ASSOCIATION_CHANGE,
}

HUBSPOT_NOTE_PROPERTY_CHANGE_ALLOWLIST = {
    "hs_note_body",
    "hubspot_owner_id",
    "hs_attachment_ids",
    "hs_timestamp",
}
```

- [ ] **Step 2: Add constant tests**

Test that:

- `0-46` maps only generic `object.*` events into `note.*`.
- The note property allow-list excludes `hs_lastmodifieddate`.
- All synthetic note events are included in supported webhook event families.

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_webhook_constants.py -v
```

Expected:

```text
passed
```

## Task 2: Normalize Generic Note Events at Ingestion Boundary

**Files:**

- Modify: `src/api/routers/integrations/hubspot_webhook_routes.py`
- Modify: `src/service/integrations/hubspot_webhook_ingestion_service.py`
- Test: `tests/api/routers/integrations/test_hubspot_webhook_routes.py`
- Test: `tests/service/integrations/test_hubspot_webhook_ingestion_service.py`

- [ ] **Step 1: Add normalization helper**

Add a helper at the ingestion boundary that:

- Accepts a provider webhook event dictionary.
- If `subscriptionType` is generic `object.*` and `objectTypeId == "0-46"`, returns an internal event with `subscriptionType` replaced by the synthetic `note.*` value.
- Preserves the original provider dictionary under raw event payload.
- Returns an ignored/unsupported result for generic non-note object events.

Required behavior:

```python
normalize({
    "subscriptionType": "object.creation",
    "objectTypeId": "0-46",
    "objectId": "123",
}) -> subscriptionType == "note.creation"
```

```python
normalize({
    "subscriptionType": "object.creation",
    "objectTypeId": "0-1",
    "objectId": "123",
}) -> ignored generic event
```

- [ ] **Step 2: Filter note property changes by allow-list**

For normalized `note.propertyChange` events:

- Dispatch only if `propertyName` is in `HUBSPOT_NOTE_PROPERTY_CHANGE_ALLOWLIST`.
- Acknowledge unsupported note property changes without queueing.
- Log at debug/info level with property name and object id.

- [ ] **Step 3: Add route and ingestion tests**

Cover:

- Generic `object.creation` with `objectTypeId = 0-46` queues a note creation task.
- Generic `object.propertyChange` for `hs_note_body` queues a note property-change task.
- Generic `object.propertyChange` for `hs_lastmodifieddate` is acknowledged and not queued.
- Generic `object.creation` with `objectTypeId = 0-1` is acknowledged and not queued as a note.
- The queued event keeps the original provider payload in `raw`.

Run:

```bash
uv run pytest tests/api/routers/integrations/test_hubspot_webhook_routes.py tests/service/integrations/test_hubspot_webhook_ingestion_service.py -v
```

Expected:

```text
passed
```

## Task 3: Add Note Debounce and Delete Bypass

**Files:**

- Modify: `src/service/integrations/hubspot_webhook_ingestion_service.py`
- Test: `tests/service/integrations/test_hubspot_webhook_ingestion_service.py`

- [ ] **Step 1: Extend same-object refresh coalescing to notes**

Use a refresh key that includes:

- Provider: `hubspot`
- Integration id
- Object family: `note`
- HubSpot object id

Expected key shape:

```text
hubspot:webhook:refresh:{integration_id}:note:{object_id}
```

- [ ] **Step 2: Keep deletions immediate**

For `note.deletion`:

- Do not debounce.
- Do not wait for pending property-change bursts.
- Release pending refresh claims for the same note object key.
- Queue the delete task immediately unless the exact event was already deduped.

- [ ] **Step 3: Add debounce tests**

Cover:

- Note create plus note property-change burst coalesces to one queued refresh.
- Note events do not coalesce with deal, company, contact, or task events that share the same numeric object id.
- Duplicate note webhook events are acknowledged without queueing.
- Note deletion is queued immediately even when a note refresh key exists.
- Note deletion releases the pending note refresh key.

Run:

```bash
uv run pytest tests/service/integrations/test_hubspot_webhook_ingestion_service.py -v
```

Expected:

```text
passed
```

## Task 4: Route Note Events in the Worker Dispatcher

**Files:**

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/dispatcher.py`
- Create: `src/workers/integrations_worker/providers/crm/hubspot/note_webhook_handler.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_dispatcher.py`

- [ ] **Step 1: Add dispatcher mapping**

Route these internal events to `HubSpotNoteWebhookHandler`:

```python
{
    "note.creation",
    "note.propertyChange",
    "note.associationChange",
    "note.deletion",
}
```

- [ ] **Step 2: Add dispatcher tests**

Cover:

- Dispatcher recognizes each `note.*` event.
- Dispatcher does not route generic `object.*` events directly.
- Dispatcher passes organisation id, integration id, portal id, object id, and raw payload to the note handler.

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_dispatcher.py -v
```

Expected:

```text
passed
```

## Task 5: Implement HubSpot Note Webhook Handler

**Files:**

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/note_webhook_handler.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_note_webhook_handler.py`

- [ ] **Step 1: Implement upsert handling**

For these events:

- `note.creation`
- `note.propertyChange`
- `note.associationChange`

The handler must:

- Fetch the latest full note snapshot by HubSpot note id.
- Include required properties and associations in the fetch.
- Persist through `HubSpotNoteRecordSyncService.persist_raw_note(...)`.
- Not hold a database session during provider calls if the surrounding worker pattern supports fresh-session boundaries.

- [ ] **Step 2: Implement 404 soft-delete fallback**

If the single-note fetch returns HubSpot 404:

- Soft-delete only the local note row matching organisation id, integration id, external id, and `activity_type = NOTE`.
- Acknowledge successfully.

If the fetch returns 401, 403, 429, timeout, or provider/server error:

- Do not soft-delete local data.
- Let the worker retry or record failure according to existing integration-worker error handling.

- [ ] **Step 3: Implement direct deletion handling**

For `note.deletion`:

- Do not call HubSpot.
- Soft-delete only the local note row matching:
  - `organisation_id`
  - `integration_id`
  - `external_id`
  - `activity_type = NOTE`
- Treat no row found as success.
- Treat already-inactive row as success.

- [ ] **Step 4: Add handler tests**

Cover:

- Note creation fetches one note and persists it.
- Note property change fetches one note and persists it.
- Note association change fetches one note and persists it.
- 404 during upsert soft-deletes the matching local note.
- 401/403/429/timeout during upsert does not soft-delete the local note.
- Deletion soft-deletes only the org/integration/external-id matched `NOTE` activity.
- Deletion does not soft-delete tasks or other activities with the same external id.
- Repeated deletion succeeds when the row is already inactive.
- Repeated deletion succeeds when the row is absent.

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_note_webhook_handler.py -v
```

Expected:

```text
passed
```

## Task 6: Ensure Single-Note Fetch Includes Required Properties and Associations

**Files:**

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/single_note_fetcher.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_single_note_fetcher.py`

- [ ] **Step 1: Add or confirm note properties**

The fetcher must request:

```python
[
    "hs_note_body",
    "hubspot_owner_id",
    "hs_attachment_ids",
    "hs_timestamp",
    "hs_lastmodifieddate",
    "createdate",
]
```

`hs_timestamp` is the note timeline/source timestamp.

`hs_lastmodifieddate` is the provider modification timestamp used for stale-write ordering.

- [ ] **Step 2: Add or confirm associations**

The fetcher must request associations needed by the writer, including:

- Deals
- Contacts
- Companies

Use the same association naming/object ids already used by the existing HubSpot note polling path.

- [ ] **Step 3: Add fetcher tests**

Cover:

- Single-note fetch requests `hs_timestamp`.
- Single-note fetch requests `hs_lastmodifieddate`.
- Single-note fetch requests deal/contact/company associations.
- The normalized raw note preserves provider properties and association fields for the writer.

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_single_note_fetcher.py -v
```

Expected:

```text
passed
```

## Task 7: Harden Note Writer Regression Behavior

**Files:**

- Modify: `src/workers/integrations_worker/providers/crm/hubspot/note_record_sync_service.py`
- Test: `tests/workers/integrations_worker/providers/crm/hubspot/test_note_record_sync_service.py`

- [ ] **Step 1: Preserve single-note scope**

`persist_raw_note(...)` must upsert only the note represented by the fetched raw note payload.

It must not deactivate unrelated notes during webhook-driven single-note upserts.

- [ ] **Step 2: Reactivate inactive notes on upsert**

If a local note row exists but is inactive and HubSpot fetch returns a valid note snapshot:

- Set the local row active again.
- Refresh note body, owner, timestamps, attachment ids, and associations.

- [ ] **Step 3: Refresh associations from provider snapshot**

On upsert:

- Replace local note association fields with the fetched provider association state.
- Clear stale deal/contact/company links that are absent from the current provider snapshot.
- Preserve organisation isolation on every query.

- [ ] **Step 4: Enforce stale snapshot skip**

If the incoming provider snapshot has `hs_lastmodifieddate` older than the local `source_modified_at`:

- Skip the write.
- Do not clear associations.
- Do not overwrite timeline/source timestamp.

If `hs_lastmodifieddate` is absent:

- Fall back to the existing writer behavior.
- Do not use `hs_timestamp` alone as a modification-order guard unless the current writer already does that for notes.

- [ ] **Step 5: Add writer regression tests**

Cover:

- Single-note upsert does not deactivate unrelated notes.
- Inactive note is reactivated on upsert.
- Association fields refresh for deal/contact/company links.
- Missing association in fetched snapshot clears the stale local link.
- Stale provider snapshot is skipped using `source_modified_at`.
- Every query filters by organisation id and integration id where applicable.

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_note_record_sync_service.py -v
```

Expected:

```text
passed
```

## Task 8: Document API and Operational Behavior

**Files:**

- Modify: `docs/API Documentation/integrations-api.md`

- [ ] **Step 1: Add HubSpot note webhook section**

Document:

- HubSpot sends notes as generic `object.*` events.
- The backend supports note object type id `0-46`.
- Supported note event types are creation, deletion, property change, and association change.
- Supported note property-change subscriptions are `hs_note_body`, `hubspot_owner_id`, `hs_attachment_ids`, and `hs_timestamp`.
- `hs_lastmodifieddate` is fetched but not subscribed to.
- Deletion is immediate and idempotent.
- Create/property/association events fetch a full note snapshot before writing.

- [ ] **Step 2: Add deployment checklist**

Document:

- Enable expanded/generic webhook support for Note in HubSpot.
- Verify note/activity read scopes.
- Reauthorize installs if scopes changed.
- Keep polling/reconciliation enabled as safety net for missed webhooks.

## Task 9: Run Focused Verification

**Files:**

- No code changes.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest \
  tests/api/routers/integrations/test_hubspot_webhook_routes.py \
  tests/service/integrations/test_hubspot_webhook_ingestion_service.py \
  tests/workers/integrations_worker/providers/crm/hubspot/test_dispatcher.py \
  tests/workers/integrations_worker/providers/crm/hubspot/test_note_webhook_handler.py \
  tests/workers/integrations_worker/providers/crm/hubspot/test_single_note_fetcher.py \
  tests/workers/integrations_worker/providers/crm/hubspot/test_note_record_sync_service.py \
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

- Existing polling/reconciliation remains the safety net for missed HubSpot webhooks.
- No note merge or restore handling is included in this pass.
- Unsupported generic object events should be observable in logs/metrics but should not fail the webhook request.
- If HubSpot app scopes change, code deployment alone is not enough; existing installations may need reauthorization.
- If app scopes do not change, webhook subscription configuration can be rolled out without requiring users to reconnect.

## Review Checklist

- [ ] Generic `object.*` note events normalize into internal `note.*` events before dispatcher routing.
- [ ] Original provider payload is retained in raw event data.
- [ ] Generic non-note events are acknowledged and not incorrectly dispatched.
- [ ] Property-change allow-list is enforced.
- [ ] Note refresh debounce key includes object family.
- [ ] Note deletion bypasses debounce.
- [ ] Note deletion is idempotent.
- [ ] Upsert 404 soft-deletes the local note.
- [ ] Upsert 401/403/429/timeouts do not soft-delete.
- [ ] Single-note fetch requests note associations.
- [ ] Single-note writer does not deactivate unrelated notes.
- [ ] Stale-write guard uses provider modification timestamp.
- [ ] All database writes preserve organisation and integration isolation.
