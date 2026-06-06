# CRM External ID Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move HubSpot, Salesforce, and future migration source identifiers out of canonical CRM tables and into a dedicated CRM source mapping layer.

**Architecture:** Canonical CRM tables should represent local product state using local UUIDs and local foreign keys. Provider and migration identities should exist only at integration boundaries through `crm_source_object_mappings`. Internal CRM flows should not create fake external IDs.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy, PostgreSQL JSONB, Alembic org migrations, pytest.

---

## Decision Summary

The current design stores provider identity directly in CRM tables through fields such as `external_id`, `owner_external_id`, `stage_external_id`, `pipeline_external_id`, `related_external_id`, and provider `data_payload`. That makes internal CRM and external CRM behavior hard to separate because local rows, HubSpot rows, Salesforce rows, and future migration rows all share the same columns.

The new design introduces a CRM-scoped source mapping table. Business logic works with local IDs only. Integration logic resolves provider IDs through the mapping table when syncing, receiving webhooks, handling CDC deletes, or writing back to the provider.

This is not a data-preserving migration. The product is not live for users yet, so no backfill is required. Schema migrations are still required so new org schemas get the correct structure.

## Full Roadmap

### Step 1: Add Source Mapping Foundation

Add `crm_source_object_mappings` as an additive org-schema table, register the model, add an Alembic migration, and add focused tests for mapping constraints. Do not remove old CRM external-id columns yet.

### Step 2: Move Provider Writers

Update HubSpot and Salesforce record writers so they create/update canonical CRM rows by local IDs and upsert mapping rows for provider identities and raw provider payloads. Mapping upsert should happen in the same logical write path as canonical row persistence.

### Step 3: Move Provider Readers, Webhooks, and CDC

Replace direct `CRM*.external_id` lookups in sync, webhook, deletion reconciliation, privacy deletion, and Salesforce CDC paths with mapping-table lookups. Provider events should resolve to a local row first, then mutate canonical CRM data by local ID.

### Step 4: Clean Internal CRM

Stop internal CRM services from creating fake external IDs such as `str(deal_id)`, `str(task_id)`, `str(stage_id)`, and `str(user_id)`. Internal CRM should use local primary keys and local foreign keys only.

### Step 5: Drop Old Provider Columns

After all code paths stop depending on old provider columns, remove provider identity columns and provider raw payload columns from canonical CRM models and org migrations. This includes old uniqueness constraints and indexes based on external IDs.

### Step 6: Update API, Agent, Timeline, and Docs Surface

Remove user-facing reliance on `external_id` and `*_external_id` fields in API schemas, agent tool responses, timeline source IDs, and CRM documentation. Expose canonical local IDs and controlled source metadata instead.

## Known Loopholes and Fixes

- **Loophole:** Dropping all `data_payload` fields blindly can remove product-owned private lead/form payloads.
  **Fix:** Move only provider raw payloads into source mappings. Keep product-owned payloads where they are domain data.

- **Loophole:** Removing direct external IDs can break webhook and CDC delete lookup.
  **Fix:** Webhook and CDC handlers must resolve provider object IDs through `crm_source_object_mappings` before touching canonical rows.

- **Loophole:** Mapping rows are polymorphic, so `local_object_id` cannot have a normal FK to every CRM table.
  **Fix:** Enforce source identity uniqueness in Postgres and validate local target existence in application tests and mapping helper code.

- **Loophole:** Soft-deleted mappings can allow duplicate source identities if uniqueness only applies to active rows.
  **Fix:** Source identity uniqueness must ignore `is_deleted`; reactivation should update the same mapping row.

- **Loophole:** Internal CRM currently uses fake external IDs for local objects.
  **Fix:** Internal CRM must stop writing fake external IDs in Step 4 and use local IDs only.

- **Loophole:** Multiple sources can point at the same local object, but source precedence is not defined.
  **Fix:** Step 1 only enables mapping. Since the app currently allows one CRM source, no multi-source merge policy is implemented yet.

---

## Step 1 Implementation Plan

### Step 1 Goal

Create the mapping table and metadata surface without changing current CRM behavior.

### Files

- Create: `src/domain/models/organisation_tables/sync/crm/source_mappings.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/__init__.py`
- Modify: `src/domain/models/organisation_tables/sync/__init__.py`
- Modify: `src/domain/models/organisation_tables/__init__.py`
- Modify: `migrations/org_migrations/env.py`
- Create: `migrations/org_migrations/versions/3c7f79637743_056_add_crm_source_object_mappings.py`
- Create: `tests/domain/models/organisation_tables/sync/crm/test_source_mappings.py`

