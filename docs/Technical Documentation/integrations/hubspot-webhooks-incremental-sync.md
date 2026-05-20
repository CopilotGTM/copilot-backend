# HubSpot Webhooks and Incremental Sync

Last reviewed: 2026-05-18

## Purpose

HubSpot CRM data should stay fresh after the initial OAuth sync without repeatedly pulling the
entire portal. The advised model is:

```text
Initial full sync creates the local baseline.
Webhooks provide fast change signals.
Incremental search provides scheduled reconciliation.
Both paths converge into the same single-object fetch and idempotent writer path.
```

This document defines how webhooks and incremental sync should work together, how often each
object family should run, and how to avoid conflicts when both paths see the same HubSpot object.

## Current State

The existing post-connect HubSpot CRM chain is:

```text
schema -> pipelines -> users -> companies -> contacts -> deals -> tasks -> notes -> finalize
```

Reference files:

- `src/service/integrations/initial_sync_service.py`
- `src/service/integrations/crm/orchestration/hubspot_initial_sync_chain.py`
- `docs/Technical Documentation/integrations/crm-initial-sync.md`

The chain is still the source of the initial baseline. It writes:

- deal schema/custom field registry
- pipelines and deal stages
- users/owners
- companies/accounts
- contacts
- deals
- deal stage history
- deal participants
- object associations
- CRM activities for HubSpot tasks and notes

HubSpot deal and company webhooks already follow the correct pattern:

- The public route verifies HubSpot's signature and dedupes events.
- The worker receives only a normalized event envelope.
- The handler resolves HubSpot `portalId` to active org integrations.
- Upsert events fetch the current full HubSpot object.
- Delete events soft-delete the local row.
- Full sync and webhook sync reuse the same normalizer/writer paths.

Reference files:

- `src/api/routers/integrations/hubspot_webhook_routes.py`
- `src/workers/integrations_worker/tasks/crm/hubspot/webhooks/event_task.py`
- `src/service/integrations/webhooks/hubspot/subscriptions.py`
- `src/service/integrations/webhooks/hubspot/deals/handler.py`
- `src/service/integrations/webhooks/hubspot/companies/handler.py`
- `src/service/integrations/webhooks/hubspot/portal_resolver.py`
- `src/workers/integrations_worker/providers/crm/hubspot/single_deal_fetcher.py`
- `src/workers/integrations_worker/providers/crm/hubspot/single_company_fetcher.py`
- `docs/Technical Documentation/hubspot-webhook-review-guide.md`
- `docs/Technical Documentation/hubspot-deal-webhook-data-flow.md`

## Why Both Webhooks and Incremental Sync

Use both because they solve different failure modes.

Webhooks are best for freshness:

- They are near-real-time.
- They do not require polling every object page.
- They are the best way to learn about deletes, restores, merges, and association changes.
- HubSpot retries failed webhook deliveries, so transient route or worker failures can recover.

Incremental sync is best for correctness:

- It catches missed webhooks, disabled subscriptions, handler bugs, queue gaps, and rollout windows.
- It gives an operational backstop when a webhook subscription was not enabled for a property.
- It lets us reconcile with HubSpot using a stored high-water mark.

Incremental sync alone is not enough because HubSpot CRM Search excludes archived records.
That means search by modified timestamp can miss deletes. Webhooks alone are not enough because
delivery can be delayed, duplicated, reordered, or temporarily misconfigured.

## Single Source of Truth

Do not build two write systems.

Both webhooks and incremental sync must discover changed object ids only. The same object refresh
service should fetch the full HubSpot object and persist it through existing normalizer/writer code.

```text
HubSpot webhook
  -> normalize event
  -> resolve portal to org integrations
  -> enqueue or call object refresh

5-minute incremental search
  -> find changed object ids
  -> enqueue or call object refresh

Object refresh
  -> acquire token for org integration
  -> fetch full HubSpot object by id
  -> normalize
  -> write through same upsert path
  -> update object cursor only after commit
```

The object refresh service is the convergence point. The source of the trigger must not change
write semantics.

## Recommended Cadence

Start with this schedule:

