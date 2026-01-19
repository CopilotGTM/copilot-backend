# CRM Integration Schema - Proposed Updates (Actionable)

**Last Updated:** December 16, 2025  
**Document Version:** 2.0

This is the follow-up to `docs/Technical Documentation/database/crm-integration-schema.md`. It lists concrete changes we should consider to (1) integrate multiple CRMs smoothly and (2) evolve toward "our own CRM" where internal objects become the source of truth.

Each proposal includes:

- What to change
- Why we need it (reason)
- Whether code + DB changes are required
- Where in the repo it likely touches

---

## Executive Summary: Gap Analysis

Based on deep analysis of the current schema and comparison with Salesforce, Pipedrive, and Zoho CRMs, here are the **critical gaps** ranked by priority:

| Gap | Impact | Priority | Effort |
|-----|--------|----------|--------|
| **No contact ↔ company relationship** | Blocks account-based features, org charts | P1 | Medium |
| **Activities are deal-centric only** | Misses activities without deals (common in SFDC/Pipedrive) | P1 | Medium |
| **No generic association layer** | Every new CRM relationship requires schema changes | P1 | Medium |
| **No products/line items on deals** | Required for accurate deal value calculations | P1 | Medium |
| **Deals are integration-scoped only** | Blocks "our own CRM" internal deal creation | P2 | High |
| **No canonical internal CRM layer** | Multi-CRM orgs get duplicate records | P3 | Very High |
| **No field metadata storage** | Custom field mapping UI not possible | P3 | Medium |

---

## Proposals (ordered by recommended sequence)

### P0. Fix "internal vs integration-owned" data ownership (Stakeholders)

**Status:** ✅ Implemented

**What we changed**

- DB: made `deal_stakeholders.integration_id` nullable in `migrations/org_migrations/versions/8f1c2d3e4a5b_014_make_deal_stakeholders_integration_id_nullable.py`
- Model: switched `DealStakeholder` to inherit `FlexibleSyncedRecordMixin` in `src/domain/models/organisation_tables/sync/crm/stakeholders.py` so `integration_id` is optional

**Reason**

- Multi-CRM orgs (or orgs with multiple data sources) should not be forced to pick a "fake owner integration" for internal/product data.
- This is also required if you want stakeholders to exist before any CRM is connected (manual mode / freemium / partial rollout).

---

### P0. Make `IntegrationParticipant` truly cross-integration (identity layer)

**Status:** 🔴 Not Implemented

**Current state**

- `integration_participants` exists, but is still integration-scoped (inherits `SyncedRecordMixin`, so `integration_id` is required).
- There is no `participant_crm_links` mapping table in the current codebase.
- Result: in a multi-CRM future, the same real-world person can end up duplicated (one row per integration) unless you rely solely on email-based uniqueness.

**What to change**

- Make `IntegrationParticipant` able to exist without being owned by a single integration (e.g., switch to `FlexibleSyncedRecordMixin` / make `integration_id` nullable).
- Keep identity uniqueness per org via `canonical_email_normalized` (already present).
- Add a mapping table (proposed: `participant_crm_links`) so one participant can link to multiple CRM records across integrations (contact/user/etc.).
- Backfill + update resolution logic to prefer the mapping table when present.

**Reason**

- Multi-CRM requires "one person" even if the same email appears in HubSpot + Salesforce + email + calendar.
- A canonical identity table becomes the central join point across CRM, email, calendar, meetings, and messaging.

---

### P1. Add a generic CRM association model (beyond deal participants)

**Status:** ✅ Implemented

**What was added**

Created `crm_object_associations` table to represent associations between arbitrary CRM objects:

- **Model:** `src/domain/models/organisation_tables/sync/crm/associations.py`
- **Migration:** `migrations/org_migrations/versions/9a2b3c4d5e6f_015_add_crm_object_associations.py`