### Data Model Contract

`CRMSourceObjectMapping` should use table name `crm_source_object_mappings`.

Required columns:

- `mapping_id UUID PRIMARY KEY`
- `organisation_id UUID NOT NULL`
- `source_kind TEXT NOT NULL`
- `integration_id UUID NULL`
- `source_run_id UUID NULL`
- `provider TEXT NOT NULL`
- `external_object_type TEXT NOT NULL`
- `external_id TEXT NOT NULL`
- `local_object_type TEXT NOT NULL`
- `local_object_id UUID NOT NULL`
- `data_payload JSONB NULL`
- `mapping_metadata JSONB NOT NULL DEFAULT '{}'::jsonb`
- `source_created_at TIMESTAMPTZ NULL`
- `source_modified_at TIMESTAMPTZ NULL`
- `synced_at TIMESTAMPTZ NOT NULL`
- `last_seen_at TIMESTAMPTZ NULL`
- `is_deleted BOOLEAN NOT NULL DEFAULT false`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

Use `mapping_metadata` instead of `metadata` to avoid SQLAlchemy declarative naming conflicts.

Source kind values:

- `integration`
- `migration`

Recommended object type values:

- `deal`
- `contact`
- `company`
- `user`
- `pipeline`
- `stage`
- `activity`
- `note`
- `task`
- `association`
- `lead`
- `lead_participant`
- `deal_participant`
- `stage_history`
- `note_link`

Provider values are plain strings such as `hubspot`, `salesforce`, or a future migration source name.

### Constraints and Indexes

Add check constraints:

- `ck_crm_source_mapping_source_kind`: `source_kind IN ('integration', 'migration')`
- `ck_crm_source_mapping_source_ref`: integration rows require `integration_id IS NOT NULL AND source_run_id IS NULL`; migration rows require `source_run_id IS NOT NULL AND integration_id IS NULL`

Add foreign key:

- `integration_id` references `knowledge_integrations.integration_id` with `ON DELETE CASCADE`

Add unique indexes:

- `uq_crm_source_mapping_integration_identity` on `(organisation_id, integration_id, provider, external_object_type, external_id)` where `source_kind = 'integration'`
- `uq_crm_source_mapping_migration_identity` on `(organisation_id, source_run_id, provider, external_object_type, external_id)` where `source_kind = 'migration'`

Add lookup indexes:

- `ix_crm_source_mapping_source_lookup` on `(organisation_id, provider, external_object_type, external_id)`
- `ix_crm_source_mapping_local_lookup` on `(organisation_id, local_object_type, local_object_id)`
- `ix_crm_source_mapping_integration` on `(organisation_id, integration_id)`
- `ix_crm_source_mapping_active` on `(organisation_id, is_deleted)`

### Task 1: Create Model

- [ ] Add `source_mappings.py` with enum classes for source kind and object type.
- [ ] Define `CRMSourceObjectMapping` as a SQLModel table.
- [ ] Use `Column(JSONB, server_default=text(EMPTY_JSONB_SQL), nullable=False)` for `mapping_metadata`.
- [ ] Use `Column(DateTime(timezone=True), nullable=False)` for `synced_at`.
- [ ] Use server defaults for `is_deleted`, `created_at`, and `updated_at` consistent with existing org models.

Verify:

```powershell
uv run python -m py_compile src/domain/models/organisation_tables/sync/crm/source_mappings.py
```

Expected: command exits successfully.

### Task 2: Register Model Exports

- [ ] Import and export `CRMSourceObjectMapping`, `CRMSourceKind`, and `CRMSourceObjectType` from `src/domain/models/organisation_tables/sync/crm/__init__.py`.
- [ ] Import and export the same symbols from `src/domain/models/organisation_tables/sync/__init__.py`.
- [ ] Import and export the same symbols from `src/domain/models/organisation_tables/__init__.py`.

Verify:

```powershell
uv run python -c "from src.domain.models.organisation_tables import CRMSourceObjectMapping; print(CRMSourceObjectMapping.__tablename__)"
```

Expected output:

```text
crm_source_object_mappings
```

### Task 3: Register Alembic Metadata

- [ ] Add `CRMSourceObjectMapping` to imports in `migrations/org_migrations/env.py`.
- [ ] Add `CRMSourceObjectMapping` to `all_org_models`.
- [ ] Do not refactor unrelated stale registrations in `env.py`.