| Object family | Frequency | Reason |
| --- | --- | --- |
| Deals | Every 5 minutes | Core sales object; high product visibility. |
| Contacts | Every 5 minutes | Needed for stakeholders and buyer/contact freshness. |
| Companies/accounts | Every 5 minutes | Needed for account/deal linking and account metadata. |
| Tasks | Every 15-30 minutes | Useful but less central than core CRM records. |
| Notes | Every 15-30 minutes | Useful activity context; can be noisier than core records. |
| Pipelines/stages | Every 2 hours | Low-change dimension data; affects deal stage resolution. |
| Users/owners | Every 2 hours | Low-change dimension data; affects owner resolution. |
| Schema/properties | Daily | Custom field catalog normally changes slowly. |
| Full reconciliation | Daily or weekly | Safety net for stale rows, missed deletes, and associations. |

For the first implementation, it is reasonable to ship only:

- 5-minute incremental search for deals, contacts, and companies
- 2-hour refresh for pipelines and owners/users
- daily schema refresh

Tasks and notes can follow once the core object path is stable.

## Cursor Rules

Use a per-integration, per-object cursor. Do not rely on only
`KnowledgeIntegration.last_synced_at`.

Recommended metadata shape:

```json
{
  "record_sync": {
    "deals": {
      "status": "success",
      "last_synced_at": "2026-05-18T10:10:00Z",
      "incremental": {
        "cursor_source_modified_at": "2026-05-18T10:06:43.123Z",
        "cursor_updated_at": "2026-05-18T10:10:00Z",
        "lookback_minutes": 10,
        "last_run_type": "incremental_search",
        "last_seen_count": 38,
        "last_written_count": 38,
        "last_skipped_stale_count": 0
      }
    }
  }
}
```

Cursor requirements:

- Store one cursor per object family: deals, contacts, companies, tasks, notes.
- Query from `cursor_source_modified_at - lookback`.
- Use a 10-minute overlap for the 5-minute jobs.
- Sort by provider modified timestamp ascending where the provider supports it.
- Advance the cursor to the max provider modified timestamp that was successfully written.
- Never advance the cursor to wall-clock `now` unless no records were returned and this behavior is
  explicitly chosen for that object family.
- If a run partially fails, keep the previous cursor and let the overlap retry the same window.

The overlap intentionally creates duplicate processing. Duplicate processing is acceptable because
the writer path must be idempotent.

## HubSpot Search Query

For incremental search, use HubSpot CRM Search:

```text
POST /crm/v3/objects/{object}/search
```

Use the object-specific provider modified field:

| Object family | Modified field |
| --- | --- |
| Deals | `hs_lastmodifieddate` or the value that normalizes into `source_modified_at`. |
| Contacts | `lastmodifieddate` with `hs_lastmodifieddate` fallback. |
| Companies | `hs_lastmodifieddate`. |
| Tasks | `hs_lastmodifieddate`. |
| Notes | `hs_lastmodifieddate`. |

Example request shape:

```json
{
  "filterGroups": [
    {
      "filters": [
        {
          "propertyName": "hs_lastmodifieddate",
          "operator": "GTE",
          "value": "2026-05-18T10:00:00Z"
        }
      ]
    }
  ],
  "sorts": [
    {
      "propertyName": "hs_lastmodifieddate",
      "direction": "ASCENDING"
    }
  ],
  "properties": ["hs_lastmodifieddate"],
  "limit": 200,
  "after": "..."
}
```

Search results should be treated as changed-id discovery, not as the source of the full local row.
After search returns an id, call the single-object fetcher for that object family.

HubSpot CRM Search constraints to design around:

- Search can lag for newly created or updated objects.
- Archived objects do not appear in search results.
- Search endpoints are rate limited.
- Page size is limited.
- Large windows can hit total-result limits.

## Property Change Values

There are two different "what changed" mechanisms, and they should not be mixed.

Incremental search should be based on provider modified timestamps:

- deals: `hs_lastmodifieddate`
- contacts: `lastmodifieddate`
- companies: `hs_lastmodifieddate`
- tasks: `hs_lastmodifieddate`
- notes: `hs_lastmodifieddate`

Webhook property-change subscriptions should be based on business properties we care about, not
the provider modified timestamp. HubSpot does not allow every property as a CRM property-change
subscription trigger; notably `hs_lastmodifieddate` is not a valid property-change subscription
property. That means:

```text
Use hs_lastmodifieddate / lastmodifieddate for scheduled incremental search.
Use explicit business fields for webhook propertyChange subscriptions.
```

Recommended first business-property webhook set:

