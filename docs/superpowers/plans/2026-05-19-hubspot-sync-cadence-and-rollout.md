# HubSpot Sync Cadence and Object Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the production sync-speed model for HubSpot CRM data and split implementation into object-specific rollout plans.

**Architecture:** Keep webhooks as the near-real-time path and scheduled incremental sync as the repair/reconciliation path. Every changed-object path fetches the full latest HubSpot object and writes through the same normalizer/writer used by full sync, with Redis debounce/locks to reduce duplicate API calls.

**Tech Stack:** Python 3.11, FastAPI webhook route, Celery integrations worker, Redis locks, PostgreSQL/SQLModel, HubSpot CRM v3 object/search APIs, pytest, ruff.

---

## Current Decision

Use both mechanisms:

- **Webhooks** for fast user-visible updates.
- **Incremental polling/search** for completeness when webhook delivery is delayed, dropped, filtered, or subscriptions drift.
- **Full reconciliation** only as a low-frequency/manual safety net.

Do not patch only the changed property from a webhook. Webhooks should continue to trigger full-object refresh:

```text
HubSpot event
  -> refresh request for object family + portal_id + object_id
  -> fetch full latest object from HubSpot
  -> normalize
  -> write through existing writer
```

## External Limits That Shape the Cadence

HubSpot documents that webhook `POST` deliveries to us do **not** count against app API limits. Our follow-up `GET /crm/v3/objects/...` and Search API calls **do** count.

HubSpot public OAuth apps are currently documented at **110 requests per 10 seconds per installed HubSpot account**, excluding CRM Search API's separate limit. HubSpot private app limits vary by tier. Use a conservative internal budget rather than running at the published ceiling.

Initial internal budget:

```text
Per HubSpot portal/integration:
  max outbound HubSpot API calls: 70 per 10 seconds
  max concurrent object refreshes: 3
  max webhook refresh debounce window: 45 seconds
```

This keeps us below documented burst limits while leaving room for OAuth refresh, user-triggered syncs, and other HubSpot calls.

Sources:

- HubSpot Webhooks API guide: https://developers.hubspot.com/docs/api-reference/latest/webhooks/guide
- HubSpot API usage limits: https://developers.hubspot.com/docs/api/usage-details

## Sync Cadence by Object

| Object | Webhook path | Incremental path | Full reconciliation | User-visible freshness target |
|---|---|---|---|---|
| Deals | Enabled: create, property change, delete | Every 5 minutes | Daily off-peak or manual repair | 30-90 seconds after webhook, <= 5 minutes if webhook missed |
| Accounts / Companies | Enabled: create, selected property change, delete | Every 5 minutes | Daily off-peak or manual repair | 30-90 seconds after webhook, <= 5 minutes if webhook missed |
| Contacts | Next object to implement | Every 5 minutes | Daily off-peak or manual repair | <= 5 minutes first, 30-90 seconds after contact webhooks later |
| Tasks | Deferred | Every 15-30 minutes later | Daily/manual | 15-30 minutes |
| Notes | Deferred | Every 15-30 minutes later | Daily/manual | 15-30 minutes |
| Pipelines / stages | No webhook in this pass | Every 2 hours later | Daily/manual | 2 hours is acceptable |
| Owners / users | No webhook in this pass | Every 2 hours later | Daily/manual | 2 hours is acceptable |
| Deal schema/properties | No webhook | Daily later | Manual on demand | Daily/manual |

## Debounce Model

Use debounce for noisy property-change bursts, not for correctness.

Recommended behavior:

```text
Key:
  hubspot:refresh:pending:{object_family}:{portal_id}:{object_id}

TTL:
  45 seconds

On propertyChange/create webhook:
  if SET NX succeeds:
    enqueue refresh task with countdown=30 seconds
  else:
    acknowledge event and do not enqueue another refresh

On deletion webhook:
  process delete immediately
  delete pending refresh key for that object
```

Why countdown matters:

- If a user edits five fields quickly, waiting ~30 seconds lets HubSpot settle and lets us fetch the final object state once.
- If we fetch immediately and skip the next four events, the first fetch might happen before all changes are visible.
- The 5-minute incremental job remains the fallback if a delayed update is missed.

## Freshness Model

Add freshness checks after debounce, not before.

For each object writer path:

```text
If local source_modified_at exists
and fetched provider source_modified_at exists
and local source_modified_at >= provider source_modified_at:
  skip write as stale/no-op
else:
  write full normalized object
```

Rules:

- Do not use webhook `occurredAt` as object freshness. It is event time, not row state.
- Do not advance scheduled incremental cursors from webhook processing.
- Incremental sync owns incremental cursors.
- Webhook refresh owns only object freshness/no-op decisions.

## Object-Specific Rollout Plans

### Plan 1: Deals Refresh Hardening

**Goal:** Make existing deal webhooks efficient without changing semantics.

**Files:**