Verify:

```powershell
uv run python -m py_compile migrations/org_migrations/env.py
```

Expected: command exits successfully.

### Task 4: Add Org Migration

- [ ] Create `migrations/org_migrations/versions/3c7f79637743_056_add_crm_source_object_mappings.py`.
- [ ] Set `revision = "3c7f79637743"`.
- [ ] Set `down_revision = "3c7f79637744"`.
- [ ] In `upgrade()`, create only `crm_source_object_mappings`.
- [ ] In `downgrade()`, drop only `crm_source_object_mappings`.
- [ ] Do not backfill existing CRM tables.
- [ ] Do not drop existing CRM external-id columns.

Verify:

```powershell
uv run python -m py_compile migrations/org_migrations/versions/3c7f79637743_056_add_crm_source_object_mappings.py
```

Expected: command exits successfully.

### Task 5: Add Focused Tests

- [ ] Create `tests/domain/models/organisation_tables/sync/crm/test_source_mappings.py`.
- [ ] Test enum values match the contract.
- [ ] Test table name is `crm_source_object_mappings`.
- [ ] Test required indexes and constraints are present in model metadata.
- [ ] Test `mapping_metadata` uses the database column name `mapping_metadata`.

Suggested test shape:

```python
from src.domain.models.organisation_tables import (
    CRMSourceKind,
    CRMSourceObjectMapping,
    CRMSourceObjectType,
)


def test_source_mapping_table_name() -> None:
    assert CRMSourceObjectMapping.__tablename__ == "crm_source_object_mappings"


def test_source_kind_values() -> None:
    assert CRMSourceKind.INTEGRATION.value == "integration"
    assert CRMSourceKind.MIGRATION.value == "migration"


def test_core_object_type_values() -> None:
    assert CRMSourceObjectType.DEAL.value == "deal"
    assert CRMSourceObjectType.CONTACT.value == "contact"
    assert CRMSourceObjectType.COMPANY.value == "company"
    assert CRMSourceObjectType.USER.value == "user"
    assert CRMSourceObjectType.NOTE_LINK.value == "note_link"


def test_source_mapping_has_required_constraints_and_indexes() -> None:
    table = CRMSourceObjectMapping.__table__
    constraint_names = {constraint.name for constraint in table.constraints}
    index_names = {index.name for index in table.indexes}

    assert "ck_crm_source_mapping_source_kind" in constraint_names
    assert "ck_crm_source_mapping_source_ref" in constraint_names
    assert "uq_crm_source_mapping_integration_identity" in index_names
    assert "uq_crm_source_mapping_migration_identity" in index_names
    assert "ix_crm_source_mapping_source_lookup" in index_names
    assert "ix_crm_source_mapping_local_lookup" in index_names


def test_mapping_metadata_column_name_is_not_sqlalchemy_metadata() -> None:
    assert "mapping_metadata" in CRMSourceObjectMapping.__table__.columns
    assert "metadata" not in CRMSourceObjectMapping.__table__.columns
```

Verify:

```powershell
uv run pytest tests/domain/models/organisation_tables/sync/crm/test_source_mappings.py -v
```

Expected: all tests pass.

### Task 6: Final Step 1 Verification

- [ ] Run focused tests.
- [ ] Run changed-file compile checks.
- [ ] Run `git diff --stat` and confirm only Step 1 files changed.

Commands:

```powershell
uv run pytest tests/domain/models/organisation_tables/sync/crm/test_source_mappings.py -v
uv run python -m py_compile src/domain/models/organisation_tables/sync/crm/source_mappings.py
uv run python -m py_compile migrations/org_migrations/env.py
uv run python -m py_compile migrations/org_migrations/versions/3c7f79637743_056_add_crm_source_object_mappings.py
git diff --stat
```

Expected:

- Source mapping tests pass.
- Compile checks pass.
- Diff contains only the new mapping model, export updates, Alembic metadata registration, migration, and focused test file.

## Step 1 Non-Goals

- Do not drop any existing CRM external-id columns.
- Do not rewrite HubSpot or Salesforce writers.
- Do not change internal CRM services yet.
- Do not change API response schemas yet.
- Do not implement migration/import workflows beyond schema readiness.

## Step 1 Completion Criteria

Step 1 is complete when the new mapping model exists, Alembic can see it, the org migration creates it, and focused tests prove the source identity constraints and lookup metadata.