```
crm_object_associations
├── association_id (PK, UUID)
├── organisation_id (FK)
├── integration_id (FK, nullable for internal associations)
├── external_id (provider-supplied, nullable)
├── from_object_type (enum: deal, contact, company, activity, product, etc.)
├── from_external_id (string)
├── from_local_id (UUID, nullable - FK when resolved)
├── to_object_type (enum)
├── to_external_id (string)
├── to_local_id (UUID, nullable - FK when resolved)
├── association_category (enum: HUBSPOT_DEFINED, SALESFORCE_STANDARD, USER_DEFINED, INTERNAL, etc.)
├── association_type_id (provider's association type identifier)
├── association_label (human-readable label from provider)
├── role_label (string, nullable - e.g., "Decision Maker", "Primary Contact")
├── is_primary (boolean, default false)
├── properties (JSONB)
├── source_created_at, source_modified_at, synced_at, last_seen_at
└── is_deleted (boolean)
```

**Key design decisions**

1. Uses `FlexibleSyncedRecordMixin` so `integration_id` is nullable - supports internal associations
2. Comprehensive indexes for query patterns: from-object lookup, to-object lookup, role-based queries
3. `CRMObjectType` enum covers: deal, contact, company, user, activity, task, meeting, call, email, note, product, line_item, ticket, lead, quote, custom
4. `CRMAssociationCategory` enum supports: hubspot_defined, salesforce_standard, pipedrive_standard, zoho_standard, user_defined, internal, other

**Why this matters for each CRM**

| CRM | Key Association APIs | What this captures |
|-----|---------------------|-------------------|
| **HubSpot** | `/crm/v4/associations` - labeled associations | `association_type_id`, `role_label` |
| **Salesforce** | `OpportunityContactRole`, `AccountContactRelation` | `role_label`, `is_primary` |
| **Pipedrive** | `/deals/{id}/participants`, `/deals/{id}/products` | `to_object_type`, polymorphic linking |
| **Zoho** | Related Lists, Contact Roles | `role_label`, many-to-many support |

**Next steps**

1. Keep `crm_deal_participants` for now (no breaking change)
2. Start populating `crm_object_associations` in CRM sync services
3. Once stable, migrate reads to the generic association table where appropriate

---

### P1. Normalize contact ↔ company relationships

**Status:** ✅ Implemented (primary association via `crm_object_associations`)

**Problem**

Currently, `crm_contacts.company_name` is just a string. There's no FK or join to `crm_companies`, blocking:

- Account-based views (show all contacts at a company)
- Org charts and relationship mapping
- De-duplication across CRMs

**What to change**

Option A (Recommended): Use `crm_object_associations` table from P1 above

- Populate with `from_object_type='contact'`, `to_object_type='company'` associations
- Supports many-to-many (required by HubSpot, Salesforce)

What we implemented now:

- We populate **contact → company** associations into `crm_object_associations` during contact sync.
- Implementation is **best-effort** and currently focuses on the **primary** company/account ID present on the contact payload (HubSpot: `associatedcompanyid`, Salesforce: `accountid`/`accountId`, Pipedrive: `org_id`).
- Code: `src/service/integrations/crm/entities/contact_sync_service.py`

Notes / limitations (intentional for now):

- This does not yet fetch full many-to-many graphs (e.g., HubSpot association API, Salesforce `AccountContactRelation`).
- Stale associations are not yet actively soft-deleted; we upsert what we see.

Option B: Add direct FK columns to `crm_contacts`:

```python
primary_company_id: UUID | None = Field(default=None, foreign_key="crm_companies.company_id")
primary_company_external_id: str | None = Field(default=None)
```

**CRM mapping**

| CRM | Contact → Company relationship |
|-----|-------------------------------|
| **HubSpot** | Association (many-to-many via API) |
| **Salesforce** | `Contact.AccountId` (primary) + `AccountContactRelation` (multi) |
| **Pipedrive** | `Person.org_id` (single organization) |
| **Zoho** | `Contact.Account_Name` + lookup |

**Reason**

