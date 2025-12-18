# CRM Integration Schema Review (Multi-CRM Readiness)

**Last Updated:** December 16, 2025  
**Document Version:** 2.0

## Context

You currently sync HubSpot CRM data into a set of "normalized" tables under `src/domain/models/organisation_tables/sync/crm/`. The goal is that this schema:

1. Works for **multiple CRM providers** (HubSpot today, Salesforce/Pipedrive/Zoho/etc. later).
2. Supports a **pre-sales/sales app** that needs: deals/opportunities, tasks/activities, participants, and related context.
3. Can evolve toward **"our own CRM"** (where internal objects become the source of truth, with external CRMs as connected systems).

This document reviews each table for: what's correct, what's HubSpot-shaped, what's missing for multi‑CRM and for an eventual "internal CRM" layer.

---

## CRM Comparison Matrix (Salesforce vs HubSpot vs Pipedrive vs Zoho)

Understanding the differences across major CRMs is critical for designing a provider-agnostic schema:

| Concept | HubSpot | Salesforce | Pipedrive | Zoho CRM |
|---------|---------|------------|-----------|----------|
| **Deal/Opportunity** | Deal | Opportunity | Deal | Deal/Potential |
| **Pipeline concept** | First-class object | Sales Process / Record Type | Pipeline | Blueprint/Pipeline |
| **Stages** | Deal Stages in Pipeline | OpportunityStage | Stage in Pipeline | Stage |
| **Contact ↔ Account** | Association (many-to-many) | AccountContactRelation + primary AccountId | Person ↔ Organization | Contact ↔ Account |
| **Contact Role on Deal** | Association labels | OpportunityContactRole (first-class object with Role field) | Participant | Contact Role |
| **Activities** | Engagements (calls, emails, meetings, notes, tasks) | Task, Event, Activity | Activity (unified model) | Task, Event, Call |
| **Activity ↔ Objects** | Via associations | WhatId (polymorphic) + WhoId | Flexible linking | Related To |
| **Products on Deals** | Line Items | OpportunityLineItem | Products | Products |
| **Owner** | hubspot_owner_id | OwnerId (FK to User) | owner_id | Owner |
| **Custom Fields** | Properties (JSON-like) | Custom Fields (strongly typed) | Custom Fields (key-hash) | Custom Fields |
| **Field History** | Property history (if enabled) | OpportunityFieldHistory | Changelog | Audit Log |
| **Close dates** | closedate | CloseDate (required) | expected_close_date | Closing_Date |
| **Probability** | deal_probability (manual or stage-derived) | Probability (stage-derived, overridable) | Not first-class | Probability |
| **Forecast Category** | forecast_category | ForecastCategoryName (stage-derived, overridable) | Not native | Forecast |
| **Currency** | deal_currency_code | CurrencyIsoCode | currency | Currency |

### Key Observations for Multi-CRM Design

1. **Contact Roles are critical for Salesforce**: Salesforce has `OpportunityContactRole` as a first-class junction object with role semantics. Your `crm_deal_participants` table handles this well, but needs to support the richer role semantics from Salesforce.

2. **Pipedrive has "Persons" not "Contacts"**: Terminology mapping required, but conceptually compatible.

3. **Activity models vary significantly**: Salesforce separates Task and Event; HubSpot has unified "engagements" by type; Pipedrive has a single Activity model.

4. **Products/Line Items are universal**: All CRMs support products/line items on deals, but with varying association mechanisms.

5. **Forecast categories**: Salesforce has sophisticated stage-to-forecast mapping; HubSpot has simpler forecast_category; Pipedrive lacks native forecasting.

---

## Big Picture: What’s Already Correct (Provider-Agnostic)

### 1) Integration-scoped identity is a strong foundation

All CRM tables inherit `SyncedRecordMixin`, which adds:

- `organisation_id` (tenant isolation)
- `integration_id` (FK → `knowledge_integrations.integration_id`)
- `is_deleted` (soft-delete reflecting provider state)
- `created_at`, `updated_at` (internal timestamps via `TimestampMixin`)

Most CRM tables also enforce `UNIQUE (integration_id, external_id)`. This is the core requirement for multi‑CRM syncing: **idempotent upserts** per connected account.

### 2) “Normalized + raw JSON” gives you flexibility

Most tables have:

- “common query” columns (e.g., `email`, `amount`, `stage_external_id`)
- `properties` (JSONB) for provider custom fields and provider-specific extras
- `data_payload` (JSONB) for raw payload/debug/replay
- sometimes `metrics` (JSONB) for derived analytics

This pattern is what makes the schema **diverse enough** to hold different CRMs without constantly migrating columns.

### 3) Pipeline/stage normalization is broadly compatible

The split into `crm_pipelines` and `crm_deal_stages` supports HubSpot and is also conceptually compatible with most CRMs (even if some need mapping from “record type/sales process” into “pipeline/stage”).

### 4) Deal participant links are a good abstraction

`crm_deal_participants` is a pragmatic way to represent:

- deal ↔ contacts
- deal ↔ companies/accounts
- deal ↔ internal owners/collaborators
- future: deal ↔ custom objects (via `CUSTOM`)

It’s not perfect (see gaps below), but it’s a solid start for pre‑sales workflows.

---

### 5) Cross-integration participant identity already exists

Beyond CRM-specific tables, the system already has a canonical identity layer:

- `integration_participants` and `integration_participant_identities` (see `src/domain/models/organisation_tables/sync/participant_models.py`)
- (Planned) a mapping table like `participant_crm_links` to map one canonical participant to multiple CRM records across integrations

This is a strong building block for multi-CRM because it supports resolving the same person across email/calendar/CRM providers, and it can become the place where "one person" lives even when multiple CRMs are connected.

---

## Table-by-Table Review

### 1) `crm_companies` (`CRMCompany`)

#### What’s correct — `crm_companies`

- Strong identity: `external_id` + `integration_id` uniqueness.
- Good “common CRM” fields: `name`, `domain`, `industry`, `annual_revenue`, `number_of_employees`, address/location, owner fields.
- Flexibility: `properties`, `metrics`, `data_payload`.
- Practical indexes for common filters (domain/name/owner/lifecycle/sync recency).

#### HubSpot-shaped / portability notes — `crm_companies`

- `lifecycle_stage` is HubSpot terminology, but can still be treated as a best-effort normalized field (map Salesforce/Zoho equivalents where possible, otherwise store in `properties`).
- `is_active` is not universal; many CRMs prefer “archived/deleted”. You already have `is_deleted` via the mixin, so `is_active` can be redundant.

#### Missing for multi‑CRM / “our own CRM” — `crm_companies`

- No first-class relationship model to contacts (company/account ↔ contacts) or parent-child company hierarchies. Those relationships are important outside HubSpot.

---

### 2) `crm_contacts` (`CRMContact`)

#### What’s correct — `crm_contacts`

- Core identity + profile fields (`first_name`, `last_name`, `email`, phones, title).
- `additional_emails` as JSONB list is a good cross-provider approach.
- Activity timestamps (`last_contacted_at`, `last_activity_at`) are useful for pre‑sales.
- Flexibility via `properties`, `metrics`, `data_payload`.

#### HubSpot-shaped / portability notes — `crm_contacts`

- `marketing_status` and `lifecycle_stage` are HubSpot-friendly fields; other CRMs have similar but not identical concepts.

#### Missing for multi‑CRM / “our own CRM” — `crm_contacts`

- Implemented: contact ↔ company linkage can be normalized via `crm_object_associations` (contact → company), with `company_name` remaining as a denormalized display field.
  - Note: current population is best-effort during contact sync (typically a primary company). Many-to-many sync and stale association cleanup can be added incrementally.

