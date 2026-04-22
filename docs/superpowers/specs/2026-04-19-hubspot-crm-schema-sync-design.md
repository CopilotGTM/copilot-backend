# HubSpot CRM Schema Sync — Redesign

**Status:** 2026-04-19 — supersedes the 2026-04-15 deal-schema-sync spec and its plan.

## 1. Goal

Ingest HubSpot **deal property definitions** into CopilotGTM and store them against our internal CRM schema so that:

- Any HubSpot property that already has a matching column on `CRMDeal` is treated as **canonical** and does **not** create a new custom-field row.
- Any HubSpot property that does **not** have a matching canonical column is mirrored into `crm_custom_field_definitions` as a provider-backed custom field, tagged so we can tell HubSpot's built-in properties apart from properties the tenant created inside HubSpot.
- Sync is strictly **HubSpot → CopilotGTM** (ingest-only). No write-back. Internal CRM rules stay authoritative.

This spec is intentionally limited to schema sync. Record sync (deals, pipelines, stages, owners, contacts, companies) is a separate, later phase. Handlers, services, APIs, and UI layers that *consume* the mirrored schema are also out of scope — they are a follow-on design.

## 2. Strategic Context (non-normative)

Internal CRM is the long-term source of truth. HubSpot is treated as an ingest-only source today so that tenants can later be migrated fully onto the internal CRM without schema churn. The schema registry built here is the foundation both for the record-sync phase and for the eventual HubSpot turn-off.

## 3. Scope

### In scope

- Fetching the HubSpot **deal** property schema via HubSpot's CRM Properties API.
- Deterministic classification of each returned property into:
  - **Canonical** — has a matching column on `CRMDeal`; counted and discarded.
  - **Provider-custom, HubSpot-defined** — `hubspotDefined=true` and no canonical match; persisted with `is_provider_custom=false`.
  - **Provider-custom, tenant-defined** — `hubspotDefined=false`; persisted with `is_provider_custom=true`.
- Persistence into `crm_custom_field_definitions` with `field_source='provider'`, `entity_type='deal'`, `provider_name='hubspot'`, `integration_id` scoped to the syncing integration.
- Active-snapshot semantics for the deal object scope: new rows inserted, existing rows updated, missing rows deactivated (not soft-deleted).
- One Tier 3 Celery task: post-connect dispatch, scheduled refresh every 14 days, reusable from any future manual re-run entry point.
- Sync history via `SyncHistoryManager` and structured metadata into `KnowledgeIntegration.connection_metadata`.
- **Write-side** guarantee that the registry writer only mutates `field_source='provider'` rows for the deal entity scope of the syncing HubSpot integration.

### Out of scope

- Handlers, service-facing APIs, controllers, or UI that expose mirrored fields to read or write flows. Those are a separate design.
- **Read-path hardening of internal CRM services and handlers** (e.g., adding `field_source='internal'` filters to `custom_field_service`, `custom_field_write_service`, `deal_write_service`, `deals_handler`). Until those consumers are redesigned, they will keep reading whatever `crm_custom_field_definitions` rows exist for the org — this is acceptable for this phase because provider rows are only written into that table by the sync task itself and are not yet surfaced to any user-facing flow. The consumption-layer design will decide how internal reads and provider reads coexist.
- Record sync for any CRM entity (deals, contacts, companies, pipelines, stages, owners).
- Schema sync for any HubSpot entity other than **deals**.
- Salesforce schema sync (separate integration, separate future spec).
- Any write-back path to HubSpot.
- Test code. Tests are designed here as concepts only; the implementation plan schedules them as a **single end-phase task**.

## 4. Product Rule

> If a HubSpot deal property maps exactly to an existing canonical column on `CRMDeal`, it must **not** be stored as a provider-backed custom field. Only non-canonical properties are persisted to `crm_custom_field_definitions`.

The canonical map is exact-string only. No fuzzy matching, no label similarity, no inferred intent.

## 5. High-Level Pipeline

```
[ Entry points ]                        [ Orchestrator ]             [ Units ]

post-connect dispatch  ──┐                                           ┌─ 1. Fetcher
                         │                                           │    HubSpot Properties API → raw list
K8s CronJob (14 days)  ──┼─► Tier 3 ─► service.run_for(              ├─ 2. Classifier
                         │    Celery     org_id, integration_id )    │    raw → Canonical | ProviderCustom
manual re-run (future) ──┘    task                                   ├─ 3. Normalizer
                                                                     │    HubSpot type/options → internal shape
                                                                     └─ 4. Registry writer
                                                                          upsert active / deactivate missing
```

