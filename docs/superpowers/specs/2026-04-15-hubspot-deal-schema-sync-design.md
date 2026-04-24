# HubSpot Deal Schema Sync Design

## Goal

Build the first CRM data-sync sub-project: fetch HubSpot deal property schema and store it in the
normalized CRM field-definition registry so later deal/contact/company sync can reuse the same
metadata contract.

This phase is intentionally limited to **schema bootstrap only**:

- fetch HubSpot **deal** property definitions
- normalize them into provider-backed `CRMCustomFieldDefinition` rows
- keep internal CRM custom-column flows separate
- do **not** sync deal records yet
- do **not** sync pipelines/stages yet
- do **not** add enrichment yet

## Scope

### In scope

- reusable CRM schema-sync folder structure under `src/service/integrations/crm/`
- internal custom-field and deal-detail read paths hardened to `field_source=internal`
- HubSpot deal-schema fetcher and sync service
- provider-backed field-definition upsert into `crm_custom_field_definitions`
- targeted tests and technical documentation updates

### Out of scope

- HubSpot deal record sync
- HubSpot pipelines/stages sync
- HubSpot associations sync
- Salesforce schema sync
- internal-CRM/external-CRM mixed-write enablement
- frontend settings/UI changes for provider-backed fields

## Why schema first

Schema is the base dependency for every later CRM entity sync.

Without local schema definitions:

- custom provider fields are opaque JSON keys
- type-aware parsing is weak
- enum/select validation is weak
- writeback serialization is unsafe
- later enrichment logic has no authoritative field metadata to reference

Deal schema is the smallest useful slice because it establishes the shared pattern we will later
reuse for contacts, companies/accounts, users, activities, and pipelines.

## Current repo constraints

### 1. OAuth and token lifecycle already exist

HubSpot custom OAuth is already implemented through the first-party provider architecture, and token
refresh/revoke is already handled by the shared integration services.

### 2. Internal custom columns already exist

The existing custom-column system under `src/service/crm/internal/` is explicitly for
**internally-managed** CRM custom columns. That API and service layer should not become the place
where provider schema is mutated by users.

That means provider-backed rows cannot be safely written into the shared registry table until the
current internal read/write paths explicitly filter `field_source=internal`.

### 3. CRM schema table now supports provider provenance

`CRMCustomFieldDefinition` has been extended to represent both:

- `field_source=internal`
- `field_source=provider`

with provider provenance columns such as `integration_id`, `provider_name`, and provider metadata.

### 4. The old CRM sync implementation is gone

There is no active HubSpot/Salesforce CRM sync pipeline in `workers/integrations_worker`; only
stale docs and `__pycache__` remain. That means we should establish the real CRM service/fetcher
structure now instead of preserving a deprecated layout.

## Alternatives considered

### Option 1: schema-only bootstrap first

- fetch HubSpot deal property definitions
- normalize and store them
- stop there

**Pros**

- smallest safe production slice
- creates the metadata contract every later CRM sync needs
- avoids mixing object-schema, pipelines, and records in one rollout

**Cons**

- no end-user visible deal records yet

### Option 2: schema plus pipelines/stages

- fetch deal properties and pipeline metadata together

**Pros**

- stronger metadata completeness for later deal sync

**Cons**

- widens phase-one scope immediately
- mixes two separate metadata domains

### Option 3: schema plus first deal sync

- fetch schema and records in the same rollout

**Pros**

- fastest visible CRM data output

**Cons**

- weaker architecture discipline
- record sync begins before schema persistence contract is stable

### Recommendation

Use **Option 1** now.

It is the cleanest first sub-project and gives us the least rework when contacts, companies,
pipelines, and deal records are added later.

However, because provider-backed schema rows will live in the same registry table as internal custom
columns, the first implementation plan must begin by hardening the current internal custom-column
paths to `field_source=internal`.

## Architecture

### Design principles

1. Follow the existing integration architecture: provider fetch layer plus sync service layer.
2. Keep internal-CRM services (`src/service/crm/internal/*`) separate from provider sync logic.
3. Create a real CRM subtree under `src/service/integrations/crm/` now so later entity syncs land
   in the same structure.
