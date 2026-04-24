# HubSpot CRM Deals Record Sync — Design (Phase 6, complete materialisation)

**Status:** Implemented
**Depends on:** Phases 1–5 (schema, pipelines, users, companies, contacts) shipped.

## 1. Context

Phase 6 is the *complete* HubSpot deal materialisation. A single Tier-3
task produces every materialised row a deal touches in the org schema:

* `crm_deals` row per HubSpot deal (typed columns + JSONB overflow + raw
  payload).
* `crm_deal_stage_history` rows from `propertiesWithHistory.dealstage`.
* `crm_deal_participants` rows from inline `associations.contacts`,
  `associations.companies`, plus collaborator / split owner properties.
* `crm_object_associations` rows for every deal↔contact, deal↔company,
  and deal↔line_item edge.

After this phase, the post-connect chain is:

```
schema → pipelines → users → companies → contacts → deals → finalize
```

Every dimension a deal references — pipeline, stage, owner, company,
primary_contact, created_by, updated_by — is populated by an earlier link,
so the writer resolves all seven FKs at write time against session-scoped
maps. The post-hoc `backfill_crm_deals` contract is preserved as a safety
net for late-arriving dimension rows.

All four target tables (`crm_deals`, `crm_deal_stage_history`,
`crm_deal_participants`, `crm_object_associations`) already exist in the
schema — no migrations ship with this phase.

## 2. Scope

**In scope**

- Single HubSpot integration per org.
- Initial, full-portal fetch (not incremental / webhook-driven).
- **Dynamic property discovery**: query `/crm/v3/properties/deals` at sync
  time and pass the complete name list to the deals fetcher. System +
  custom + portal-private properties are all captured.
- **Full audit trail**: `propertiesWithHistory` defaults to the entire
  property list — every numeric, select, date and string field's change
  history is captured in `data_payload.propertiesWithHistory` and the
  `dealstage` series is materialised as `crm_deal_stage_history` rows.
- Inline associations fetch
  (`?associations=contacts,companies,line_items`) feeding three writers:
  the deal row's primary FKs, `crm_deal_participants` rows for every
  associated contact / company, and `crm_object_associations` rows for the
  full edge graph (incl. line items, no participant equivalent).
- Owner-derived participants: `hs_all_collaborator_owner_ids` and
  `hs_all_deal_split_owner_ids` produce `crm_deal_participants` rows of
  type USER with role `collaborator` / `split_owner`.
- Single-pass upsert per writer. Update on synthetic `external_id`
  collision; insert otherwise. No deactivation pass (see §7).
- Chain-correctness: re-raise on `SoftTimeLimitExceeded`, never write
  `initial_sync_status` (owned by chain terminators).

**Out of scope**

- Stale-deal / stale-participant reaper (soft-delete rows that vanish from
  HubSpot).
- Chunked streaming for portals with > 100k deals.
- Internal-CRM deal reconciliation — rewriting `integration_id=INTERNAL_CRM`
  deal rows' company/contact FKs to point at HubSpot-sourced rows is owned
  by a separate reconciliation phase.
- Activity / engagement (calls, emails, meetings, notes) sync from
  HubSpot — separate object types with their own dedicated phases.

## 3. Files

**Created**

```
src/service/integrations/crm/records/deals/
  __init__.py
  deal_types.py
  deal_normalizer.py        — deal row + transitions + participants + edges
  deal_writer.py            — crm_deals upsert with 7 FK resolutions
  stage_history_writer.py   — crm_deal_stage_history upsert
  participant_writer.py     — crm_deal_participants upsert
  association_writer.py     — crm_object_associations upsert (deal-side)
  deal_sync_service.py      — orchestrates all four writers in one session
src/workers/integrations_worker/providers/crm/hubspot/
  deals_fetcher.py
src/workers/integrations_worker/tasks/crm/
  hubspot_deal_record_sync_task.py
docs/superpowers/specs/
  2026-04-23-hubspot-crm-deals-record-sync-design.md
```

**Modified**

- `src/service/integrations/crm/records/common/hubspot_chain.py` — chain
  becomes 7 links; `sync_hubspot_deals` inserted between contacts and
  finalize.