Fresh-session lifecycle (matches the existing `integrations_worker` pattern). The **service** owns only the sessions needed for its own work; the **task** owns the sessions around sync history and integration-metadata updates (see Section 11).

Service internal lifecycle:

1. **Service Session A — extract integration state.** Load `KnowledgeIntegration`, verify HubSpot + active + owned by `organisation_id`, read credentials into an `IntegrationTokenInfo`, release session.
2. **No DB session held.** Fetcher calls HubSpot Properties API with OAuth token refresh via `call_with_token_retry`.
3. **In-memory classify + normalize.** Pure-Python pass over the raw list builds the set of normalized provider-custom fields.
4. **Service Session B — persist registry.** Open a fresh session; registry writer upserts active rows and deactivates missing ones; commit; release. No sync-history or connection-metadata writes happen here — those are the task's responsibility.
5. **Return `SchemaSyncResult`** with counts to the caller.

### Idempotency guarantees

- Re-running with the same HubSpot payload yields the same active set. Rows are keyed by the existing `(integration_id, entity_type, provider_field_key, is_deleted)` partial unique index on `field_source='provider'`.
- Re-runs update `last_seen_at` and metadata fields; they never insert duplicates.
- Canonical-mapped properties are always excluded — no drift into the registry.

### Tenant isolation invariants (enforced on every DB op the sync code owns)

- All reads and writes in the sync code filter by `organisation_id` **and** `integration_id`.
- Registry writer filters `field_source='provider'` on every statement and so never mutates internal rows.
- Internal CRM services and handlers are **not** modified by this phase; making their reads provider-aware is deferred to the consumption-layer design.

## 6. Pre-defined Mapping File

The single source of truth for "HubSpot property → canonical `CRMDeal` column" lives as a Python constant so it is type-checked, grep-able, and edit-gated by PR review.

**File:** `src/service/integrations/crm/schema/hubspot_deal_mapping.py`

**Public API:**

```python
HUBSPOT_DEAL_CANONICAL_FIELDS: Mapping[str, str]      # frozen dict

def is_hubspot_deal_canonical(property_name: str) -> bool: ...
def canonical_column_for(property_name: str) -> str | None: ...
```

**Contents (initial set):**

```python
HUBSPOT_DEAL_CANONICAL_FIELDS: dict[str, str] = {
    "dealname":                       "name",
    "amount":                         "amount",
    "deal_currency_code":             "currency",
    "amount_in_home_currency":        "amount_home_currency",
    "closedate":                      "close_date",
    "dealstage":                      "stage_external_id",
    "pipeline":                       "pipeline_external_id",
    "hubspot_owner_id":               "owner_external_id",
    "dealtype":                       "deal_type",
    "description":                    "description",
    "hs_forecast_category":           "forecast_category",
    "hs_priority":                    "priority",
    "hs_deal_stage_probability":      "probability",
    "hs_projected_amount":            "predicted_amount",
    "createdate":                     "source_created_at",
    "hs_lastmodifieddate":            "source_modified_at",
    "hs_date_entered_current_stage":  "stage_entered_at",
    "notes_last_updated":             "last_activity_at",
    "days_to_close":                  "days_to_close",
    "closed_won_date":                "closed_won_at",
    "closed_lost_reason":             "closed_reason",
}
```

**Invariants enforced by a module-level self-validator (runs at import):**

- Every value must be an attribute on `src.domain.models.organisation_tables.sync.crm.deals.CRMDeal`. Renaming a `CRMDeal` column without updating the map causes import-time failure in tests and boot.
- Keys are unique and lowercase snake_case.
- Values are unique — no two HubSpot properties may claim the same canonical column.

**Expansion contract:**

This file is also the field-level data map that the future record-sync phase will use to populate `CRMDeal` from HubSpot payloads. New canonical mappings are added here — not inside the sync service.

