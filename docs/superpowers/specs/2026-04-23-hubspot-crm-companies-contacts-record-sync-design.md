# HubSpot CRM Companies + Contacts Record Sync — Design

**Status:** Implemented (phases 4 & 5)
**Depends on:** Phases 1–3 (schema, pipelines, users) shipped.
**Blocks:** Phase 6 (deals fact table) — can't resolve `company_id` /
`primary_contact_id` at write time without these dimensions in place.

## 1. Context

The post-connect HubSpot initial-sync chain fills dimension tables before
the CRMDeal fact table so that FKs can be resolved at write time rather
than patched later. After phases 1–3 the chain runs:

```
schema → pipelines → users → finalize
```

Phases 4 and 5 extend it to:

```
schema → pipelines → users → companies → contacts → finalize
```

Companies must run before contacts because a contact's `company_id` is
resolved at write time from the in-session `crm_companies` snapshot.

Both dimension tables already exist in our schema with all required
columns — no migrations are shipped as part of this phase.

## 2. Scope

**In scope**

- Single HubSpot integration per org.
- Initial, full-portal fetch (not incremental / webhook-driven).
- Static curated property list per entity (custom-property discovery is
  deferred — see §10).
- Active-snapshot upsert for companies (insert / update / reactivate /
  deactivate).
- Upsert-only for contacts — `CRMContact` has no `is_active` column, so
  deactivation is deferred (see §6 and §10).
- Deal-row backfill at end of each phase via the existing
  `DealFkBackfillOp` contract.
- Chain-correctness: re-raise on `SoftTimeLimitExceeded`, never write
  `initial_sync_status` (owned by chain terminators).

**Out of scope**

- Entity reconciliation between `integration_id=INTERNAL_CRM` rows and
  `integration_id=HUBSPOT` rows (see §11).
- Chunked / streaming fetch for very large portals (> 100k rows per entity).
- Custom-property discovery analogous to the deal schema phase.
- Backfill from a later companies sync to older `crm_contacts.company_id`
  gaps.

## 3. Files

**Created**

```
src/service/integrations/crm/records/companies/
  __init__.py
  company_types.py
  company_normalizer.py
  company_writer.py
  company_sync_service.py
src/service/integrations/crm/records/contacts/
  __init__.py
  contact_types.py
  contact_normalizer.py
  contact_writer.py
  contact_sync_service.py
src/workers/integrations_worker/providers/crm/hubspot/
  companies_fetcher.py
  contacts_fetcher.py
src/workers/integrations_worker/tasks/crm/
  hubspot_company_record_sync_task.py
  hubspot_contact_record_sync_task.py
```

**Modified**

- `src/service/integrations/crm/records/common/hubspot_chain.py` — inserted
  `sync_hubspot_companies` → `sync_hubspot_contacts` between users and
  finalize.
- `src/workers/integrations_worker/config.py` — TASK_LIMITS + task_routes
  entries for both tasks on `integrations_priority`.
- `src/workers/integrations_worker/tasks/crm/__init__.py` — re-exports.
- `src/service/integrations/initial_sync_service.py` — added
  `companies` / `contacts` to the `record_sync` queued subtree.
- `send_hubspot_deal_schema_sync.py` — `--companies-only` and
  `--contacts-only` dispatch modes.

## 4. HubSpot API contract

Both phases use the standard CRM Objects API:

- Companies: `GET /crm/v3/objects/companies?archived=false&limit=100&properties=<csv>`
- Contacts:  `GET /crm/v3/objects/contacts?archived=false&limit=100&properties=<csv>`

Response shape (both endpoints):

```json
{
  "results": [
    {
      "id": "12345",
      "properties": { "name": "Acme", "domain": "acme.com", ... },
      "createdAt": "2024-01-01T00:00:00Z",
      "updatedAt": "2024-02-01T00:00:00Z",
      "archived": false
    }
  ],
  "paging": { "next": { "after": "cursor" } }
}
```