| Object family | Property-change triggers |
| --- | --- |
| Deals | `dealname`, `amount`, `dealstage`, `pipeline`, `closedate`, `hubspot_owner_id`, `dealtype`, `hs_priority`, `hs_forecast_amount`, `hs_forecast_probability`, `hs_deal_stage_probability` |
| Companies | `name`, `domain`, `website`, `industry`, `type`, `annualrevenue`, `phone`, `address`, `city`, `state`, `country`, `zip`, `description` |
| Contacts | `firstname`, `lastname`, `email`, `phone`, `mobilephone`, `jobtitle`, `associatedcompanyid`, `hubspot_owner_id`, `lifecyclestage`, `hs_lead_status`, `address`, `city`, `state`, `zip`, `country` |

The route may choose to filter out unsupported property-change events locally, but the HubSpot app
must still create one provider subscription per property. The local handler should continue to fetch
the full object after a property-change event, so selected-property webhooks do not produce partial
local rows.

## Webhook Scope

Webhook subscriptions should be used first for changes that search cannot safely cover:

| Object family | Webhook events |
| --- | --- |
| Deals | creation, propertyChange, deletion, restore, merge, associationChange |
| Companies | creation, propertyChange, deletion, restore, merge, associationChange |
| Contacts | creation, propertyChange, deletion/privacy deletion, restore, merge, associationChange |
| Tasks | creation, propertyChange, deletion, restore |
| Notes | creation, propertyChange, deletion, restore |

The current worktree already covers deal creation/propertyChange/deletion and
company creation/propertyChange/deletion. Expanding to contact/task/note should follow the same
subscription table and handler pattern.

Do not write directly from the webhook payload. Webhook payloads identify what changed; they do not
carry the full object state needed by our CRM tables.

## Conflict and Ordering Rules

Webhooks and incremental sync can run at the same time. That is expected.

Required behavior:

- Both paths write by `(organisation_id, integration_id, external_id)`.
- External ids are never trusted without org and integration scope.
- The object refresh path fetches the latest provider snapshot before writing.
- If the local row has a newer `source_modified_at` than the fetched payload, skip the write.
- If timestamps are equal, allow the write to be idempotent or skip based on payload equality.
- If the provider returns 404 for an upsert refresh, soft-delete the local row.
- Delete webhooks soft-delete only the target local row; they do not cascade child records.

This makes event order less important:

```text
Webhook arrives first -> object is upserted.
Incremental search sees the same object later -> stale/equal write no-ops.

Incremental search runs first -> object is upserted.
Webhook arrives later -> fetches latest object and no-ops or confirms same state.

Delete arrives before an update webhook -> update fetch returns 404 and keeps tombstone.
Update arrives before delete webhook -> update writes current row, delete tombstones later.
```

## Dedupe and Locks

There are two separate dedupe layers.

Webhook delivery dedupe:

- The public route dedupes by HubSpot event identity for 24 hours.
- If Redis is unavailable, dedupe should fail open and let idempotent writers protect correctness.
- If Celery enqueue fails after claiming a dedupe key, release the key so HubSpot can retry.

Object refresh dedupe:

- Add a short-lived lock per object refresh:

```text
hubspot:refresh:{integration_id}:{object_type}:{object_id}
```

Recommended TTL:

- 1-5 minutes for deals, contacts, companies
- 5-15 minutes for slower tasks, notes, or high-volume portals

The lock is an optimization to reduce duplicate provider calls. It is not the correctness boundary.
Correctness still comes from tenant-scoped idempotent writes and timestamp freshness checks.

## Full Sync vs Incremental Sync Writer Behavior

Full sync can treat the fetched list as a full active snapshot.

Incremental sync cannot.

Important consequence:

- Companies currently have full-snapshot deactivation in the full writer path.
- Tasks and notes also deactivate rows that are absent from the fetched snapshot.
- Incremental sync must never pass only changed ids into a full-snapshot deactivation path.

Use one of these patterns:

1. Existing single-object write methods where available:
   - deals: `_persist_raw_deals(...)`
   - companies: `persist_raw_company(...)` / `apply_single(...)`
2. Add equivalent single-object methods for contacts, tasks, and notes.
3. Or add an explicit `sync_mode` argument:

```python
sync_mode: Literal["full_snapshot", "incremental"]
```

In `full_snapshot` mode, absent rows can be deactivated if that object family supports it.
In `incremental` mode, only changed objects are upserted or soft-deleted.

## Object Refresh Contracts

Each object family should expose a service-level refresh method.