- Modify: `src/api/routers/integrations/hubspot_webhook_routes.py`
- Modify: `src/workers/integrations_worker/tasks/crm/hubspot/webhooks/event_task.py`
- Modify: `src/service/integrations/webhooks/hubspot/deals/handler.py`
- Modify: `src/service/integrations/crm/records/hubspot/deals/deal_writer.py`
- Test: `tests/api/routers/integrations/test_hubspot_webhook_routes.py`
- Test: `tests/workers/integrations_worker/test_hubspot_webhook_event_task.py`
- Test: `tests/service/integrations/webhooks/hubspot/deals/test_handler.py`
- Test: `tests/service/integrations/crm/records/hubspot/deals/test_deal_writer.py`

- [ ] Add a Redis-backed refresh pending key for `deal.propertyChange` and `deal.creation`.
- [ ] Enqueue deal refresh with `countdown=30` when the pending key is first claimed.
- [ ] Skip duplicate deal refresh enqueue while the key exists.
- [ ] Keep `deal.deletion` immediate and cancel any pending refresh key.
- [ ] Add source-modified freshness skip in the deal writer or service layer.
- [ ] Verify same-batch coalescing still works.
- [ ] Verify separate webhook requests for the same deal collapse to one refresh while the pending key exists.
- [ ] Verify stale fetched payload does not overwrite newer local `source_modified_at`.

**Verification:**

```bash
uv run pytest tests/api/routers/integrations/test_hubspot_webhook_routes.py -v
uv run pytest tests/workers/integrations_worker/test_hubspot_webhook_event_task.py -v
uv run pytest tests/service/integrations/webhooks/hubspot/deals/test_handler.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/deals/test_deal_writer.py -v
uv run ruff check src/api/routers/integrations/hubspot_webhook_routes.py src/service/integrations/webhooks/hubspot src/workers/integrations_worker/tasks/crm/hubspot tests/api/routers/integrations/test_hubspot_webhook_routes.py tests/workers/integrations_worker/test_hubspot_webhook_event_task.py tests/service/integrations/webhooks/hubspot/deals/test_handler.py
```

### Plan 2: Accounts / Companies Refresh Hardening

**Goal:** Apply the same debounce/freshness model to company webhooks.

**Files:**

- Modify: `src/api/routers/integrations/hubspot_webhook_routes.py`
- Modify: `src/service/integrations/webhooks/hubspot/companies/handler.py`
- Modify: `src/service/integrations/crm/records/hubspot/companies/company_writer.py`
- Test: `tests/api/routers/integrations/test_hubspot_webhook_routes.py`
- Test: `tests/service/integrations/webhooks/hubspot/companies/test_handler.py`
- Test: `tests/service/integrations/crm/records/hubspot/companies/test_company_writer.py`

- [ ] Reuse the same pending refresh key pattern with `object_family=company`.
- [ ] Keep selected property allowlist from the current implementation.
- [ ] Keep `company.deletion` immediate and scoped to `(organisation_id, integration_id, external_id)`.
- [ ] Add source-modified freshness skip to `apply_single(...)` or the company persist service.
- [ ] Verify single-company refresh never deactivates unrelated companies.
- [ ] Verify stale fetched company payload does not overwrite newer local data.

**Verification:**

```bash
uv run pytest tests/api/routers/integrations/test_hubspot_webhook_routes.py -v
uv run pytest tests/service/integrations/webhooks/hubspot/companies/test_handler.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/companies/test_company_writer.py -v
uv run ruff check src/api/routers/integrations/hubspot_webhook_routes.py src/service/integrations/webhooks/hubspot src/service/integrations/crm/records/hubspot/companies tests/api/routers/integrations/test_hubspot_webhook_routes.py tests/service/integrations/webhooks/hubspot/companies/test_handler.py tests/service/integrations/crm/records/hubspot/companies/test_company_writer.py
```

### Plan 3: Core Incremental Sync

**Goal:** Add a 5-minute repair layer for deals, companies/accounts, and contacts.

Use existing detailed plan:

```text
docs/superpowers/plans/2026-05-18-hubspot-incremental-sync.md
```

Execution order from that plan:

- [ ] Cursor helpers.
- [ ] HubSpot Search changed-id fetcher.
- [ ] Contact single-object fetch/persist path.
- [ ] Shared object refresh service.
- [ ] Core incremental service.
- [ ] Single-integration Celery task.
- [ ] All-org dispatcher task.
- [ ] Docs.
- [ ] Verification.

Cadence:

```text
CronJob:
  every 5 minutes
  task: integrations.sync_hubspot_incremental_core_all_orgs
  queue: integrations
  objects: deals, companies, contacts
  lookback: 10 minutes
```

### Plan 4: Contacts Webhooks

**Goal:** Add contact create/update/delete webhooks after the core incremental path is stable.

**Files:**