Pagination: `paging.next.after` cursor. Fetchers mirror
`owners_fetcher.py` — `_MAX_PAGES = 5000` (bounds at ~500k records per run
with a WARN log on ceiling). Factory `_make_request(params)` avoids
closing over the loop variable (ruff B023).

Property lists (curated, hardcoded in each fetcher):

- Companies: `name, domain, website, description, industry, type,
  lifecyclestage, hubspot_owner_id, numberofemployees, annualrevenue,
  phone, address, city, state, country, zip, timezone, linkedin_company_page,
  hs_createdate, hs_lastmodifieddate`.
- Contacts: `firstname, lastname, jobtitle, email, hs_additional_emails,
  phone, mobilephone, fax, website, company, associatedcompanyid,
  hubspot_owner_id, lifecyclestage, hs_lead_status, hs_marketable_status,
  address, city, state, zip, country, hs_timezone, notes_last_contacted,
  notes_last_updated, createdate, lastmodifieddate, hs_createdate,
  hs_lastmodifieddate`.

## 5. Normalisation

Pure functions, no I/O. Each normaliser returns
`(list[Normalized…], skipped_malformed_count)`.

**Skipped rows**

- Non-dict payload.
- Missing / non-string `id`.
- `archived == true` (defensive — server-side `archived=false` should
  filter these).

**Field mappings**

Company properties → `CRMCompany` columns: `name, domain, website,
description, industry, type→company_type, lifecyclestage,
hubspot_owner_id→owner_external_id, numberofemployees (parsed int),
annualrevenue (parsed float), phone, city, state, country, address,
zip→postal_code, timezone, linkedin_company_page→linkedin_url`.

Contact properties → `CRMContact` columns: `firstname, lastname, email
(lowercased), jobtitle, company→company_name, associatedcompanyid→
company_external_id, phone, mobilephone, fax, website, hubspot_owner_id→
owner_external_id, lifecyclestage, hs_lead_status→lead_status,
hs_marketable_status→marketing_status, address, city, state, zip→postal_code,
country, hs_timezone→timezone, notes_last_contacted→last_contacted_at,
notes_last_updated→last_activity_at, hs_additional_emails (semicolon-split
→ additional_emails tuple)`.

Timestamps: `source_created_at` / `source_modified_at` come from
`raw.createdAt` / `raw.updatedAt`, falling back to `properties.hs_createdate`
/ `properties.hs_lastmodifieddate` (and the legacy `createdate` /
`lastmodifieddate` for contacts).

Numerics (`numberofemployees`, `annualrevenue`): HubSpot returns these as
strings. Defensive coerce — on parse failure leave the field null; don't
reject the row.

## 6. Writers

**Company writer (`HubSpotCompanyWriter.apply`)**

Two passes, scoped to `(organisation_id, integration_id, is_deleted=false)`:

- Pass A: iterate normalized; upsert by `external_id`; insert new row or
  update existing and mark `is_active=True`; bump `synced_at = last_seen_at = now`.
- Pass B: any prior row whose `external_id` is absent from the full
  HubSpot snapshot → `is_active = False`.

`company_id` PK is never rewritten — referenced by `CRMDeal.company_id`
and `CRMContact.company_id`.

**Contact writer (`HubSpotContactWriter.apply`)**

One pass, scoped identically. No deactivation pass because `CRMContact`
has no `is_active` column today. `contact_id` PK is never rewritten —
referenced by `CRMDeal.primary_contact_id`.

`company_id` + `owner_id` resolution:

- Before Pass A, load `{company_external_id → company_id}` from
  `crm_companies` AND `{owner_external_id → crm_users.user_id}` from
  `crm_users` (same session, same org+integration scope).
- For each incoming contact with `company_external_id` / `owner_external_id`,
  look up the PKs. Unresolved → the respective FK stays NULL and the matching
  counter (`unresolved_company_link` / `unresolved_owner_link`) ticks.