4. Keep schema sync independent from record sync.
5. Shared registry table, separate read paths: internal APIs/services only read `field_source=internal`;
   provider sync only writes and reads `field_source=provider`.
6. Normalize provider data into stable internal shapes before writing to the DB.

### Folder structure

```text
src/service/integrations/crm/
  __init__.py
  common/
    __init__.py
    schema_types.py
    field_type_mapper.py
    field_registry_service.py
  schema/
    __init__.py
    schema_sync_result.py
    hubspot_deal_schema_service.py

src/workers/integrations_worker/providers/crm/
  __init__.py
  hubspot/
    __init__.py
    deal_schema_fetcher.py
```

### Responsibilities

#### `field_registry_service.py`

Owns persistence rules for provider-backed schema rows in `CRMCustomFieldDefinition`.

Responsibilities:

- upsert provider-backed field definitions
- soft-delete or deactivate stale provider rows when needed
- filter by `field_source=provider`
- never mutate internal custom-column rows
- apply snapshot semantics only within an explicit entity scope

This is the core reusable service. Future HubSpot contacts schema, Salesforce opportunity schema,
and account/company schema should all reuse this same registry path.

This service is not used by internal custom-column UI flows.

#### `schema_types.py`

Defines provider-neutral dataclasses/enums for schema payloads before they hit the DB.

Example concepts:

- normalized field definition
- normalized select option
- provider schema sync request metadata

This keeps fetchers from returning arbitrary dicts and keeps the sync service from knowing raw
HubSpot payload structure everywhere.

#### `field_type_mapper.py`

Maps provider-specific field metadata into `CRMCustomFieldDataType`.

For HubSpot, it should translate combinations like:

- `string` -> `text`
- `number` -> `number`
- `date` -> `date`
- `datetime` -> `datetime`
- `bool` -> `boolean`
- `calculation_equation` -> `formula`
- enumeration/select -> `select`
- enumeration/booleancheckbox -> `select`
- multi-checkbox -> `multiselect`

Unknown/unsupported types should degrade predictably, most likely to `text`, while preserving the
raw payload in `data_payload`.

#### `deal_schema_fetcher.py`

Owns HubSpot API fetch behavior only.

Responsibilities:

- call the HubSpot deal properties endpoint
- handle pagination if needed
- apply retry/rate-limit logic
- return raw HubSpot property definitions

It should not know anything about our DB model.

#### `hubspot_deal_schema_service.py`

Orchestrates:

1. resolve access token
2. fetch raw schema
3. normalize provider payloads
4. persist them through `FieldRegistryService`
5. return sync counters/result metadata

This service is the phase-one entrypoint for “sync HubSpot deal schema”.

#### `schema_sync_result.py`

Provides a typed result object for logs, tests, and future sync history.

Suggested counters:

- fetched
- created
- updated
- marked_inactive
- unchanged
- failed

## Data model contract

Provider-backed deal schema will be stored in `CRMCustomFieldDefinition` with:

- `organisation_id`
- `integration_id`
- `entity_type=deal`
- `field_source=provider`
- `provider_name=hubspot`
- `key`
- `external_id` if HubSpot exposes a separate stable field identifier worth storing
- `label`
- `data_type`
- `description`
- `options`
- `required`
- `is_provider_custom`
- `data_payload`
- `source_modified_at`
- `last_seen_at`

### Key rules

1. Internal custom-column rows remain untouched.
2. Internal custom-field APIs and internal deal responses must query `field_source=internal`.
3. Provider-backed rows are filtered by `integration_id` and `field_source=provider`.
4. `key` is the canonical field key used by later record sync and later UI resolution.
5. Raw provider schema stays available in `data_payload` for debugging and future expansion.
6. Missing-row deactivation must run against an explicit entity scope; empty snapshots cannot imply
   "all entities" for the integration.

## HubSpot API behavior

For phase one, the primary source should be the HubSpot deal properties API, not deal records.

Expected metadata needed from HubSpot property definitions:

- internal property name
- label
- type / field type
- description
- option lists for select-like fields
- requiredness or equivalent validation signal if exposed
- custom vs default/system signal
- modification metadata when available

We are not relying on pipeline metadata yet in this phase.

## Sync flow

