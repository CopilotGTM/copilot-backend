# CRM Schema Multi-CRM Support Changelog

**Created:** December 16, 2025  
**Last Updated:** December 16, 2025  
**Version:** 1.0

---

## Executive Summary

This document captures all changes made to evolve the CRM integration schema from a HubSpot-centric design to a **multi-CRM-ready architecture** supporting HubSpot, Salesforce, Pipedrive, and Zoho CRMs. The changes enable the platform to:

1. **Integrate multiple CRM providers** with a provider-agnostic schema
2. **Handle sales and pre-sales workflows** across different CRM models
3. **Support future evolution** toward "our own CRM" as internal objects become the source of truth

---

## Goals and Outcomes

### Original Goals

| Goal | Description | Status |
|------|-------------|--------|
| Multi-CRM Integration | Schema works for HubSpot, Salesforce, Pipedrive, Zoho | ✅ Achieved |
| Provider-Agnostic Activities | Support Salesforce Task/Event, HubSpot engagements, Pipedrive activities | ✅ Achieved |
| Contact↔Company Relationships | Normalize many-to-many associations | ✅ Achieved |
| Consistent Owner Resolution | FK-based owner filtering across entities | ✅ Achieved |
| Cross-CRM Analytics | Canonical enums for consistent reporting | ✅ Achieved |
| Internal Object Creation | Allow stakeholders without CRM integration | ✅ Achieved |
| Future "Our Own CRM" | Architectural path to canonical internal layer | ✅ Documented |

### What Was Deferred

| Feature | Reason | Documentation |
|---------|--------|---------------|
| Core CRM Layer | No multi-CRM customers yet; adds complexity | [core-crm-layer-analysis.md](../Future%20Functionality%20Documentation/core-crm-layer-analysis.md) |
| Field Metadata Storage | Write-back not required yet | [crm-field-metadata-and-writeback.md](../Future%20Functionality%20Documentation/crm-field-metadata-and-writeback.md) |
| Products/Line Items | CPQ/billing-adjacent; lower priority for pre-sales | Proposals doc |
| IntegrationParticipant Cross-Integration | Identity layer refactor; separate initiative | Proposals doc |

---

## Changes by Priority

### P0: Fix Data Ownership (Stakeholders)

**Problem:** `DealStakeholder` required `integration_id`, blocking internal stakeholder creation from calendar/email discovery or manual entry.

**Solution:** Made `integration_id` nullable using `FlexibleSyncedRecordMixin`.

| Artifact | Path |
|----------|------|
| Migration | [migrations/org_migrations/versions/8f1c2d3e4a5b_014_make_deal_stakeholders_integration_id_nullable.py](../../migrations/org_migrations/versions/8f1c2d3e4a5b_014_make_deal_stakeholders_integration_id_nullable.py) |
| Model | [src/domain/models/organisation_tables/sync/crm/stakeholders.py](../../src/domain/models/organisation_tables/sync/crm/stakeholders.py) |

**Key Changes:**

- Changed `DealStakeholder` from `SyncedRecordMixin` to `FlexibleSyncedRecordMixin`
- `integration_id` is now nullable with FK constraint preserved
- Supports `MANUAL`, `CALENDAR_DISCOVERY`, `EMAIL_DISCOVERY` sources

---

### P1: Generic CRM Object Associations

**Problem:** No way to represent arbitrary CRM relationships (contact↔company, activity↔contact, deal↔products). Each relationship required schema changes.

**Solution:** Created `crm_object_associations` table for polymorphic many-to-many relationships.

| Artifact | Path |
|----------|------|
| Migration | [migrations/org_migrations/versions/9a2b3c4d5e6f_015_add_crm_object_associations.py](../../migrations/org_migrations/versions/9a2b3c4d5e6f_015_add_crm_object_associations.py) |
| Model | [src/domain/models/organisation_tables/sync/crm/associations.py](../../src/domain/models/organisation_tables/sync/crm/associations.py) |

**Schema:**

```sql
crm_object_associations
├── association_id (PK, UUID)
├── organisation_id (FK)
├── integration_id (FK, nullable - for internal associations)
├── external_id (provider-supplied, nullable)
├── from_object_type (enum: deal, contact, company, activity, etc.)
├── from_external_id (string)
├── from_local_id (UUID, nullable - FK when resolved)
├── to_object_type (enum)
├── to_external_id (string)
├── to_local_id (UUID, nullable - FK when resolved)
├── association_category (enum: HUBSPOT_DEFINED, SALESFORCE_STANDARD, etc.)
├── association_type_id (provider's type identifier)
├── association_label (human-readable)
├── role_label (e.g., "Decision Maker", "Primary Contact")
├── is_primary (boolean)
├── properties (JSONB)
├── source_created_at, source_modified_at, synced_at, last_seen_at
└── is_deleted (boolean)
```