**Company writer — owner_id resolution**

Symmetrically, `HubSpotCompanyWriter.apply` accepts the same owner map and
resolves `CRMCompany.owner_id` at write time (`unresolved_owner_link`
counter when absent). Works because the chain runs users-before-companies,
so `crm_users` is already populated by the time companies writes fire.

**Why no hard delete**

Dropping an unseen row (or flipping `is_deleted=True`) would break the PK
invariant if the entity returns — CRMDeal FKs (`company_id`,
`primary_contact_id`) would dangle. Active-snapshot soft flags keep the
row addressable for later reactivation.

## 7. Deal-row backfill

Both phases use the existing `backfill_crm_deals` contract
(`src/service/integrations/crm/records/common/backfill.py`). Allow-lists
already cover the target columns; no contract changes shipped with this
phase.

**Companies phase — one op (FK + name + label shape):**

```python
DealFkBackfillOp(
    external_col="company_external_id",
    fk_col="company_id",
    name_col="company_name",
    label_col="company_domain",
    mapping={ext_id: (company_pk, company_name, company_domain, None), ...},
)
```

**Contacts phase — one op (FK + name + email shape):**

```python
DealFkBackfillOp(
    external_col="primary_contact_external_id",
    fk_col="primary_contact_id",
    name_col="primary_contact_name",
    email_col="primary_contact_email",
    mapping={ext_id: (contact_pk, full_name, None, email), ...},
)
```

The backfill predicate patches rows where the FK is NULL OR the name /
label / email has drifted — i.e. also force-syncs display columns against
the latest HubSpot snapshot.

## 8. Orchestration

Each service follows the fresh-session lifecycle established by phase 3:

```
Session A  — load KnowledgeIntegration + IntegrationTokenInfo. Release.
(no session) — paginated HubSpot fetch.
(no session) — pure-Python normalise.
Session B  — writer.apply() + backfill_crm_deals + commit.
```

Contacts' Session B additionally loads `{company_external_id → company_id}`
at the top of the write session.

Each Tier-3 task:

- Creates sync history via `SyncHistoryManager.start_fresh`.
- Runs service; on `<Phase>SyncConfigurationError` → `record_sync_failure`
  + re-raise (chain aborts via `link_error`).
- On success → `complete_fresh` + `_update_integration_metadata()` writes
  `connection_metadata.record_sync.<phase>` with status + counters +
  `last_synced_at`.
- Re-raises `SoftTimeLimitExceeded` after recording failure.
- Never writes `initial_sync_status`.

## 9. Worker configuration

TASK_LIMITS (mirror phase 3 profile):

```python
"integrations.sync_hubspot_companies": {"soft":300, "hard":360, "retries":3, "retry_delay":60}
"integrations.sync_hubspot_contacts":  {"soft":300, "hard":360, "retries":3, "retry_delay":60}
```

task_routes: both land on `integrations_priority` (co-scheduled with
the chain they belong to).

## 10. Known limitations / deferred

1. **Custom-property discovery** — analogous to the deal schema phase.
   Today overflow lives in `properties` JSONB and the raw record is always
   preserved in `data_payload`. A schema phase for companies/contacts can
   be added without breaking existing records.
2. **`CRMContact.is_active`** — without it we can't deactivate
   contacts-gone-from-HubSpot. Adding this column (via migration) is the
   next natural step before webhook-based updates ship.
3. **Late-arrival company-to-contact backfill** — if a company is
   archived at contact-sync time but resurfaces later, the `crm_contacts`
   row keeps `company_id=NULL` until re-synced. A follow-up can run the
   analogue of `backfill_crm_deals` against `crm_contacts` at the end of
   companies sync.
4. **Chunked / streaming fetch** — both phases accumulate the full record
   set in memory before writing (fine for < 100k rows per entity per org,
   which covers realistic portals). Streaming would require a writer that
   batches without needing the full snapshot for Pass B deactivation.
