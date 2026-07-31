# CRM Tenant Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete rollout Task 4 so CRM rows cannot reference or collide with
rows owned by another organisation.

**Architecture:** Keep globally unique UUID primary keys, but add
`(organisation_id, id)` candidate keys to every parent reached by a CRM
relationship. Replace each CRM-side single-column foreign key with a composite
foreign key that maps organisation to organisation, and scope the four CRM
business/provider unique indexes by organisation. The migration builds new
indexes first, validates new foreign keys before dropping old ones, and never
changes runtime routing or service APIs.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy, PostgreSQL 16, Alembic,
pytest.

---

## Current Evidence and Scope

The Task 1 model audit currently finds:

- 23 tables declared by
  `src.domain.models.organisation_tables.sync.crm`
- 59 CRM-side foreign keys to tenant tables without tenant identity
- 4 CRM unique indexes without tenant identity
- 10 referenced parent identities requiring composite candidate keys
- 37 CRM relationships without an existing
  `(organisation_id, foreign_key_column)` index

Task 4 owns all findings whose child model is declared in the CRM package.
Task 5 owns inbound links from chat, calendar, email, messaging, knowledge,
meeting, agent, and job models.

The ten parent identities are:

```text
crm_companies.company_id
crm_contacts.contact_id
crm_deal_stages.stage_id
crm_deals.deal_id
crm_leads.lead_id
crm_pipelines.pipeline_id
crm_users.user_id
deal_jobs.job_id
knowledge_integrations.integration_id
lead_forms.form_id
```

The four business/provider indexes are:

```text
crm_custom_field_definitions.uq_crm_custom_field_provider_key_state
crm_lead_participants.uq_crm_lead_participant_local_role
crm_lead_participants.uq_crm_lead_participant_primary_role
lead_form_fields.uq_lead_form_field_key_active
```

## Invariants

1. A CRM foreign key maps
   `(child.organisation_id, child.reference_id)` to
   `(parent.organisation_id, parent.id)`.
2. Both sides include `organisation_id` in the same ordinal position.
3. Each parent pair is backed by a unique candidate key.
4. Each child pair has an organisation-leading index.
5. Nullable business references remain nullable; composite PostgreSQL foreign
   keys preserve `MATCH SIMPLE`.
6. Existing `ON DELETE CASCADE` behavior remains only on
   `crm_import_rows.import_job_id` and
   `crm_source_object_mappings.integration_id`.
7. New foreign keys are added `NOT VALID`, validated, and only then replace
   the old single-column constraint.
8. Existing globally unique UUID primary keys are not replaced.
9. Runtime providers, master tables, service queries, and schema routing do not
   change.
10. The migration accepts the same validated `schema` argument as revision
    082 and does not infer an organisation from table contents.

## File Map

### Audit contract

- Modify:
  `scripts/maintenance/audit_shared_tenant_readiness.py`
- Modify:
  `tests/scripts/maintenance/test_audit_shared_tenant_readiness.py`
- Create:
  `tests/migrations/org/test_083_harden_crm_tenant_integrity.py`

### CRM metadata

- Create:
  `src/domain/models/organisation_tables/sync/crm/base.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/activities.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/associations.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/companies.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/contacts.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/custom_fields.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/deals.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/imports.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/lead_forms.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/leads.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/source_mappings.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/users.py`
- Modify:
  `src/domain/models/organisation_tables/job_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/integration_models.py`

### Database transition

- Create:
  `migrations/org_migrations/versions/3c7f79637716_083_harden_crm_tenant_integrity.py`

## Task 4.1: Make the Audit Prove Both Sides of Tenant Foreign Keys

- [x] **Step 1: Add a failing malformed-composite test**

Create a parent and child with this constraint:

```python
ForeignKeyConstraint(
    ["organisation_id", "parent_id"],
    ["parents.id", "parents.id"],
    name="fk_children_parent_bad_tenant_mapping",
)
```

Also test a constraint whose local columns contain `organisation_id` but whose
remote columns do not. Both must appear in
`unscoped_tenant_foreign_keys`.

- [x] **Step 2: Verify red**

Run:

```powershell
uv run pytest `
  tests/scripts/maintenance/test_audit_shared_tenant_readiness.py -q
```

Expected: the new malformed-composite tests fail because the current audit
checks only local column names.

- [x] **Step 3: Implement the exact mapping check**

A tenant foreign key is scoped only when:

```python
local_columns[0] == "organisation_id"
remote_columns[0] == "organisation_id"
```

and the first foreign-key element maps the local organisation column to the
remote organisation column. Keep master-table references exempt because their
target table is not in the tenant-table classification.

- [x] **Step 4: Verify green**

Run the same test file and require every audit contract test to pass.

## Task 4.2: Lock the CRM Metadata Contract

- [x] **Step 1: Add a failing CRM-derived contract**

The test discovers CRM tables from SQLModel classes whose module starts with:

```python
"src.domain.models.organisation_tables.sync.crm"
```

It must prove:

- no CRM-prefixed finding remains in `unscoped_tenant_foreign_keys`
- no CRM-prefixed finding remains in `unscoped_unique_rules`
- every CRM tenant foreign key contains
  `("organisation_id", local_id)` on the child and
  `("organisation_id", remote_id)` on the parent
- every referenced parent pair has a unique constraint or unique index
- every child pair is the leading prefix of an index or unique constraint

The test compares the relationships derived from model metadata with the
migration inventory, so the migration cannot silently omit a CRM relationship.

- [x] **Step 2: Verify red**

Run:

```powershell
uv run pytest `
  tests/migrations/org/test_083_harden_crm_tenant_integrity.py -q
```