**Enums Created:**

- `CRMObjectType`: deal, contact, company, user, activity, task, meeting, call, email, note, product, line_item, ticket, lead, quote, custom
- `CRMAssociationCategory`: hubspot_defined, salesforce_standard, pipedrive_standard, zoho_standard, user_defined, internal, other

**CRM Provider Mapping:**

| CRM | Association APIs | What This Captures |
|-----|------------------|-------------------|
| HubSpot | `/crm/v4/associations` - labeled associations | `association_type_id`, `role_label` |
| Salesforce | `OpportunityContactRole`, `AccountContactRelation` | `role_label`, `is_primary` |
| Pipedrive | `/deals/{id}/participants`, `/deals/{id}/products` | `to_object_type`, polymorphic linking |
| Zoho | Related Lists, Contact Roles | `role_label`, many-to-many support |

---

### P1: Contact↔Company Relationship Normalization

**Problem:** `crm_contacts.company_name` was just a string. No FK or join to `crm_companies`, blocking account-based views.

**Solution:** Populate contact→company associations in `crm_object_associations` during contact sync.

| Artifact | Path |
|----------|------|
| Service | [src/service/integrations/crm/entities/contact_sync_service.py](../../src/service/integrations/crm/entities/contact_sync_service.py) |

**Key Changes:**

- Added `_extract_contact_company_external_ids()` - extracts company IDs from provider payloads
- Added `_upsert_contact_company_associations()` - creates association records
- Supports HubSpot (`associatedcompanyid`), Salesforce (`accountid`), Pipedrive (`org_id`)
- Primary company is marked with `is_primary=True`

**Provider Field Mapping:**

| CRM | Field Name | Notes |
|-----|------------|-------|
| HubSpot | `associatedcompanyid` | String numeric |
| Salesforce | `accountid`, `account_id`, `accountId` | Multiple casing variants |
| Pipedrive | `org_id`, `orgid` | Can be object `{value, name}` |

---

### P1: Activity Generalization (Tasks → Activities)

**Problem:** `crm_deal_tasks` was deal-centric (required `deal_id`). Salesforce/Pipedrive activities can exist without deals.

**Solution:** Renamed table to `crm_activities` with polymorphic relationship support.

| Artifact | Path |
|----------|------|
| Migration | [migrations/org_migrations/versions/a1b2c3d4e5f6_016_rename_deal_tasks_to_activities.py](../../migrations/org_migrations/versions/a1b2c3d4e5f6_016_rename_deal_tasks_to_activities.py) |
| Model | [src/domain/models/organisation_tables/sync/crm/activities.py](../../src/domain/models/organisation_tables/sync/crm/activities.py) |

**Key Changes:**

- Renamed table `crm_deal_tasks` → `crm_activities`
- Renamed column `task_id` → `activity_id`
- Renamed column `task_type` → `activity_type`

**New Columns Added:**

```python
# Polymorphic relationship
related_object_type: str | None  # DEAL, CONTACT, COMPANY, USER, LEAD
related_external_id: str | None
related_local_id: UUID | None
related_name: str | None

# Direct FK columns for common associations
contact_id: UUID | None = Field(foreign_key="crm_contacts.contact_id")
contact_external_id: str | None
company_id: UUID | None = Field(foreign_key="crm_companies.company_id")
company_external_id: str | None

# Meeting/event support
end_date: datetime | None
duration_minutes: int | None
attendees: list[dict] = Field(sa_column=Column(JSONB, server_default="'[]'"))
is_all_day: bool = Field(default=False)

# Communication fields
direction: str | None  # INBOUND, OUTBOUND
call_outcome: str | None
activity_subtype: str | None  # provider-specific subtype
```

**Activity Types Supported:**

| Type | Description |
|------|-------------|
| TASK / TODO | Generic to-do items |
| EMAIL | Email activity (logged or tracked) |
| CALL | Phone call (logged or scheduled) |
| MEETING | Meeting/event with attendees |
| NOTE | Free-form note attached to records |
| EVENT | Calendar event (Salesforce/Zoho) |
| SMS, LINKEDIN_MESSAGE, CHAT, OTHER | Extended types |

**Backwards Compatibility:**