---

### 3) `crm_users` (`CRMUser`)

#### What’s correct — `crm_users`

- Normalized owner/user profile with `external_id`.
- Team membership via `team_ids` is flexible.
- `is_active` and canonical `is_deleted` cover common CRM user lifecycle patterns (with `is_deleted_remote` retained only as a backwards-compatible alias).

#### HubSpot-shaped / portability notes — `crm_users`

- Teams/roles differ widely by CRM (Salesforce roles/queues, HubSpot teams, Pipedrive visibility groups). Keeping teams as raw IDs is fine; treat deeper semantics as provider-specific in `properties`.

#### Missing for multi‑CRM / “our own CRM” — `crm_users`

- Deals/contacts/tasks store `owner_external_id` and owner name/email but do not maintain a consistent **FK link** to `crm_users.user_id`. You can do it via enrichment, but it’s not structurally enforced.

---

### 4) `crm_pipelines` (`CRMPipeline`) and `crm_deal_stages` (`CRMDealStage`)

#### What’s correct — `crm_pipelines` / `crm_deal_stages`

- Provider-agnostic representation of a pipeline and ordered stages.
- `stage_state` (open/won/lost) and probability constraints enable consistent analytics.
- `object_type` on pipelines is a good forward-compat hook (deals vs tickets vs other objects).
- JSON `properties` + `data_payload` retain provider details.

#### HubSpot-shaped / portability notes — `crm_pipelines` / `crm_deal_stages`

- Some CRMs don’t have “pipelines” as a first-class object; they have “sales processes” or “record types”. You can still project those into `crm_pipelines` and `crm_deal_stages` as a normalization layer.

#### Missing for multi‑CRM / “our own CRM” — `crm_pipelines` / `crm_deal_stages`

- No table for “field metadata” (stage definitions, picklists, custom fields definitions). This is optional, but becomes important when adding CRMs with heavy customization.

---

### 5) `crm_deals` (`CRMDeal`)

#### What’s correct — `crm_deals`

- Captures the core deal/opportunity fields used in most CRMs: `name`, `amount`, `currency`, close dates, status, probability.
- Stores both:
  - provider identifiers (`pipeline_external_id`, `stage_external_id`) and
  - normalized FK references (`pipeline_id`, `stage_id`)
  This is good for resilience (you can show stage text even if stages aren’t synced yet).
- `crm_url` is universally useful (deep link to provider UI).
- Rich extensibility: `properties`, `metrics`, `associations_payload`, `data_payload`.
- Supports internal augmentation via `deal_summary_data` (AI output).

#### HubSpot-shaped / portability notes — `crm_deals`

- `days_to_close` is explicitly HubSpot-derived; this should likely live in `metrics` as a computed field for all providers (or be renamed to a provider-neutral meaning).
- `associated_contact_count`, `associated_company_count`, `note_count` are provider-dependent.

#### Missing for multi‑CRM / “our own CRM” — `crm_deals`

- Company/contact relationships are only partially normalized:
  - fields like `company_external_id`, `primary_contact_external_id` are useful, but there is no enforced FK to `crm_companies` / `crm_contacts`.
  - multi-CRM reality: many CRMs allow many contacts, many accounts, and “contact roles” on opportunities. That belongs in a relationship/association model (see `crm_deal_participants` and the “gaps” section).
- The table is fundamentally **integration-scoped** (via required `integration_id`). If you want “our own CRM” objects created internally, you’ll need either:
  - an internal “CRM integration” concept, or
  - a separate canonical table that is not tied to a single integration.

---

### 6) `crm_deal_participants` (`CRMDealParticipant`)

#### What’s correct — `crm_deal_participants`

- This is the most important multi‑CRM abstraction in the current design.
- Supports multiple participant entity types (`CONTACT`, `COMPANY`, `USER`, plus `CUSTOM`).
- `relationship_role` as a free string is good because CRMs differ wildly in association labels.
- Includes `related_local_id` + `related_name` + `related_email` for enrichment and denormalized display (good for product UX).