- `src/workers/integrations_worker/config.py` — TASK_LIMITS entry
  (`soft=600`, `hard=660`, `retries=3`, `retry_delay=60`) and
  `task_routes` on `integrations_priority`.
- `src/workers/integrations_worker/tasks/crm/__init__.py` — exports
  `sync_hubspot_deals_task`.
- `src/service/integrations/initial_sync_service.py` — adds
  `record_sync.deals = {"status": "queued"}` to the post-connect state.
- `send_hubspot_deal_schema_sync.py` — `--deals-only` dispatch flag.

## 4. Complete payload capture

The user's explicit goal for this phase: capture the full HubSpot deal,
not a curated subset.

**Mechanism.** `HubSpotDealRecordSyncService._build_property_list` calls
`HubSpotSchemaFetcher.fetch_properties("deals")` (the same Properties API
helper phase 1 uses) at the start of the run. It extracts every `name`
returned by HubSpot, then unions in a baseline safety set of every key in
`HUBSPOT_DEAL_CANONICAL_FIELDS` plus the property-history trackers — so
even if the Properties API returns an unexpectedly slim list, every column
the writer materialises is still requested. The merged + sorted list is
handed to `HubSpotDealsFetcher` as the `properties=` query parameter.

**Where each property lands on the row:**

| HubSpot property                       | CRMDeal landing place                       |
| -------------------------------------- | ------------------------------------------- |
| In `HUBSPOT_DEAL_CANONICAL_FIELDS`     | dedicated typed column (e.g. `name`, `amount`) |
| Money / metrics fields (hs_arr, hs_mrr, …) | `metrics` JSONB                          |
| Anything else (custom + non-canonical) | `properties` JSONB                          |
| Raw record incl. `propertiesWithHistory`, `associations` | `data_payload` JSONB |
| Inline associations block (raw)        | `associations_payload` JSONB                |

A spot-check is part of the verification plan (§9): pick one deal, compare
HubSpot's UI field set against the row's typed-column / `properties` /
`metrics` / `data_payload` keyset.

## 5. Resolution maps

Five session-scoped maps load before the writer runs (one DB round-trip
each). All scoped to `(organisation_id, integration_id, is_deleted=false)`
so portal A's deal can never bind to portal B's dimension.

| Map           | Loaded from           | Used to resolve                                        |
| ------------- | --------------------- | ------------------------------------------------------ |
| `pipeline_map`| `crm_pipelines`       | `pipeline_id`, `pipeline_name`, `pipeline_label`       |
| `stage_map`   | `crm_deal_stages`     | `stage_id`, `stage_name`, `stage_label`, fallback `pipeline_id` |
| `user_map`    | `crm_users`           | `owner_id`+name+email, `created_by_user_id`, `updated_by_user_id` |
| `company_map` | `crm_companies`       | `company_id`, `company_name`, `company_domain`         |
| `contact_map` | `crm_contacts`        | `primary_contact_id`, `primary_contact_name`, `primary_contact_email` |

A miss leaves the FK NULL, keeps the `*_external_id`, and ticks the
matching `unresolved_*_link` counter on `DealWriteResult`. The writer
never raises on a miss — observability + later backfill cover it.

**Pipeline fallback.** If the deal's `pipeline` HubSpot property is missing
or stale but `dealstage` resolves, the writer derives `pipeline_id` from
the matched stage's owning pipeline. This catches HubSpot's occasional
stage-without-pipeline data shape.

## 6. Stage resolution (the "main game")

The user's first explicit ask: "How will we get the stages straight?"

End-to-end:

1. Fetcher requests `dealstage` (always — it's in
   `HUBSPOT_DEAL_CANONICAL_FIELDS`) and `propertiesWithHistory=dealstage,…`.
2. Normaliser stores the HubSpot stage ID on
   `NormalizedHubSpotDeal.stage_external_id`.
3. Writer's `_resolve_all` looks up `stage_external_id` in `stage_map`.
   On hit: `stage_id`, `stage_name`, `stage_label` populated; `pipeline_id`
   derived from the stage row when not already resolved from the deal's
   own `pipeline` property.