- `CRMDealTask` is aliased to `CRMActivity`
- `CRMTaskStatus`, `CRMTaskType`, `CRMTaskPriority` aliased to activity equivalents
- `task_id` property aliased to `activity_id`
- `task_type` property aliased to `activity_type`

---

### P2: Deletion Semantics Standardization

**Problem:** Inconsistent deletion flags: `is_deleted` (mixin), `is_deleted_remote` (some tables), `is_active` (different meaning).

**Solution:** Standardize on `SyncedRecordMixin.is_deleted` as the canonical provider-deletion flag.

| Artifact | Path |
|----------|------|
| Migration | [migrations/org_migrations/versions/c7d8e9f0a1b2_017_standardize_crm_deletion_flags.py](../../migrations/org_migrations/versions/c7d8e9f0a1b2_017_standardize_crm_deletion_flags.py) |
| Model (Users) | [src/domain/models/organisation_tables/sync/crm/users.py](../../src/domain/models/organisation_tables/sync/crm/users.py) |
| Model (Activities) | [src/domain/models/organisation_tables/sync/crm/activities.py](../../src/domain/models/organisation_tables/sync/crm/activities.py) |

**Key Changes:**

- Backfilled `is_deleted = TRUE` from `is_deleted_remote = TRUE` records
- Dropped `is_deleted_remote` column from `crm_users` and `crm_activities`
- Added backwards-compatible `is_deleted_remote` property alias on both models
- Kept `is_active` where it has distinct business meaning (inactive customer ≠ deleted)

**Service Updates:**

- `user_sync_service.py`: Uses `is_deleted` instead of `is_deleted_remote`
- `deal_task_sync_service.py`: Removed `is_deleted_remote` assignment

---

### P2: Canonical Enum Constraints

**Problem:** Activity status/type/priority stored as `str` without validation, breaking cross-CRM analytics.

**Solution:** Added enum mapping module + DB CHECK constraints.

| Artifact | Path |
|----------|------|
| Migration | [migrations/org_migrations/versions/d8e9f0a1b2c3_018_add_activity_enum_constraints.py](../../migrations/org_migrations/versions/d8e9f0a1b2c3_018_add_activity_enum_constraints.py) |
| Enum Mappings | [src/service/integrations/crm/entities/activity_enum_mappings.py](../../src/service/integrations/crm/entities/activity_enum_mappings.py) |
| Tests | [tests/service/integrations/crm/test_activity_enum_mappings.py](../../tests/service/integrations/crm/test_activity_enum_mappings.py) |

**Canonical Enums:**

```python
# CRMActivityStatus
NOT_STARTED, IN_PROGRESS, COMPLETED, WAITING, DEFERRED, CANCELLED

# CRMActivityType
TASK, TODO, EMAIL, CALL, MEETING, NOTE, EVENT, SMS, LINKEDIN_MESSAGE, CHAT, OTHER

# CRMActivityPriority
NONE, LOW, MEDIUM, HIGH, URGENT
```

**Normalization Functions:**

- `normalize_activity_status(raw_status)` → canonical or None
- `normalize_activity_type(raw_type)` → canonical or OTHER
- `normalize_activity_priority(raw_priority)` → canonical or NONE

**Provider Mapping Examples:**

| Provider | Raw Value | Canonical |
|----------|-----------|-----------|
| Salesforce | "Waiting on someone else" | WAITING |
| Salesforce | "Deferred" | DEFERRED |
| Pipedrive | "0" (done=false) | NOT_STARTED |
| Pipedrive | "1" (done=true) | COMPLETED |
| Pipedrive | "deadline" | TASK |
| HubSpot | "IN_PROGRESS" | IN_PROGRESS |

**Unknown Value Handling:**

- Unknown statuses: preserved in `properties._raw_activity_status`, column set to NULL
- Unknown types: preserved in `activity_subtype` and `properties._raw_activity_type`, column set to OTHER
- Unknown priorities: preserved in `properties._raw_activity_priority`, column set to NONE

---

### P2: Owner FK Resolution

**Problem:** Owner info stored as denormalized strings (`owner_external_id`, `owner_name`, `owner_email`) without FK to `crm_users`.

**Solution:** Added `owner_id` FK column to all four CRM entity tables.