#### HubSpot-shaped / portability notes — `crm_deal_participants`

- The data population is currently HubSpot-driven (association types like `deal_to_contact_unlabeled`), but the schema itself is portable.

#### Missing for multi‑CRM / “our own CRM” — `crm_deal_participants`

- It is **deal-specific**. Other CRMs often need generic associations, which are better modeled in `crm_object_associations`:
  - contact ↔ company
  - task/activity ↔ contact/deal/company
  - deal ↔ line items/products/quotes
  - company ↔ parent company
- `related_local_id` is polymorphic and not FK-enforced. That’s normal for polymorphic associations, but it limits referential integrity.

---

### 7) `crm_deal_stage_history` (`CRMDealStageHistory`)

#### What’s correct — `crm_deal_stage_history`

- Stage transition modeling is provider-agnostic conceptually.
- Supports entered/exited timestamps and snapshot metrics (amount/probability).
- Excellent for analytics (velocity, conversion, stuck deals).

#### Portability notes — `crm_deal_stage_history`

- Some CRMs only provide stage history if a feature is enabled (e.g., field history tracking). Expect sparse or missing data for some providers.

#### Missing for multi‑CRM — `crm_deal_stage_history`

- Only stage history is modeled; not other opportunity field histories (amount changes, close date changes). That’s fine if stage history is the only required dimension.

---

### 8) `crm_activities` (`CRMActivity`)

#### What’s correct — `crm_activities`

- A canonical task/activity table is essential for pre‑sales workflows.
- Captures common fields: `subject`, `body`, `status`, `task_type`, `priority`, `due_date`, owner fields, completion timestamps.
- Flexible `properties`/`metrics`/`data_payload`.

#### HubSpot-shaped / portability notes — `crm_activities`

- Many CRMs model “activities” rather than “deal tasks”:
  - Salesforce has Task and Event
  - HubSpot has tasks and engagements
  - Pipedrive has activities
  The *concept* is portable, but the current table is named and keyed as deal-centric.

#### Missing for multi‑CRM / “our own CRM” — `crm_activities`

- Activity association is limited to `deal_id` / `deal_external_id`. Tasks often relate to contacts/companies/leads **without** a deal.
- Status/type/priority are stored as `str` (not the provided enums), which reduces consistency for cross-CRM analytics unless you enforce canonicalization in the sync layer.
- No participant/attendee modeling for “MEETING” task type (often needed in sales workflows).

---

### 9) `deal_stakeholders` (`DealStakeholder`)

#### What’s correct — `deal_stakeholders`

- This is a good “value add” table that is *not* CRM-provider-specific:
  - stance, decision power, temperature, engagement score
  - motivation/pain points/keywords
  - supports manual + discovered stakeholders
- Links to `crm_deals` and optionally `crm_contacts` (good for enrichment).

#### Portability notes — `deal_stakeholders`

- This table is more “your product’s CRM layer” than “provider sync layer”.

#### Structural note

- Implemented: `DealStakeholder` now uses `FlexibleSyncedRecordMixin` so `integration_id` is nullable, supporting `MANUAL`, `CALENDAR_DISCOVERY`, and `EMAIL_DISCOVERY` sources.
  - Migration: `migrations/org_migrations/versions/8f1c2d3e4a5b_014_make_deal_stakeholders_integration_id_nullable.py`

---

## The Main Gaps (Across Tables)

### Gap A: Relationship modeling is too deal-centric

You have a strong `crm_deal_participants` table, but multi‑CRM systems usually need a **generic association** layer between arbitrary CRM objects (contact ↔ company, activity ↔ contact, deal ↔ line item, etc.).

Implemented: `crm_object_associations` provides this generic association layer; remaining work is mainly about expanding population/coverage across providers and relationship types.