Expected: fail because revision 083 and composite CRM model constraints do not
exist.

## Task 4.3: Declare Tenant-Qualified CRM Relationships

- [x] **Step 1: Add CRM-only synced mixins**

`crm/base.py` defines CRM versions of the existing synced mixins. They retain
the current fields but override `integration_id` without the inherited
single-column foreign key:

```python
class CRMSyncedRecordMixin(SyncedRecordMixin):
    integration_id: UUID = Field(
        index=True,
        description="Integration owning this CRM record.",
    )


class CRMFlexibleSyncedRecordMixin(FlexibleSyncedRecordMixin):
    integration_id: UUID | None = Field(
        default=None,
        index=True,
        description="Integration owning this CRM record.",
    )
```

The same module exposes one declaration helper:

```python
def tenant_foreign_key(
    table_name: str,
    local_column: str,
    target_table: str,
    target_column: str,
    *,
    ondelete: str | None = None,
) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organisation_id", local_column],
        [
            f"{target_table}.organisation_id",
            f"{target_table}.{target_column}",
        ],
        name=f"fk_{table_name}_{local_column}_org",
        ondelete=ondelete,
    )
```

- [x] **Step 2: Replace CRM single-column declarations**

Use the CRM-only mixins for synced CRM tables. Remove direct
`foreign_key=...` declarations for other CRM relationships and add one
`tenant_foreign_key(...)` entry to the owning table's `__table_args__`.
Preserve nullability and the two existing cascade actions.

- [x] **Step 3: Add candidate keys and supporting indexes**

Add `UniqueConstraint("organisation_id", id_column, ...)` for the ten parent
pairs. Add only the 37 missing child indexes, with
`organisation_id` as the first column.

- [x] **Step 4: Scope the four CRM unique indexes**

Prepend `organisation_id` to each existing unique index and give the new index
a distinct `_org` name. Preserve every existing partial predicate.

- [x] **Step 5: Verify metadata green**

Run the Task 4 migration contract and the model audit. The CRM-derived
sub-report must be empty while non-CRM Task 5 findings remain.

## Task 4.4: Add Revision 083

- [x] **Step 1: Define immutable migration inventories**

Revision `3c7f79637716` follows `3c7f79637717` and contains:

- `CRM_CANDIDATE_KEYS`: the ten parent pairs
- `CRM_TENANT_FOREIGN_KEYS`: the 59 model-derived relationships
- `CRM_FOREIGN_KEY_INDEXES`: the 37 missing child indexes
- `CRM_SCOPED_UNIQUE_INDEXES`: the four replacement indexes and predicates

All table and column names are fixed source constants; only the validated
Alembic `schema` argument is dynamic.

- [x] **Step 2: Build new indexes before constraints**

Create parent unique indexes and missing child indexes with deterministic
names. Use PostgreSQL concurrent index creation through Alembic's autocommit
block. Treat a deterministic existing valid index/constraint as a resumable
retry; reject a conflicting invalid object rather than guessing.

- [x] **Step 3: Attach and validate composite constraints**

Attach parent candidate indexes as unique constraints. Add each composite
foreign key with `postgresql_not_valid=True`, validate it, then discover and
drop the matching legacy single-column foreign key through a parameterized
`pg_catalog` query.

- [x] **Step 4: Replace CRM business uniqueness**

Create the tenant-scoped replacement index before dropping the stronger legacy
index. Preserve partial predicates exactly.

- [x] **Step 5: Implement downgrade**

Downgrade recreates legacy unique indexes and single-column foreign keys before
removing scoped rules. It then drops composite child indexes and parent
candidate constraints in dependency-safe reverse order.

- [x] **Step 6: Verify migration contract**

Run:

```powershell
uv run pytest `
  tests/migrations/org/test_083_harden_crm_tenant_integrity.py `
  tests/migrations/org/test_082_add_missing_organisation_identity.py `
  tests/migrations/test_org_migration_head_contract.py -q

uv run alembic -c migrations/org_migrations/alembic.ini heads
```

Expected head:

```text
3c7f79637716 (head)
```

## Final Verification

- [x] Run the focused migration and audit tests.
- [x] Run CRM service/model tests that construct or query affected models.
- [x] Run the model readiness audit and prove:
  - zero CRM unscoped foreign keys
  - zero CRM unscoped unique rules
  - non-CRM findings remain assigned to Task 5
- [x] Run Ruff on every Task 4 file.
- [x] Run the Python comment-smell scanner on new Task 4 modules/tests.
- [x] Run `git diff --check`.
- [x] Review staged versus unstaged state so the existing Task 1-3 staged slice
  and unrelated documentation remain preserved.

**Task 4 exit gate:** CRM model metadata and revision 083 jointly prevent a
CRM child row from referencing a parent owned by another organisation, while
allowing the same provider/business identifier in different organisations.