**Not in this file:** HubSpot→internal data-type mapping (normalizer's job), business rules (internal-CRM services), Salesforce mappings (separate file when that integration is redesigned).

## 7. Classification

Pure function, no I/O. Input: one raw HubSpot property dict. Output: one tagged shape.

```python
@dataclass(slots=True, frozen=True)
class CanonicalMatch:
    hubspot_property: str
    canonical_column: str

@dataclass(slots=True, frozen=True)
class ProviderCustomMatch:
    hubspot_property: str
    is_provider_custom: bool    # True = tenant custom in HubSpot; False = HubSpot built-in we don't canonicalize

Classification = CanonicalMatch | ProviderCustomMatch
```

**Rules:**

- Canonical lookup is exact string match via `canonical_column_for(name)`.
- `hubspotDefined` missing → treated as `False` → flagged as tenant custom. HubSpot always sends this flag in practice; missing defaults to the safer side (tenant custom).
- Archived properties never reach the classifier — they're filtered at the fetch layer (`archived=false` request param).

## 8. Normalization

Pure function, no I/O. Runs only for `ProviderCustomMatch` — canonical matches are discarded before this step.

```python
@dataclass(slots=True)
class NormalizedSchemaOption:
    value: str
    label: str
    description: str | None = None
    display_order: int | None = None
    hidden: bool = False

@dataclass(slots=True)
class NormalizedSchemaField:
    provider_field_key: str              # HubSpot property "name" — DB unique-key component
    key: str                             # internal machine key; equals provider_field_key for HubSpot
    label: str                           # HubSpot "label" (fallback: property name)
    description: str | None
    data_type: CRMCustomFieldDataType    # from the HubSpot→internal type table below
    is_provider_custom: bool             # from classifier
    display_order: int | None
    options: list[NormalizedSchemaOption]
    validation: dict[str, Any]           # empty for phase 1
    formula: dict[str, Any] | None       # {"source": "hubspot", "expression": ...} for calculated fields
    data_payload: dict[str, Any]         # raw HubSpot property, unmodified, for replay
    source_modified_at: datetime | None  # parsed from raw["updatedAt"]
```

### HubSpot → internal data-type table (deterministic)

| HubSpot shape | `CRMCustomFieldDataType` |
|---|---|
| `type=string` (any non-enumeration) | `text` |
| `type=number`, `numberDisplayHint=currency` | `currency` |
| `type=number`, `numberDisplayHint=percentage` | `percentage` |
| `type=number`, other | `number` |
| `type=bool` | `boolean` |
| `type=date` | `date` |
| `type=datetime` | `datetime` |
| `type=enumeration`, `fieldType in {checkbox, multiple_checkboxes}` | `multiselect` |
| `type=enumeration`, other `fieldType` (`select`, `radio`, `booleancheckbox`) | `select` |
| `calculated=true` with `calculationFormula` present | `formula` |
| anything unrecognized | `text` (fallback; WARNING log) |

### Option normalization

- Each HubSpot option → one `NormalizedSchemaOption`.
- Missing `label` falls back to `value`. `hidden` and `displayOrder` are preserved.
- DB `options` JSONB shape: `{"values": [ {...}, ... ], "source": "hubspot"}`.

### Formula handling

- `calculated=true` + `calculationFormula` present → `data_type='formula'`, `formula={"source": "hubspot", "expression": raw["calculationFormula"]}`.
- `calculated=true` + no formula string → fall back to `text`; WARNING log.
- We do **not** evaluate HubSpot formulas.

## 9. Persistence & Active-Snapshot Semantics

### Registry writer contract

Provider-backed persistence is the only place that writes to `crm_custom_field_definitions` with `field_source='provider'`. Invoked once per sync run, inside Session B, with the full normalized list.

```python
class HubSpotDealRegistryWriter:
    def __init__(self, *, session, organisation_id: UUID, integration_id: UUID) -> None: ...

    async def apply(
        self,
        *,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult: ...
```

**Mandatory scoping on every query and mutation:**

- `organisation_id = <current>`
- `integration_id  = <current HubSpot integration>`
- `entity_type    = 'deal'`
- `field_source   = 'provider'`
- `is_deleted     = false`

The writer refuses to issue any statement missing these filters. Internal rows (`field_source='internal'`) are invisible to it by construction.

### Active-snapshot rules

For the deal object scope of the current HubSpot integration:

1. Load the existing set of **non-deleted** provider rows (both active and inactive), keyed by `provider_field_key`, and remember each row's `is_active` state before the run.
2. For each incoming `NormalizedSchemaField`:
   - **New key (no existing row)** → insert with `is_active=true`, `last_seen_at=now`, all metadata populated. Bump `created`.
   - **Existing key, was active** → update `label`, `description`, `data_type`, `is_provider_custom`, `options`, `formula`, `data_payload`, `source_modified_at`, `last_seen_at`. Leave `is_active=true`. Bump `updated`.
   - **Existing key, was inactive** → same field updates as above, plus set `is_active=true`. Bump `reactivated` (**not** `updated` — the buckets are mutually exclusive).
3. Any existing row whose `provider_field_key` is **not** in the incoming set and was previously `is_active=true` gets `is_active=false`. Bump `deactivated`. No hard delete, no soft delete (`is_deleted` stays `false`). Historical references to the field remain renderable. Rows that were already inactive are left alone and not counted.

### Result shape

```python
@dataclass(slots=True)
class RegistryWriteResult:
    created: int
    updated: int
    deactivated: int
    reactivated: int
    rows: list[CRMCustomFieldDefinition]
```

Counts bubble up into `SchemaSyncResult` and into `connection_metadata`.

## 10. Write-Side Isolation (and deferred read-path work)

Provider and internal rows share the same table. The scope of this phase is the **write side** only.

- **Registry writer → provider rows only.** Every statement issued by the writer filters `field_source='provider'` at the DB level. Internal rows are invisible to it by construction, so the writer cannot corrupt internal custom-field state even if a tenant happens to have an internal key that collides with a HubSpot property name.
- **Read path is out of scope** for this phase. Existing internal CRM services (`custom_field_service`, `custom_field_write_service`, `deal_write_service`) and handlers (`deals_handler`) are **not** modified here. Provider rows will exist in `crm_custom_field_definitions` after the sync runs but are not surfaced to any user-visible read path yet — no handler, service, or UI consumes them in this phase. The follow-on consumption-layer design decides how internal reads distinguish `field_source='internal'` from `field_source='provider'`.

Rationale: the product rule for this phase is "populate the registry so later phases have something to consume". Forcing read-path filters into four unrelated files now would expand scope without delivering product value, and those files will be re-examined when consumption is designed.

## 11. Sync Orchestration

### Tier 3 Celery task

- **Name:** `integrations.sync_hubspot_deal_schema`
- **Queue:** `integrations_priority`
- **Retries:** 3, base delay 60s (exponential backoff via Celery defaults)
- **Soft / hard time limit:** 900s / 960s

Lifecycle:

1. Coerce `organisation_id` and `integration_id` args from strings to UUIDs.
2. `SyncHistoryManager.start_fresh(...)` in a session, release.
3. `HubSpotDealSchemaSyncService().run(organisation_id=..., integration_id=...)` → `SchemaSyncResult`.
4. `SyncHistoryManager.complete_fresh(..., status=SUCCESS, records_processed=custom_properties_persisted, metadata=<result>)`.
5. Update `KnowledgeIntegration.last_synced_at` and `connection_metadata.schema_sync.deals`.
6. `SoftTimeLimitExceeded` → `record_sync_failure(...)` + return `{"status": "timed_out"}`.
7. Provider/DB exception → mark sync history `FAILED` + re-raise for Celery retry.

### Post-connect dispatch

`InitialSyncService.apply_post_connect_state()` — for `IntegrationType.HUBSPOT` writes:

```json
{
  "initial_sync_status": "queued",
  "schema_sync": {
    "deals": { "status": "queued" }
  }
}
```

`InitialSyncService.trigger_initial_sync()` — for HubSpot, replace the existing `not_implemented` placeholder with:

```python
send_integrations_task(
    task_name="integrations.sync_hubspot_deal_schema",
    kwargs={
        "organisation_id": str(organisation_id),
        "integration_id":  str(integration.integration_id),
    },
)
```

Salesforce retains its current `not_implemented` placeholder — no behavior change.

### Scheduled refresh (Kubernetes CronJob → `cron_dispatch.py`, every 14 days)

This codebase does **not** use in-process Celery Beat. `IntegrationsWorkerConfig.beat_schedule` is empty by design; fixed schedules are owned by Kubernetes CronJobs that invoke `python -m src.workers.integrations_worker.cron_dispatch --job <name>`. We follow the same pattern.

- A new Tier 1 Celery task `integrations.sync_hubspot_deal_schema_all_orgs`:
  1. Opens a master session, reads all `KnowledgeIntegration` rows where `integration_type = HUBSPOT` and `is_active = true`.
  2. For each, dispatches the Tier 3 task with `organisation_id` + `integration_id`.
  3. Holds no per-org session; it only reads from master.
- A new `ScheduledJob` entry in `src/workers/integrations_worker/scheduled_jobs.py`:
  - `name = "sync-hubspot-deal-schema-all-orgs"`
  - `task_name = "integrations.sync_hubspot_deal_schema_all_orgs"`
  - `schedule = "0 2 */14 * *"` (every 14 days at 02:00 UTC — schedule metadata used by the K8s CronJob manifest and by dispatch validation)
  - `max_lag_seconds = 60 * 60 * 48` (alert if the run is more than 48h late)
- The K8s CronJob manifest change (infra repo) is **out of scope** for this plan — ops applies it once `scheduled_jobs.py` is merged.

### Manual re-run

Any future "Refresh schema" hook dispatches the same Tier 3 task. No new task shape. The handler that exposes the hook is **out of scope** for this spec.

### Service result shape

```python
@dataclass(slots=True)
class SchemaSyncResult:
    provider:                     str   # "hubspot"
    object_type:                  str   # "deal"
    fetched_properties:           int
    canonical_properties:         int   # counted, not persisted
    custom_properties_persisted:  int
    created:                      int
    updated:                      int
    reactivated:                  int
    deactivated:                  int
    skipped:                      int   # type-unmappable, WARNING logged
    started_at:                   datetime
    completed_at:                 datetime
```

## 12. Error Handling & Observability

### Failure classes

1. **Configuration / validation** (non-retryable)
   - Integration missing, wrong type, inactive, or not owned by `organisation_id`.
   - Credentials missing or expired past refresh.
   - → Abort early. Mark sync history `FAILED` with `error_class="configuration"`. No Celery retry.

2. **Provider / API** (retryable with bounds)
   - HubSpot 401 after a refresh attempt → trigger the existing reauth path; fail sync with `error_class="provider_auth"`; no further retry.
   - HubSpot 429 → Celery retry with backoff.
   - HubSpot 5xx → Celery retry with backoff.

3. **Persistence / normalization** (surface, don't silently downgrade)
   - **Type fallback** (e.g., unrecognized `type`/`fieldType`, or `calculated=true` with no `calculationFormula`) → normalize as `text`, WARNING log with `{integration_id, property_name, type, fieldType}`, persist normally. This is **not** a skip — the row is still written.
   - **Skipped** → raw HubSpot property is structurally malformed and cannot produce a `NormalizedSchemaField` at all (e.g., missing `name` field, or non-string `name`). Increment `skipped`, WARNING log, continue the run. No row is written for the property.
   - DB errors → mark sync history `FAILED`; re-raise for Celery retry.

### Sync history

Every run writes one `SyncHistoryRecord` via `SyncHistoryManager`:

- `entity = "schema"`, `object_type = "deal"`, `provider = "hubspot"`.
- `metadata` = the `SchemaSyncResult` as a dict.
- Terminal status: `SUCCESS` | `FAILED` | `TIMED_OUT`.

### Connection metadata

```json
{
  "schema_sync": {
    "deals": {
      "status": "success" | "failed" | "queued" | "timed_out",
      "last_synced_at": "2026-04-19T02:00:00Z",
      "fetched_properties": 123,
      "canonical_properties": 18,
      "custom_properties_persisted": 105,
      "created": 2,
      "updated": 101,
      "reactivated": 1,
      "deactivated": 1,
      "skipped": 0,
      "error_class": null,
      "error_message": null
    }
  }
}
```

### Logging

- Structured logs with `organisation_id`, `integration_id`, `task_name`, `sync_history_id`.
- No HubSpot access tokens or refresh tokens ever logged.

### Non-goals

- No silent "best effort success" downgrades. Failures must be visible in sync history and `connection_metadata`.
- No auto-repair of canonical drift. Removed HubSpot properties simply become inactive.

## 13. Testing (end-phase, single task)

Tests are a single end-of-implementation task, not per-step. The plan schedules them as one block. Required coverage:

**Unit (pure, no DB, no network):**

- `classify()` — canonical hit, tenant-custom hit, HubSpot-defined-not-canonical hit, missing `hubspotDefined` flag defaults.
- `hubspot_deal_mapping` self-validator — rejects map when value is not a `CRMDeal` attribute; rejects duplicate values.
- `normalize()` — one case per row of the HubSpot → internal type table; option normalization; formula path; calculated-without-formula fallback.

**Component (in-memory DB):**

- Registry writer — insert new, update existing, deactivate missing, reactivate previously-inactive, refuse-to-touch `field_source='internal'` rows (write-side isolation only — no assertion about what the internal read services return).

**Integration (mocked HubSpot HTTP):**

- Fetcher — happy path; 401-then-refresh; 429 retry; 5xx retry; archived filter; malformed payload.
- Service `run()` — end-to-end with a fixture HubSpot payload; asserts counts in `SchemaSyncResult` and rows in DB.

**Worker:**

- Tier 3 task — success shape, configuration failure path, timed-out path, sync-history start/complete calls.
- Beat scheduler task — dispatches one Tier 3 per active HubSpot integration; skips inactive.
- Post-connect — HubSpot triggers Tier 3 dispatch; Salesforce still applies `not_implemented` placeholder.

**Regression:**

- Existing HubSpot OAuth / initial-sync tests updated to reflect new `schema_sync.deals.status="queued"` metadata.

## 14. Future Extension Path

The design is intentionally shaped so the next slices are additive, not restructuring.

- **Contacts / companies schema** — new mapping files (`hubspot_contact_mapping.py`, `hubspot_company_mapping.py`), new fetch `object_type` param, same classifier/normalizer/writer. `CRMCustomFieldEntity` enum expands (`CONTACT`, `COMPANY`) and the writer is templated over `entity_type`.
- **Record sync** — the canonical mapping file doubles as the record-sync field map. Record-sync code reads `HUBSPOT_DEAL_CANONICAL_FIELDS` to drive canonical column writes and writes unmapped values into `CRMDeal.properties` keyed by the registry rows this sync produces.
- **Salesforce schema sync** — separate mapping file, separate fetcher, same shapes. Provider name changes, `CRMCustomFieldSource.PROVIDER` and the table are already multi-provider.
- **Manual "Refresh schema" endpoint** — a thin handler that dispatches the Tier 3 task. No change to this spec.

## 15. Open Decisions Resolved

- **Sync direction?** HubSpot → internal only, ingest-only.
- **Object scope?** Deals only.
- **Mapping file format?** Python constant with import-time self-validation.
- **Two custom-column types?** Both live in `crm_custom_field_definitions` with `field_source='provider'`; `is_provider_custom` distinguishes HubSpot-built-in vs. tenant-created.
- **Persist canonical-mapped properties as custom rows?** No.
- **Persist unmapped HubSpot built-in properties as custom rows?** Yes (with `is_provider_custom=false`).
- **Sync triggers?** Post-connect dispatch + K8s CronJob firing a Tier 1 fan-out task every 14 days + the same Tier 3 task reusable for future manual re-run.
- **Salesforce?** Untouched. Retains existing placeholder behavior.
- **Deactivation semantics?** `is_active=false` on missing rows. No soft or hard delete.
- **Tests in plan?** Single end-phase task.

## 16. Documentation Changes Required After Implementation

- `src/workers/integrations_worker/README.md` — document the Tier 3 schema task, the Tier 1 Beat scheduler, and the fresh-session flow.
- `docs/Technical Documentation/integrations/architecture-overview.md` — state current truth: HubSpot custom OAuth live, HubSpot deal schema sync live, HubSpot record sync not yet implemented.
- `docs/Technical Documentation/integrations/sync-by-provider.md` — HubSpot CRM section: deal schema sync supported, task name, 14-day refresh, canonical-vs-custom persistence rule.

## 17. Implementation Readiness

This design is ready to convert into a step-by-step implementation plan. The plan will sequence:

1. Mapping file + classifier + normalizer (pure functions).
2. Fetcher.
3. Registry writer.
4. Service orchestrator + `SchemaSyncResult`.
5. Tier 3 Celery task.
6. Tier 1 fan-out task + schedule metadata.
7. Post-connect dispatch update.
8. Documentation updates.
9. Tests (single end-phase task covering everything above).

No internal CRM service or handler is modified in this plan. Read-path isolation lands with the consumption-layer design.