Suggested contracts:

```python
class HubSpotObjectRefreshRequest:
    organisation_id: UUID
    integration_id: UUID
    object_type: Literal["deal", "contact", "company", "task", "note"]
    external_id: str
    trigger: Literal["webhook", "incremental_search", "manual_reconcile"]
    event_id: str | None = None
    occurred_at: datetime | None = None
```

```python
class HubSpotObjectRefreshResult:
    object_type: str
    external_id: str
    action: Literal["created", "updated", "reactivated", "deleted", "skipped_stale", "not_found"]
    source_modified_at: datetime | None
    local_synced_at: datetime
```

The object refresh method owns:

- token loading
- full object fetch
- 404-to-soft-delete conversion
- normalizer/writer call
- stale payload check
- per-object logging

The incremental search job owns:

- reading cursor metadata
- querying HubSpot Search
- queueing/calling object refresh for each id
- advancing cursor after successful writes
- sync history and run metadata

The webhook handler owns:

- subscription-type routing
- portal-to-integration resolution
- event delete vs upsert decision
- queueing/calling object refresh

## Scheduling and Queues

The integrations worker config currently routes HubSpot CRM tasks to
`integrations_priority`, and fixed schedules are owned by Kubernetes CronJobs rather than Celery
beat.

Recommended new tasks:

```text
integrations.sync_hubspot_incremental_core
  - every 5 minutes
  - deals, contacts, companies

integrations.sync_hubspot_incremental_activities
  - every 15-30 minutes
  - tasks, notes

integrations.sync_hubspot_dimensions
  - every 2 hours
  - pipelines/stages, owners/users

integrations.sync_hubspot_schema_all_orgs
  - daily
  - deal/contact/company schema as supported
```

The first release can ship only:

```text
integrations.sync_hubspot_incremental_core
integrations.sync_hubspot_dimensions
existing sync_hubspot_deal_schema_all_orgs
```

Core incremental jobs should not run for integrations where:

- `initial_sync_status != "success"`
- integration is inactive
- integration type is not HubSpot
- OAuth token context cannot be resolved

## Rate Limits and Backpressure

The 5-minute cadence is acceptable if the implementation is rate-aware.

Controls:

- Search only changed core objects, not full pages.
- Use one query window per object family per integration.
- Keep `limit=200`.
- Page until no more results or a safety ceiling is hit.
- If a query returns near the 10,000 result boundary, split the time window and retry smaller ranges.
- Add per-integration concurrency limits so one large portal cannot starve the queue.
- Respect `429` and provider retry headers through existing token/request retry helpers where possible.
- Log request counts, result counts, and cursor lag.

Recommended safety ceilings for first release:

| Object family | Per-run page ceiling | Action at ceiling |
| --- | --- | --- |
| Deals | 500 pages | Stop, keep cursor unchanged, log warning. |
| Contacts | 500 pages | Stop, keep cursor unchanged, log warning. |
| Companies | 250 pages | Stop, keep cursor unchanged, log warning. |
| Tasks | 250 pages | Stop, keep cursor unchanged, log warning. |
| Notes | 250 pages | Stop, keep cursor unchanged, log warning. |

Keeping the cursor unchanged at the ceiling is safer than skipping data. The next run can retry,
or a later improvement can split the window.

## Tenant Isolation

Every read and write must preserve the repository's tenant boundary rules:

- Queries must filter by `organisation_id`.
- Provider-backed CRM rows must also filter by `integration_id`.
- HubSpot `portalId` is routing metadata, not an authorization boundary.
- The portal resolver must only yield active approved organisations and active HubSpot integrations.
- External ids from HubSpot must never be used without org/integration filters.

## Failure Handling

Failure behavior should be explicit:

| Failure | Behavior |
| --- | --- |
| Search request fails before any object writes | Mark run failed; keep cursor unchanged. |
| Some object refreshes fail | Mark partial/failure; keep cursor unchanged or store per-object failures. |
| Object fetch returns 404 | Soft-delete local row for upsert-style refresh. |
| Delete webhook row not found | No-op success. |
| Dedupe Redis unavailable | Continue processing; rely on idempotent writers. |
| Lock acquisition fails | Skip duplicate refresh; let current holder finish. |
| HubSpot returns 429 | Retry/back off; do not advance cursor until successful writes. |
| Search result window exceeds ceiling | Stop, keep cursor unchanged, alert/log warning. |