- Many CRMs treat this as a first-class relationship (Accounts ↔ Contacts).
- Without it, account-based workflows and de-duplication become impossible.

**Requires code changes?** Yes  
**Requires DB migration?** No additional (depends on P1 `crm_object_associations`)

**Likely code areas**

- `src/domain/models/organisation_tables/sync/crm/contacts.py`
- `src/domain/models/organisation_tables/sync/crm/companies.py`
- Contact sync services that can fetch account/contact associations

---

### P1. Generalize CRM activities beyond "deal tasks"

**Status:** ✅ Implemented

**Problem**

`crm_deal_tasks` was deal-centric (required `deal_id`). But:

- Salesforce `Task` and `Event` use polymorphic `WhatId` (deal/account/case) + `WhoId` (contact/lead)
- Pipedrive activities can link to person, organization, deal, or lead
- HubSpot engagements can exist without deal associations
- Pre-sales needs activity history across the customer, not just inside a deal

**What we implemented**

Renamed table `crm_deal_tasks` → `crm_activities` with full polymorphic support:

- Model: `src/domain/models/organisation_tables/sync/crm/activities.py`
- Migration: `migrations/org_migrations/versions/a1b2c3d4e5f6_016_rename_deal_tasks_to_activities.py`
- Backwards compatibility: `CRMDealTask` aliased to `CRMActivity`

**New columns added:**