| Artifact | Path |
|----------|------|
| Migration | [migrations/org_migrations/versions/e9f0a1b2c3d4_019_add_owner_id_fk_columns.py](../../migrations/org_migrations/versions/e9f0a1b2c3d4_019_add_owner_id_fk_columns.py) |
| Model (Deals) | [src/domain/models/organisation_tables/sync/crm/deals.py](../../src/domain/models/organisation_tables/sync/crm/deals.py) |
| Model (Contacts) | [src/domain/models/organisation_tables/sync/crm/contacts.py](../../src/domain/models/organisation_tables/sync/crm/contacts.py) |
| Model (Companies) | [src/domain/models/organisation_tables/sync/crm/companies.py](../../src/domain/models/organisation_tables/sync/crm/companies.py) |
| Model (Activities) | [src/domain/models/organisation_tables/sync/crm/activities.py](../../src/domain/models/organisation_tables/sync/crm/activities.py) |

**Key Changes:**

- Added `owner_id: UUID | None` FK to `crm_users.user_id` on all four tables
- Created indexes: `ix_crm_deal_owner_id`, `ix_crm_contact_owner_id`, `ix_crm_company_owner_id`, `ix_crm_activity_owner_id`
- Backfilled `owner_id` by joining `owner_external_id` + `integration_id` → `crm_users.external_id`
- Added FK constraints with `ON DELETE SET NULL`

**Design Decisions:**

- Columns are nullable (not all records have owners; backfill may not resolve 100%)
- Denormalized `owner_external_id`/`owner_name`/`owner_email` retained for display/fallback
- `ON DELETE SET NULL` prevents cascading issues if CRM user is deleted

---

## Service Layer Updates

### TasksAccessService

| Artifact | Path |
|----------|------|
| Service | [src/service/crm/tasks_access_service.py](../../src/service/crm/tasks_access_service.py) |

**Changes:**

- Updated imports from `CRMDealTask` to `CRMActivity`
- Updated column references (`task_id` → `activity_id`, `task_type` → `activity_type`)
- Removed `is_deleted_remote` filter (now uses `is_deleted` only)

### DealTaskSyncService

| Artifact | Path |
|----------|------|
| Service | [src/service/integrations/crm/entities/deal_task_sync_service.py](../../src/service/integrations/crm/entities/deal_task_sync_service.py) |

**Changes:**

- Added enum normalization using `normalize_activity_*` functions
- Removed `is_deleted_remote` assignment
- Updated metrics calculation to use canonical enum values
- Preserves raw provider values in `properties` when normalization falls back

### UserSyncService

| Artifact | Path |
|----------|------|
| Service | [src/service/integrations/crm/entities/user_sync_service.py](../../src/service/integrations/crm/entities/user_sync_service.py) |

**Changes:**

- Updated `_update_status_fields()` to use `is_deleted` instead of `is_deleted_remote`
- Maps provider `deleted`/`isDeleted` flags to canonical `is_deleted`

---

## Module Exports Update