## Observability

Each run should log and store:

- organisation id
- integration id
- object family
- trigger: webhook, incremental search, manual reconcile
- query window start
- previous cursor
- new cursor
- lookback minutes
- pages fetched
- ids discovered
- objects refreshed
- created/updated/reactivated/deleted counts
- skipped stale count
- provider 404 count
- unresolved FK counts where applicable
- duration
- rate-limit retry count
- cursor lag: `now - cursor_source_modified_at`

Metrics to add later:

- `hubspot_incremental_run_total`
- `hubspot_incremental_run_duration_seconds`
- `hubspot_incremental_records_discovered_total`
- `hubspot_object_refresh_total`
- `hubspot_object_refresh_skipped_stale_total`
- `hubspot_cursor_lag_seconds`
- `hubspot_webhook_events_enqueued_total`
- `hubspot_webhook_events_deduped_total`

## Rollout Plan

### Phase 1: Core incremental sync

1. Add shared HubSpot Search fetcher for changed ids.
2. Add per-object cursor metadata helpers.
3. Add object refresh service for deals, contacts, and companies.
4. Reuse existing deal and company single-object paths.
5. Add contact single-object fetch and persist path.
6. Add `integrations.sync_hubspot_incremental_core`.
7. Schedule every 5 minutes by Kubernetes CronJob.
8. Add tests for cursor overlap, stale skip, and no snapshot deactivation.

### Phase 2: Webhook expansion

1. Keep existing deal and company webhook handling.
2. Add contact creation/propertyChange/deletion/restore/merge subscriptions.
3. Route contact webhooks through the same object refresh service.
4. Add restore/merge/association-change handling for deals and companies.
5. Add operational docs for enabling subscriptions in the HubSpot app.

### Phase 3: Dimensions

1. Add 2-hour refresh for pipelines/stages.
2. Add 2-hour refresh for owners/users.
3. Keep daily schema refresh.
4. Re-run targeted backfills after dimensions update if unresolved FK counts are high.

### Phase 4: Activities

1. Add task single-object fetch/persist path.
2. Add note single-object fetch/persist path.
3. Add 15-30 minute incremental search for tasks and notes.
4. Add webhooks for task/note deletion where supported.

### Phase 5: Reconciliation

1. Add daily or weekly full reconciliation.
2. Compare local active external ids against provider active ids in bounded pages.
3. Detect stale local rows and soft-delete or mark inactive according to object policy.
4. Emit a reconciliation report before enabling automated deletes broadly.

## Tests To Add

Core tests:

- Incremental query uses `cursor - lookback`.
- Cursor advances to max successfully written provider modified timestamp.
- Cursor does not advance on failed or partial runs.
- Search pagination handles `after`.
- Duplicate object ids are deduped inside one run.
- Incremental sync does not deactivate rows absent from changed-id results.
- Object refresh skips stale provider payloads.
- Webhook and incremental refresh of the same object are idempotent.
- Delete webhook soft-deletes only within `(organisation_id, integration_id, external_id)`.
- 404 during upsert refresh converts to local soft-delete.
- Portal resolver never routes inactive orgs or non-HubSpot integrations.

Regression tests near existing files:

- `tests/service/integrations/webhooks/hubspot/deals/test_handler.py`
- `tests/service/integrations/webhooks/hubspot/companies/test_handler.py`
- `tests/service/integrations/crm/records/hubspot/deals/test_deal_writer.py`
- `tests/service/integrations/crm/records/hubspot/companies/test_company_writer.py`
- new tests under `tests/workers/integrations_worker/providers/crm/hubspot/`
- new tests under `tests/workers/integrations_worker/tasks/crm/hubspot/`

## First Implementation Decision

The advised first implementation is:

```text
5-minute incremental search:
  deals, contacts, companies

Webhook-driven refresh:
  keep existing deal/company support
  add contact support next

2-hour dimension refresh:
  pipelines/stages, owners/users

Daily:
  schema/properties

Later:
  tasks, notes, full reconciliation, restore/merge/associationChange coverage
```

This gives good freshness for the core CRM data while preserving correctness through one shared,
idempotent write path.

## External References

- HubSpot CRM Search: `https://developers.hubspot.com/docs/api/crm/search`
- HubSpot Webhooks: `https://developers.hubspot.com/docs/api/webhooks`
- HubSpot API usage limits: `https://developers.hubspot.com/docs/api/usage-details`