```python
# Polymorphic relationship
related_object_type: str | None  # DEAL, CONTACT, COMPANY, USER, LEAD
related_external_id: str | None
related_local_id: UUID | None  # polymorphic (not FK-enforced)
related_name: str | None  # denormalized display name

# Direct FK columns for common associations
contact_id: UUID | None = Field(foreign_key="crm_contacts.contact_id")
contact_external_id: str | None
company_id: UUID | None = Field(foreign_key="crm_companies.company_id")
company_external_id: str | None
# deal_id already existed

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

**Activity types supported:**

- `TASK` / `TODO`: Generic to-do items
- `EMAIL`: Email activity (logged or tracked)
- `CALL`: Phone call (logged or scheduled)
- `MEETING`: Meeting/event with attendees
- `NOTE`: Free-form note attached to records
- `EVENT`: Calendar event (Salesforce/Zoho)
- `SMS`, `LINKEDIN_MESSAGE`, `CHAT`, `OTHER`: Extended types

**CRM activity mapping**

| CRM | Activity Model | Associations |
|-----|---------------|--------------|
| **HubSpot** | Engagements (calls, emails, meetings, notes, tasks) | Via `/crm/v4/associations` |
| **Salesforce** | Task, Event separate objects | `WhatId` (polymorphic), `WhoId` |
| **Pipedrive** | Unified Activity model | `deal_id`, `person_id`, `org_id` |
| **Zoho** | Task, Event, Call separate | `Related To` field (polymorphic) |

**Requires code changes?** Done  
**Requires DB migration?** Done

---

### P1. Add products/line items support on deals

**Status:** 🔴 Not Implemented

**Problem**

All major CRMs support products/line items on deals:

- Salesforce: `OpportunityLineItem` (links Opportunity ↔ Product2 via PricebookEntry)
- HubSpot: Line Items (associated to deals)
- Pipedrive: Products on deals (with quantity, price, discount)
- Zoho: Products on Potentials

Currently, `CRMParticipantType.LINE_ITEM` exists as an enum but there's no dedicated line item model.

**Proposed approach**

Key features we recommend for a future `crm_deal_line_items` table:

- **Product reference**: `product_external_id`, `product_name`, `product_sku`, `product_description`
- **Pricing**: `quantity`, `unit_price`, `amount`, `currency`
- **Discounts**: `discount`, `discount_type` (PERCENTAGE/AMOUNT), `discount_percentage`
- **Tax**: `tax_rate`, `tax_amount`
- **Recurring billing**: `billing_frequency` (ONE_TIME/MONTHLY/QUARTERLY/ANNUALLY), `billing_start_date`, `term_months`, `recurring_revenue`
- **Display**: `display_order`, `line_number`
- Standard sync fields: `synced_at`, `last_seen_at`, `is_deleted`, `properties`, `data_payload`

**Why this matters**

- Accurate deal value calculation (sum of line items vs. manual amount)
- Product-level analytics (which products sell, attach rates)
- Pre-sales can see what's being quoted
- Future CPQ (Configure-Price-Quote) integration

**Requires code changes?** Yes  
**Requires DB migration?** Yes

---

### P1.5 Support multi-CRM mappings for canonical identities

**Status:** ✅ Implemented

(Details as previously documented)

---

### P2. Standardize deletion + archival semantics across CRM tables

**Status:** ✅ Implemented

**Current state**

- `SyncedRecordMixin.is_deleted` - canonical soft-delete
- `CRMUser.is_deleted_remote` - removed as a stored column (kept as a backwards-compatible alias)
- `CRMCompany.is_active` - different meaning (active vs inactive account)
- `CRMActivity.is_deleted_remote` - removed as a stored column (kept as a backwards-compatible alias)

**Recommendation**

1. Use `is_deleted` (from mixin) as the single canonical flag for "record was deleted in provider"
2. Keep `is_active` only where it has distinct business meaning (e.g., "inactive customer" is different from "deleted record")
3. Deprecate `is_deleted_remote` columns over time (or alias them to `is_deleted`)

**What we changed**

- Removed `is_deleted_remote` DB columns from `crm_users` and `crm_activities`
- Updated service/query code to use `SyncedRecordMixin.is_deleted` as the canonical provider-deletion flag
- Kept `is_deleted_remote` as a Python-level alias property for backwards compatibility

**Requires code changes?** Yes  
**Requires DB migration?** Yes

---

### P2. Canonical enums + constraints for cross-CRM analytics

**Status:** ✅ Implemented

**Current state**

- `CRMDealStatus` enum exists and is enforced
- `CRMStageState` enum exists and is enforced
- `CRMActivityStatus`, `CRMActivityType`, `CRMActivityPriority` enums exist and are now enforced via DB CHECK constraints

**What we implemented**

1. Added canonical enum mapping module: `src/service/integrations/crm/entities/activity_enum_mappings.py`
   - Maps provider-specific values (HubSpot, Salesforce, Pipedrive, Zoho) to canonical enums
   - Provides `normalize_activity_status()`, `normalize_activity_type()`, `normalize_activity_priority()` helpers
2. Updated sync layer (`deal_task_sync_service.py`) to normalize provider values during sync
3. Added DB CHECK constraints via migration `d8e9f0a1b2c3_018_add_activity_enum_constraints.py`
   - Normalizes existing non-canonical values
   - Enforces canonical values for new inserts/updates

**Provider mapping example** (see `activity_enum_mappings.py` for full mappings):

```python
# Status mapping (excerpt)
ACTIVITY_STATUS_MAP = {
    # HubSpot
    "NOT_STARTED": CRMActivityStatus.NOT_STARTED,
    "IN_PROGRESS": CRMActivityStatus.IN_PROGRESS,
    "COMPLETED": CRMActivityStatus.COMPLETED,
    # Salesforce
    "Not Started": CRMActivityStatus.NOT_STARTED,
    "In Progress": CRMActivityStatus.IN_PROGRESS,
    "Completed": CRMActivityStatus.COMPLETED,
    "Waiting on someone else": CRMActivityStatus.WAITING,
    "Deferred": CRMActivityStatus.DEFERRED,
    # Pipedrive
    "0": CRMActivityStatus.NOT_STARTED,  # done=false
    "1": CRMActivityStatus.COMPLETED,     # done=true
}
```

**Requires code changes?** Done  
**Requires DB migration?** Done

---

### P2. Add owner FK resolution

**Status:** ✅ Implemented

**Problem**

Deals, contacts, and tasks store owner info as denormalized strings:

- `owner_external_id`
- `owner_name`
- `owner_email`

But there's no FK to `crm_users.user_id`, preventing:

- Consistent owner filtering
- User-level analytics
- Ownership transfers

**What we implemented**

Added `owner_id` FK column to all four CRM entity models:

- `CRMDeal.owner_id` → FK to `crm_users.user_id`
- `CRMContact.owner_id` → FK to `crm_users.user_id`
- `CRMCompany.owner_id` → FK to `crm_users.user_id`
- `CRMActivity.owner_id` → FK to `crm_users.user_id`

**Models updated:**

- `src/domain/models/organisation_tables/sync/crm/deals.py`
- `src/domain/models/organisation_tables/sync/crm/contacts.py`
- `src/domain/models/organisation_tables/sync/crm/companies.py`
- `src/domain/models/organisation_tables/sync/crm/activities.py`

**Migration:** `migrations/org_migrations/versions/e9f0a1b2c3d4_019_add_owner_id_fk_columns.py`

Migration operations:

1. Adds nullable `owner_id` (UUID) columns to all four tables
2. Creates indexes for efficient owner-based queries
3. Backfills `owner_id` by joining `owner_external_id` + `integration_id` → `crm_users.external_id`
4. Adds FK constraints (`ON DELETE SET NULL`)

**Design decisions:**

- Columns are nullable (not all records have owners, and backfill may not resolve 100%)
- Denormalized `owner_external_id`/`owner_name`/`owner_email` retained for display/fallback
- `ON DELETE SET NULL` prevents cascading issues if CRM user is deleted
- Indexes enable efficient owner-based filtering and analytics

**Requires code changes?** Done  
**Requires DB migration?** Done

---

### P3. Introduce an internal "core CRM" layer + external mappings

**Status:** 🟡 Deferred (Future)

**Vision**

Create a two-layer architecture:

1. **Sync Layer (`crm_*` tables)**: Provider-scoped import/cache
2. **Core Layer (`core_*` tables)**: Internal source of truth

```
┌─────────────────────────────────────────────────────────────┐
│                     YOUR APPLICATION                        │
│  (reads/writes core_* tables, owns business logic)          │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                    CORE LAYER                               │
│  core_accounts, core_people, core_opportunities,            │
│  core_activities (integration_id = NULL for internal)       │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│               CRM OBJECT MAPPINGS                           │
│  (organisation_id, integration_id, external_id) →           │
│  (internal_object_type, internal_id)                        │
└─────────┬─────────────────────┬─────────────────┬───────────┘
          │                     │                 │