#### Why this matters

- Different CRMs expose different “association graphs”.
- Pre‑sales workflows (meeting prep, relationship maps, org charts) rely on more than deal ↔ contact links.

### Gap B: Activities should be generalized beyond “deal tasks”

#### Status: ✅ Implemented

The `crm_activities` table now provides a canonical activity model with:

- Multiple activity types: tasks, calls, meetings/events, emails, notes
- Polymorphic associations to deals/contacts/companies via `related_object_type` + `related_external_id`
- Direct FK columns: `deal_id`, `contact_id`, `company_id`
- Meeting attendee support via `attendees` JSONB
- Communication fields: `direction`, `call_outcome`, `duration_minutes`

### Gap C: No “canonical internal CRM object” layer yet

Because `integration_id` is required on core CRM entities, the schema currently assumes:

- every company/contact/deal/task originates from an external CRM integration

If the roadmap is to “behave like our own CRM”, you will eventually want:

1. **Internal canonical objects** (what your app edits/owns)
2. **Mappings to external CRMs** (what you sync in/out)

Without that split, multi‑CRM orgs will have duplicate deals/contacts across integrations and no clear “source of truth”.

### Gap D: Provider metadata / field definitions are not stored

This is not required for syncing “happy path” data, but it becomes important for:

- custom field mapping and UI (picklists, labels)
- consistent normalization across CRMs
- supporting write-back actions later (create/update in CRM)

---

## Recommendations (Minimal Changes vs Strategic Changes)

### Minimal (keeps current tables, improves multi‑CRM)

1. Implemented: add a generic association table (or generalize `crm_deal_participants`):
   - `crm_object_associations`: `(integration_id, from_object_type, from_external_id, to_object_type, to_external_id, role, properties, data_payload, source_created_at, source_modified_at, last_seen_at)`
2. Implemented (initial): normalize contact ↔ company relationships via `crm_object_associations` during contact sync.
3. Implemented: Generalize activities:
   - Renamed `crm_deal_tasks` to `crm_activities` with polymorphic `related_object_type` + `related_external_id` + direct FK columns.
4. Decide on one deletion/archival strategy:
   - Implemented: `is_deleted` (mixin) is canonical; redundant `is_deleted_remote` DB columns were removed (alias properties remain for backwards compatibility).

### Strategic (supports “our own CRM” long term)

1. Introduce canonical internal tables (example naming):
   - `core_accounts`, `core_people`, `core_opportunities`, `core_activities`
2. Add mapping tables:
   - `crm_object_mappings` linking `(organisation_id, integration_id, external_object_type, external_id)` → `(internal_object_type, internal_id)`
3. Treat `crm_*` tables as the **integration import/cache layer**, and treat `core_*` as the **product CRM layer**.

This separation makes multi‑CRM orgs workable (one canonical record, many external mappings) and sets you up for write-back and internal creation.

---

## Salesforce-Specific Gap Analysis

Given that Salesforce is the most complex CRM to integrate (and often the most critical for enterprise customers), here's a detailed analysis of Salesforce-specific considerations:

### Salesforce Objects → Current Schema Mapping

| Salesforce Object | Current Schema Target | Gap Level |
|-------------------|----------------------|-----------|
| **Opportunity** | `crm_deals` | ✅ Good fit |
| **OpportunityStage** | `crm_deal_stages` | ✅ Good fit |
| **Account** | `crm_companies` | ✅ Good fit |
| **Contact** | `crm_contacts` | ✅ Good fit |
| **User** | `crm_users` | ✅ Good fit |
| **OpportunityContactRole** | `crm_deal_participants` | 🟡 Partial - needs `is_primary`, richer role |
| **AccountContactRelation** | `crm_object_associations` | 🟡 Partial - schema supports it; sync coverage is provider-specific |
| **Task** | `crm_activities` | ✅ Now supports `WhatId`/`WhoId` via polymorphic columns |
| **Event** | `crm_activities` | ✅ Now supports attendees, end_date, duration |
| **OpportunityLineItem** | ❌ Missing | 🟡 Optional - often CPQ/billing-adjacent; can be deferred for pre-sales workflows |
| **Product2/PricebookEntry** | ❌ Missing | 🟡 Optional - product catalog support can be added later |
| **OpportunityFieldHistory** | `crm_deal_stage_history` | 🟡 Partial - only stage history |
| **RecordType** | ❌ Missing | 🟡 Minor - map to pipeline concept |
| **Lead** | ❌ Missing | 🟡 Future - separate object type |