```text
Task entrypoint / manual trigger
  -> load KnowledgeIntegration
  -> resolve valid HubSpot access token
  -> deal_schema_fetcher.fetch()
  -> normalize each property into schema_types.NormalizedCRMFieldDefinition
  -> FieldRegistryService.upsert_provider_fields(...)
  -> return SchemaSyncResult
```

### Session pattern

Follow the same fresh-session rule used elsewhere:

- do not hold DB session during HubSpot API calls
- load integration/token data
- release session
- fetch from HubSpot
- acquire fresh session for DB writes

## Folder strategy for future CRM work

This first phase should intentionally set the permanent pattern.

Later expansion should look like:

```text
src/service/integrations/crm/
  common/
  schema/
  records/
  associations/
  enrichment/
```

And under worker/provider fetchers:

```text
src/workers/integrations_worker/providers/crm/
  common/
  hubspot/
  salesforce/
```

That gives us one predictable home for:

- schema fetch/sync
- record fetch/sync
- association sync
- enrichment orchestration

without pushing CRM code back into top-level `src/service/integrations/*.py`.

## Error handling

### Fetch-level failures

- 401 should use the shared token-resolution path
- 429 should retry with backoff
- 5xx should raise a sync failure with enough context for logs/history

### Normalization failures

If one field payload is malformed:

- log the bad field key
- count it as failed
- continue processing the rest when safe

Schema sync should be best-effort per field, not all-or-nothing, unless the entire provider payload
is invalid.

### Persistence failures

Registry writes should be atomic per batch/transaction.

If the write phase fails:

- roll back the transaction
- log the integration id, org id, provider, entity type
- return/raise a failure suitable for `IntegrationSyncHistory` once CRM sync history is wired

## Testing strategy

### Unit tests

1. field type mapping
2. HubSpot payload normalization
3. field-registry upsert behavior
4. stale provider-field deactivation behavior
5. internal custom-field services filter to `field_source=internal`
6. HubSpot schema service orchestration

### Service-level tests

1. internal custom-column APIs do not return provider-backed rows
2. internal deal detail/custom-column reads do not include provider-backed rows
3. sync creates provider-backed field rows
4. rerun updates changed labels/options
5. rerun leaves unchanged rows stable
6. malformed field payload only fails that row
7. internal custom-column rows are never returned/mutated by provider sync

### Non-goals for this phase

- live HubSpot integration tests
- deal record sync tests
- pipeline sync tests

## Documentation updates required

When implemented, these docs should be updated:

- `docs/Technical Documentation/integrations/architecture-overview.md`
- `docs/Technical Documentation/integrations/sync-by-provider.md`
- `docs/API Documentation/deals/crm-custom-fields-api.md`

The custom-fields API doc especially needs one clarification:

- provider-backed rows may exist in the same registry table
- but the existing internal custom-column API remains internal-only and must not edit provider rows

## Rollout sequence

### Phase 1

- harden internal custom-field services, import paths, and deal-detail custom-column reads to
  `field_source=internal`
- create CRM schema folder structure
- implement HubSpot deal schema fetcher
- implement normalized schema types and field mapper
- implement field registry service
- implement HubSpot deal schema sync service
- add tests

### Phase 2

- add first trigger path for schema sync
- add sync-history recording once the CRM worker trigger exists

### Later phases

- HubSpot pipelines/stages schema
- HubSpot deal records
- HubSpot contacts/companies/users
- Salesforce schema using the same framework
- enrichment services on top of schema + records

## Open decisions resolved for this spec

- **Schema first or records first?** Schema first.
- **Reuse top-level integrations files or create CRM subtree?** Create the CRM subtree.
- **Reuse internal custom-field services for provider sync?** No.
- **Use one shared field registry table?** Yes, with source/provenance separation.
- **Can provider rows be written before internal filtering exists?** No.
- **Implement only HubSpot deal schema now?** Yes.

## Summary

The correct first CRM sync slice is a **HubSpot deal-schema bootstrap** built on a reusable CRM
schema-sync framework.

That gives us:

- a durable folder structure
- a durable field-registry contract
- clean separation from internal custom-column management
- a stable base for every later CRM entity sync

without prematurely coupling records, pipelines, and enrichment into the first rollout.