4. Phase 2 already wrote `crm_deal_stages.external_id` for every active
   HubSpot stage (with `is_active=false` for stages that vanish from a
   later snapshot but are still referenced by historical deals), so the
   write-time miss rate should be zero on a healthy portal.

If a deal references a stage that's never appeared in any pipeline sync
(very rare, e.g. portal admin deletes a stage between phases 2 and 6),
the row still lands with `stage_id=NULL` and `stage_external_id` populated.
A subsequent pipeline sync that re-discovers the stage would patch the row
via `backfill_crm_deals`.

## 7. Upsert-only — no deactivation pass

`CRMDeal` uses `deal_status` (OPEN/WON/LOST/CLOSED) and `is_deleted`, not
`is_active`. We deliberately do not soft-delete deals that disappear from
HubSpot:

- Historical deal data is frequently still wanted even after a HubSpot
  archive (analytics, win/loss reports, deal coaching).
- A future "stale deal reaper" can read `last_seen_at` and apply policy
  (mark `is_deleted=true` after N days unseen, or force `CLOSED`) — but
  that's a separate decision, not this phase's responsibility.

The writer still updates `last_seen_at = now` on every touched row so the
reaper, when implemented, has the data it needs.

## 8. Property-history capture and stage-history materialisation

`HubSpotDealsFetcher` defaults `propertiesWithHistory` to **the full
property list** — every property HubSpot returns from
`/crm/v3/properties/deals` ships back with its complete change history.
This satisfies the user's "complete deal audit trail" requirement: amount
changes, owner reassignments, custom dropdown re-classifications,
forecast-category shifts — all captured.

The raw block is preserved on `crm_deals.data_payload.propertiesWithHistory`
for replay / future analytics. The `dealstage` series specifically is
also materialised into `crm_deal_stage_history` rows by
`HubSpotDealStageHistoryWriter`:

* `entered_at` ← entry timestamp.
* `exited_at` ← next-newer entry's timestamp (None for the current stage).
* `from_stage_external_id` ← next-older entry's value.
* `stage_id` / `from_stage_id` ← resolved via the same `stage_map` the
  deal writer uses.
* `amount_at_stage` ← latest `amount` history entry whose timestamp is
  ≤ entered_at (co-temporal lookup).
* `probability_at_stage` ← same lookup against
  `hs_deal_stage_probability` history.
* `source` ← HubSpot's `sourceType` (USER / WORKFLOW / API / IMPORT).
* `external_id` ← synthetic `{deal_external_id}:{stage_external_id}:{entered_at_iso}`
  for idempotent re-syncs.

**Caveat.** HubSpot caps the page `limit` to 50 (vs the usual 100) when
`propertiesWithHistory` is requested, so the deals fetcher uses
`_PAGE_LIMIT = 50` and `_MAX_PAGES = 10_000` (still 500k deals per run).
Requesting history on every property also enlarges the per-deal payload;
the 60s timeout on the fetcher is calibrated for this.

## 8a. Participant materialisation

`HubSpotDealParticipantWriter` produces `crm_deal_participants` rows from:

* `associations.contacts.results` → `participant_type=CONTACT`,
  `relationship_role` = HubSpot's association `type` string
  (`deal_to_contact`, `deal_to_contact_primary`, etc.). `is_primary=true`
  on the `_primary` variant.
* `associations.companies.results` → `participant_type=COMPANY`, same role
  semantics.
* `properties.hs_all_collaborator_owner_ids` → `participant_type=USER`,
  `relationship_role="collaborator"`.
* `properties.hs_all_deal_split_owner_ids` → `participant_type=USER`,
  `relationship_role="split_owner"`.

`related_local_id` resolves at write time via `contact_map` /
`company_map` / `user_map` (same dimensions the deal writer uses). Misses
keep `related_external_id` populated for a future re-sync to patch.

Synthetic `external_id`:
`{deal_external_id}:{participant_type}:{related_external_id}:{role}`.

## 8b. Generic association edges