5. **Entity reconciliation with internal CRM** — see §11.

## 11. Internal-CRM entity reconciliation (NOT included)

### Current behaviour

Every `CRMCompany` / `CRMContact` row carries `integration_id`. The
`InternalCRMSetupService` maintains a sentinel integration of type
`IntegrationType.INTERNAL_CRM`; `DealWriteService` creates internal
companies/contacts with `integration_id = <INTERNAL_CRM>.integration_id`
and `external_id = str(uuid4())`.

Because every statement in phases 4 / 5 filters by
`integration_id = :hubspot_integration`, internal-CRM rows are **never
read, updated, or deactivated** by this pipeline. They survive untouched.

### The resulting duplication

Every real-world entity that exists in both the internal CRM and HubSpot
lands as two rows (one per integration). Deal attribution fragments —
internal deals point at internal companies; HubSpot deals will point at
HubSpot companies.

### Why this PR does not solve it

Silently mutating internal rows would destroy user intent. The right
place for a merge pass is a dedicated phase with explicit match rules
and reversible semantics.

### Proposed next step (own PR)

Add a reconciliation phase between phase 5 (contacts) and phase 6
(deals). New Celery task `integrations.reconcile_internal_to_hubspot`:

1. For each internal `CRMCompany` with a clean domain match to an
   `integration_id=HUBSPOT` row → rewrite internal-deal `company_id` to
   the HubSpot row's PK; mark internal row `is_deleted=True`.
2. For each internal `CRMContact` with a clean email match → same
   rewrite for `primary_contact_id` and participant rows; mark internal
   row `is_deleted=True`.
3. Leave ambiguous matches unmerged; surface a report in
   `connection_metadata.reconciliation.ambiguous`.

This is invasive and needs its own spec; tracked as a follow-up.

## 12. Verification

1. `uv run ruff check src/ tests/` — clean on all new + modified files.
2. `uv run mypy src/` — clean.
3. Chain shape smoke:
   ```bash
   uv run python -c "from src.service.integrations.crm.records.common.hubspot_chain import build_hubspot_initial_sync_chain; from uuid import uuid4; print([t.task for t in build_hubspot_initial_sync_chain(organisation_id=uuid4(), integration_id=uuid4()).tasks])"
   ```
   Expect six names in order: `sync_hubspot_deal_schema`,
   `sync_hubspot_pipelines`, `sync_hubspot_users`, `sync_hubspot_companies`,
   `sync_hubspot_contacts`, `mark_hubspot_initial_sync_succeeded`.
4. Task registration smoke:
   ```bash
   uv run python -c "from src.workers.integrations_worker.celery_app import app; print([n for n in app.tasks if 'hubspot' in n])"
   ```
   Must include `integrations.sync_hubspot_companies` and
   `integrations.sync_hubspot_contacts`.
5. End-to-end (once target integration reconnected):
   ```bash
   uv run python send_hubspot_deal_schema_sync.py <org> <int>
   ```
   Verify in DB:
   - `crm_companies` populated (active-snapshot).
   - `crm_contacts` populated; rows with an `associatedcompanyid` have
     `company_external_id` AND `company_id` resolved.
   - `crm_deals.company_id` / `primary_contact_id` backfilled for any
     deal whose `*_external_id` is in the new snapshot, plus the matching
     display columns (name, domain, email).
   - `connection_metadata.record_sync.companies.status == "success"`,
     same for `contacts`, and finally
     `connection_metadata.initial_sync_status == "success"` (written
     solely by the finalize link).
6. Isolated re-runs:
   ```bash
   uv run python send_hubspot_deal_schema_sync.py --companies-only <org> <int>
   uv run python send_hubspot_deal_schema_sync.py --contacts-only  <org> <int>
   ```
   Neither flips `initial_sync_status`.