┌─────────▼─────────┐ ┌─────────▼─────────┐ ┌─────▼───────────┐
│   HUBSPOT SYNC    │ │  SALESFORCE SYNC  │ │  PIPEDRIVE SYNC │
│   crm_* tables    │ │   crm_* tables    │ │  crm_* tables   │
│  (integration A)  │  (integration B)  │ │ (integration C) │
└───────────────────┘ └───────────────────┘ └─────────────────┘
```

**Why this is deferred**

This is a strategic capability for:

- Multi-CRM organisations (HubSpot + Salesforce, etc.)
- Standalone / "our own CRM" mode (create deals without any CRM)
- CRM migrations (HubSpot → Salesforce) without losing history
- Write-back to CRM(s) with conflict resolution

Those are not required for the current single-CRM-per-org sync approach, so we defer until a concrete trigger exists.

**Decision & use cases document**

See the future-facing rationale and trigger conditions in:

- `docs/Future Functionality Documentation/core-crm-layer-analysis.md`
  - Forecast category support
  - Win/loss analysis fields (loss_reason, win_reason, competitor)
  - Stage history tracking (JSONB array)
  - FKs to account and primary_contact

1. **CoreActivity (core_activities):**
   - Canonical activity types with CHECK constraint
   - Polymorphic relationships (account, person, opportunity)
   - Meeting-specific fields (attendees, location, meeting_link)
   - Call-specific fields (direction, call_outcome, call_disposition)
   - Recurrence support (is_recurring, recurrence_rule)

2. **CRMObjectMapping (crm_object_mappings):**
   - Maps external CRM records to internal core records
   - `sync_direction` (INBOUND, OUTBOUND, BIDIRECTIONAL)
   - `is_primary_source` flag for conflict resolution
   - `is_write_back_enabled` for selective write-back
   - Conflict detection and resolution tracking
   - Match confidence for auto-matched records
   - Verification tracking (verified_by_user_id, verified_at)

**Implementation roadmap (when triggered)**

1. Design core schema
2. Create models and migrations
3. Build sync-to-core pipeline
4. Migrate product reads from `crm_*` to `core_*`
5. Add write-back support

**Requires code changes?** Yes (significant)  
**Requires DB migration?** Yes (new tables + backfill)

---

### P3. Store provider field metadata (for custom fields + write-back)

**Status:** 🔴 Not Implemented - Strategic (Future)

**What to add**

```python
class CRMFieldDefinition(SyncedRecordMixin, table=True):
    """Stores field metadata from CRM providers for custom field support."""
    __tablename__ = "crm_field_definitions"
    
    field_definition_id: UUID = Field(primary_key=True)
    
    object_type: str  # deal, contact, company
    field_key: str  # API name (e.g., "custom_field_abc123")
    field_label: str  # Display name
    field_type: str  # text, number, date, picklist, boolean, etc.
    
    is_required: bool = False
    is_readonly: bool = False
    is_custom: bool = True
    
    # For picklists
    picklist_values: list[dict] | None = Field(sa_column=Column(JSONB))
    # [{"value": "hot", "label": "Hot", "display_order": 1}, ...]
    
    # For number fields
    number_precision: int | None
    number_scale: int | None
    
    properties: dict = Field(default_factory=dict)
    synced_at: datetime