| Artifact | Path |
|----------|------|
| **init**.py | [src/domain/models/organisation_tables/sync/crm/**init**.py](../../src/domain/models/organisation_tables/sync/crm/__init__.py) |

**Changes:**

- Added exports for `CRMActivity`, `CRMActivityStatus`, `CRMActivityType`, `CRMActivityPriority`, `CRMActivityObjectType`
- Added backwards compatibility aliases: `CRMDealTask`, `CRMTaskStatus`, `CRMTaskType`, `CRMTaskPriority`
- Added exports for `CRMObjectAssociation`, `CRMObjectType`, `CRMAssociationCategory`
- Removed import from deleted `deal_tasks.py`

---

## Documentation Created

### Technical Documentation

| Document | Path | Description |
|----------|------|-------------|
| Schema Review | [docs/Technical Documentation/database/crm-integration-schema.md](../Technical%20Documentation/database/crm-integration-schema.md) | Full schema review with CRM comparison matrix |
| Update Proposals | [docs/Technical Documentation/database/crm-integration-schema-update-proposals.md](../Technical%20Documentation/database/crm-integration-schema-update-proposals.md) | P0-P3 proposals with implementation status |

### Future Functionality Documentation

| Document | Path | Description |
|----------|------|-------------|
| README | [docs/Future Functionality Documentation/README.md](../Future%20Functionality%20Documentation/README.md) | Index for future features |
| Core CRM Layer | [docs/Future Functionality Documentation/core-crm-layer-analysis.md](../Future%20Functionality%20Documentation/core-crm-layer-analysis.md) | Core layer rationale, triggers, architecture |
| Field Metadata | [docs/Future Functionality Documentation/crm-field-metadata-and-writeback.md](../Future%20Functionality%20Documentation/crm-field-metadata-and-writeback.md) | Field definitions schema, write-back support |

---

## Migration Summary

| Order | Revision | Description |
|-------|----------|-------------|
| 1 | `8f1c2d3e4a5b` | Make deal_stakeholders.integration_id nullable |
| 2 | `9a2b3c4d5e6f` | Add crm_object_associations table |
| 3 | `a1b2c3d4e5f6` | Rename crm_deal_tasks → crm_activities |
| 4 | `c7d8e9f0a1b2` | Standardize deletion flags |
| 5 | `d8e9f0a1b2c3` | Add activity enum constraints |
| 6 | `e9f0a1b2c3d4` | Add owner_id FK columns |

---

## How Multi-CRM Goals Were Achieved

### 1. Provider-Agnostic Schema ✅

The schema now handles CRM differences without code changes:

| CRM Difference | Schema Solution |
|----------------|-----------------|
| Contact→Company (HubSpot many-to-many, Salesforce primary+multi, Pipedrive single) | `crm_object_associations` with `is_primary` flag |
| Activity types (Salesforce Task/Event, HubSpot engagements, Pipedrive unified) | `crm_activities` with polymorphic `related_object_type` |
| Contact roles (Salesforce OpportunityContactRole, HubSpot labels) | `crm_object_associations` with `role_label` |
| Owner identifiers | `owner_id` FK to `crm_users` with denormalized fallback |

### 2. Polymorphic Associations ✅

Activities and associations can link to any object type:

```python
# Activity linked to contact (Salesforce WhoId pattern)
activity.related_object_type = "CONTACT"
activity.related_external_id = "003xxx"
activity.contact_id = UUID(...)  # FK for referential integrity

# Contact→Company association (many-to-many)
association.from_object_type = "contact"
association.to_object_type = "company"
association.role_label = "primary"
```

### 3. Consistent Analytics ✅

Canonical enums enable cross-CRM reporting:

```sql
-- Works regardless of CRM provider
SELECT status, COUNT(*) 
FROM crm_activities 
WHERE status IN ('COMPLETED', 'IN_PROGRESS')
GROUP BY status;
```

### 4. Internal Object Support ✅

`FlexibleSyncedRecordMixin` allows records without CRM integration:

```python
# Manually discovered stakeholder
stakeholder = DealStakeholder(
    integration_id=None,  # No CRM
    source=StakeholderSource.EMAIL_DISCOVERY,
    ...
)
```

### 5. Future Extensibility ✅

Architecture is ready for "our own CRM" evolution:

- `crm_*` tables remain the sync/cache layer
- `crm_object_associations` provides the relationship foundation
- Core CRM layer can be added when multi-CRM customers exist
- Field metadata can be added when write-back is needed

---

## Testing

| Test File | Path |
|-----------|------|
| Enum Mappings | [tests/service/integrations/crm/test_activity_enum_mappings.py](../../tests/service/integrations/crm/test_activity_enum_mappings.py) |

---

## Breaking Changes

### None for Existing Code

All changes maintain backwards compatibility:

1. **Model Aliases:** `CRMDealTask` → `CRMActivity`, `CRMTaskStatus` → `CRMActivityStatus`, etc.
2. **Property Aliases:** `task_id` → `activity_id`, `task_type` → `activity_type`, `is_deleted_remote` → `is_deleted`
3. **Existing Queries:** Work unchanged; new columns are optional

### Migration Notes

1. Run migrations in order (014 → 019)
2. Backfill operations are idempotent and safe to re-run
3. Enum constraints allow NULL for unknown values (no data loss)
4. Owner FK backfill is best-effort (unresolved owners remain NULL)

---

## Future Work Triggers

### Implement Core CRM Layer When

- Customer with multiple CRMs (HubSpot + Salesforce) signs up
- "Our own CRM" mode requested (create deals without CRM)
- CRM migration feature needed (HubSpot → Salesforce)

### Implement Field Metadata When

- Custom field mapping UI required
- Write-back to CRM requested
- Picklist validation needed

### Implement Products/Line Items When

- Deal value accuracy from line items required
- Product-level analytics needed
- CPQ integration requested

---

## Conclusion

The CRM schema is now **multi-CRM ready** with:

- ✅ Generic association layer for arbitrary relationships
- ✅ Polymorphic activity model supporting all CRM patterns
- ✅ Canonical enums for cross-CRM analytics
- ✅ Owner FK resolution for consistent filtering
- ✅ Flexible sync patterns for internal object creation
- ✅ Documented path to "our own CRM" future

The architecture supports HubSpot, Salesforce, Pipedrive, and Zoho CRMs today, and can evolve to support additional providers or internal CRM mode when business needs justify the investment.