`HubSpotDealAssociationWriter` produces `crm_object_associations` rows for
*every* deal association — contacts, companies, AND line_items — in a
provider-agnostic edge format. Deal-centric consumers prefer the
`crm_deal_participants` table; consumers walking the generic many-to-many
graph (analytics, reporting, the associations agent tool) read from this
table.

`to_local_id` is resolved for contacts and companies. line_items have no
local dimension table today, so those rows have `to_local_id=NULL` —
expected; counted under `associations_unresolved_to_local` for
observability.

Synthetic `external_id`:
`{from_external_id}:{from_object_type}:{to_object_type}:{to_external_id}:{type_id}`.

## 9. Verification

1. `uv run ruff check src/ tests/` — clean on all new + modified files.
2. `uv run mypy src/` — clean.
3. Chain shape smoke:

   ```
   uv run python -c "from src.service.integrations.crm.records.common.hubspot_chain import build_hubspot_initial_sync_chain; from uuid import uuid4; print(build_hubspot_initial_sync_chain(organisation_id=uuid4(), integration_id=uuid4()).tasks)"
   ```

   Must list seven tasks in order: `sync_hubspot_deal_schema`,
   `sync_hubspot_pipelines`, `sync_hubspot_users`, `sync_hubspot_companies`,
   `sync_hubspot_contacts`, **`sync_hubspot_deals`**,
   `mark_hubspot_initial_sync_succeeded`.
4. Task registration smoke:

   ```
   uv run python -c "from src.workers.integrations_worker.celery_app import app; print([n for n in app.tasks if 'hubspot' in n])"
   ```

   Must include `integrations.sync_hubspot_deals`.
5. Property-list sanity (REPL with a real token):

   ```python
   await HubSpotSchemaFetcher(token_info=...).fetch_properties(object_type="deals")
   ```

   Must return ≥ 80 entries on a normal portal; `dealname`, `dealstage`,
   `pipeline`, `amount`, `closedate`, `hubspot_owner_id` must all be
   present.
6. End-to-end on the live test integration (org
   `3ed09720-971e-4fb1-a1d9-bd5e57b432f1`, integration
   `d2a1b7a7-60f6-4273-9717-ace90b6475cb`):

   ```
   uv run python send_hubspot_deal_schema_sync.py 3ed09720-971e-4fb1-a1d9-bd5e57b432f1 d2a1b7a7-60f6-4273-9717-ace90b6475cb
   ```

   Inspect:

   - `crm_deals`: one row per HubSpot deal. `stage_id` + `pipeline_id`
     non-null for every deal whose `dealstage` exists in
     `crm_deal_stages`. `owner_id` resolved via `crm_users`.
     `data_payload.propertiesWithHistory` populated with transition arrays.
   - `connection_metadata.record_sync.deals.status == "success"` with
     visible counters (`deals_created`, `deals_updated`,
     `unresolved_*_link` per FK, `property_list_size`).
   - `initial_sync_status == "success"` (written by finalize, end-to-end
     chain happy-path).
7. Isolated re-run: `--deals-only <org> <integration>` dispatches the
   single task without touching `initial_sync_status` or any dimension.
8. Spot-check completeness: pick one deal row, compare
   `data_payload` + `properties` + `metrics` + typed columns against the
   HubSpot UI for that deal. Every visible custom field must appear
   somewhere; nothing silently dropped.

## 10. Out of scope (deferred)

- **Stale-row reaper.** Uses `last_seen_at` to soft-delete deal /
  participant / association / stage-history rows that vanish from HubSpot
  on a schedule. Today: rows are never deactivated; historical context is
  preserved.
- **Chunked streaming** for >100k-deal portals (current implementation
  buffers the entire deal set in memory before writing).
- **Internal-CRM deal reconciliation** — rewriting internal deal rows'
  company/contact FKs to point at HubSpot-sourced rows is independently
  scheduled.
- **AI deal-summary regeneration trigger.** Phase 6 writes raw deal data;
  the summary agent is invoked by a separate post-sync hook.
- **HubSpot engagement objects** (calls, emails, meetings, notes) — these
  are first-class HubSpot object types with their own `/crm/v3/objects/`
  endpoints and warrant their own dedicated phases.