```

**Use cases**

1. Custom field mapping UI - show users available fields to map
2. Write-back validation - know field types before creating records
3. Picklist rendering - show dropdown options in UI
4. Field-level sync configuration

**Requires code changes?** Yes  
**Requires DB migration?** Yes

---

## Implementation Priority Matrix

| Priority | Proposal | Effort | Impact | Dependencies |
|----------|----------|--------|--------|--------------|
| **P1 (Next)** | Generic associations | Medium | High | None |
| **P1 (Next)** | Contact ↔ Company | Medium | High | Associations (optional) |
| **P1 (Next)** | Generalize activities | Medium | High | Associations |
| **P1 (Next)** | Products/Line Items | Medium | Medium | None |
| **P2** | Deletion semantics | Low | Low | None |
| **P2** | Enum constraints | Low | Medium | None |
| **P2** | Owner FK resolution | Low | Medium | None |
| **P3 (Future)** | Core CRM layer | Very High | Very High | P1 items |
| **P3 (Future)** | Field metadata | Medium | Medium | None |

---

## Open Decisions

1. **Single vs multiple association tables?**
   - Single `crm_object_associations` is more flexible but requires polymorphic queries
   - Multiple typed tables (deal-participants, contact-company) are simpler to query

2. **Activity table naming?**
   - Keep `crm_deal_tasks` and add columns (less migration risk)
   - Rename to `crm_activities` (cleaner model, more migration work)

3. **Core layer timing?**
   - Start core layer now (parallel development)
   - Wait until multi-CRM sync is stable

4. **Primary CRM selection?**
   - Per-organisation setting
   - Per-object type setting (e.g., deals from SFDC, contacts from HubSpot)
   - Per-record override

---

## Recommended Next Steps

1. **Implement P1: Generic Associations** - This unblocks contact-company and activity generalization
2. **Implement P1: Products/Line Items** - Low dependency, high value for deal accuracy
3. **Implement P1: Generalize Activities** - Required for Salesforce/Pipedrive compatibility
4. **Plan P3: Core Layer Architecture** - Design document before implementation