- Create: `src/service/integrations/webhooks/hubspot/contacts/handler.py`
- Modify: `src/service/integrations/webhooks/hubspot/contacts/subscriptions.py`
- Modify: `src/service/integrations/webhooks/hubspot/subscriptions.py`
- Modify: `src/api/routers/integrations/hubspot_webhook_routes.py`
- Create/modify: `src/workers/integrations_worker/providers/crm/hubspot/single_contact_fetcher.py`
- Modify: `src/service/integrations/crm/records/hubspot/contacts/contact_sync_service.py`
- Modify: `src/service/integrations/crm/records/hubspot/contacts/contact_writer.py`
- Test: `tests/service/integrations/webhooks/hubspot/contacts/test_handler.py`

- [ ] Add `contact.creation`, `contact.propertyChange`, `contact.deletion`.
- [ ] Define selected contact property allowlist separately from account/deal properties.
- [ ] Fetch full current contact on create/property change.
- [ ] Soft-delete contact on deletion or 404.
- [ ] Use same object refresh debounce service.
- [ ] Keep `contact.privacyDeletion` as a separate compliance path.

Cadence:

```text
Before contact webhooks:
  contacts sync by incremental search every 5 minutes

After contact webhooks:
  webhook freshness target 30-90 seconds
  incremental search remains every 5 minutes as repair
```

### Plan 5: Deferred Activity Objects

**Goal:** Add tasks/notes later without delaying core CRM freshness.

Initial cadence:

```text
Tasks:
  incremental every 15-30 minutes

Notes:
  incremental every 15-30 minutes
```

Rules:

- Do not add activity objects to the 5-minute loop until API usage is observed in dev/prod.
- Do not subscribe to activity webhooks until HubSpot support for those event types is verified against current docs/API.
- Use the same full-object refresh model if supported.

### Plan 6: Low-Change Metadata

**Goal:** Keep supporting dimensions fresh without wasting API budget.

Cadence:

```text
Pipelines/stages:
  every 2 hours

Owners/users:
  every 2 hours

Deal schema/properties:
  daily off-peak and manual on demand
```

Rules:

- These should not run every 5 minutes.
- Deal refresh should tolerate temporarily unresolved owner/stage/pipeline links.
- Later metadata sync/backfill resolves missing references.

## Rollout Order

1. **Deals debounce/freshness** because deal webhooks are already live.
2. **Companies debounce/freshness** because company webhooks are now implemented.
3. **Core incremental sync** for deals, companies, contacts every 5 minutes.
4. **Contacts webhooks** after core incremental proves stable.
5. **Tasks/notes incremental** only after measuring API usage.
6. **Low-change metadata schedules** after the core path is stable.

## Observability Requirements

Add structured logs/metrics for:

- refresh requested
- refresh skipped due pending debounce
- refresh started
- refresh succeeded
- refresh soft-deleted
- refresh skipped stale
- refresh failed with provider status
- HubSpot API calls by portal/integration/object family
- queue delay from webhook received to refresh start
- age of local data versus provider modified timestamp

Minimum log fields:

```text
organisation_id
integration_id
portal_id
object_family
object_id
subscription_type
property_name
debounce_key
source_modified_at
local_source_modified_at
result
```

## Acceptance Criteria

- A burst of five `deal.propertyChange` events for the same deal produces one HubSpot object fetch within the debounce window.
- A burst of five `company.propertyChange` events for the same company produces one HubSpot object fetch within the debounce window.
- Deletion events are not delayed by debounce.
- Webhook full-object refresh still uses the existing writers.
- Scheduled incremental sync catches a missed webhook within 5 minutes plus indexing/queue delay.
- Stale provider payloads do not overwrite newer local rows when provider timestamps are available.
- No code path trusts external object id without `organisation_id` and `integration_id`.
- Focused tests and ruff pass before each commit.

## Open Questions Before Implementation

1. Should the first deal/account refresh wait 30 seconds, or should it run immediately and suppress only later duplicates?
   - Recommendation: wait 30 seconds for property changes and creations; process deletions immediately.
2. Do we want one shared `HubSpotObjectRefreshService` before implementing contacts, or harden deals/accounts first and extract after?
   - Recommendation: harden deals first with a small helper, then extract shared service when accounts is added.
3. Should core incremental sync run in dev at 5 minutes immediately?
   - Recommendation: yes, dev first. Prod after observing API call volume and cursor lag.
4. What is the production freshness promise we tell users?
   - Recommendation: "Most HubSpot deal/account changes appear in under 2 minutes; missed webhook changes are repaired by scheduled sync within about 5-10 minutes."

## Execution Handoff

Plan complete and saved to:

```text
docs/superpowers/plans/2026-05-19-hubspot-sync-cadence-and-rollout.md
```

Recommended execution:

1. Start with **Plan 1: Deals Refresh Hardening**.
2. Review results and API-call behavior.
3. Then execute **Plan 2: Accounts / Companies Refresh Hardening**.
4. Then execute the existing detailed incremental plan:

```text
docs/superpowers/plans/2026-05-18-hubspot-incremental-sync.md
```