### Key Salesforce Fields to Support

#### OpportunityContactRole (critical for sales intelligence)

```text
Role (string - picklist like "Decision Maker", "Economic Buyer")
IsPrimary (boolean)
ContactId (FK)
OpportunityId (FK)
```

Current `crm_deal_participants` has `relationship_role` (string) but:

- No `is_primary` flag
- No way to distinguish "unlabeled association" from "Decision Maker role"

**Recommendation:** Add `is_primary: bool` column to `crm_deal_participants`

#### Task/Event polymorphic associations

```text
WhatId (polymorphic FK - Opportunity, Account, Case, etc.)
WhoId (polymorphic FK - Contact, Lead)
```

Status: ✅ Implemented in `crm_activities`

The `crm_activities` table now has:

- `related_object_type` (what type)
- `related_external_id` / `related_local_id` (what's ID)
- `contact_id` / `contact_external_id` (who - contact)
- `company_id` / `company_external_id` (what - company)
- `deal_id` / `deal_external_id` (what - deal)

---

## Implementation Roadmap Summary

### Phase 1: Foundation (P1 Items) - 🟡 Partial

1. **Generic Associations Table** ✅
   - Created `crm_object_associations` model
   - Populate in parallel with existing `crm_deal_participants`
   - Migrate contact↔company relationships

2. **Products/Line Items** 🔴
   - Universal across CRMs but deferred (often CPQ/billing-adjacent)

3. **Activity Generalization** ✅
   - Renamed `crm_deal_tasks` → `crm_activities`
   - Added polymorphic columns + contact/company FKs
   - Supports activities without deals

### Phase 2: Enhancement (P2 Items) - 2-4 weeks

1. **Owner FK Resolution**
   - Add `owner_id` FK to deals/contacts/companies
   - Run enrichment to resolve external IDs

2. **Enum Standardization**
   - Add check constraints for task status/type/priority
   - Create mapping layer in sync services

### Phase 3: Strategic (P3 Items) - 8-12 weeks

1. **Core CRM Layer Design**
   - Design `core_*` table schema
   - Define sync-to-core pipelines
   - Plan migration strategy

2. **Write-Back Support**
   - Store field metadata from providers
   - Implement create/update in CRM

---

## Conclusion

The current CRM schema is **mostly provider-agnostic** and can support additional CRMs for the *deal + contact + company + user + pipeline/stage* slice, especially because you store raw provider fields in JSON.

The primary risks for "smoothly integrating others" and for "becoming our own CRM" are:

1. **Relationship modeling beyond deals** (associations) - Critical for Salesforce OpportunityContactRole and AccountContactRelation
2. **Activity modeling beyond deal-linked tasks** - Required for Salesforce Task/Event polymorphism
3. **Products/line items** - Universal across CRMs but currently missing
4. **Lack of a canonical internal CRM layer** (source of truth separate from integrations)

If you address those gaps, the rest of the schema (identity strategy, JSON flexibility, pipeline/stage normalization) is a strong base to build on.

---

## Related Documents

- [CRM Integration Schema Update Proposals](./crm-integration-schema-update-proposals.md) - Detailed implementation proposals
- [Integration Access Control](../../API%20Documentation/integration-access-control-update.md) - Security model for integrations
