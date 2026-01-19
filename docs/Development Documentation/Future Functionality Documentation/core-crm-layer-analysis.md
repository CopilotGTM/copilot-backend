# Core CRM Layer: Analysis & Decision Document

**Created:** December 16, 2025  
**Purpose:** Evaluate whether the "core CRM layer" should be implemented now or deferred

---

## Executive Summary

The "core CRM layer" (`core_accounts`, `core_people`, `core_opportunities`, `core_activities`) was proposed as P3 (strategic/future) in the schema review. It creates an **internal source of truth** separate from the provider-synced `crm_*` tables.

**Recommendation:** **Defer implementation.** The use cases that justify this layer are future-oriented. The current single-CRM-per-org architecture works well. Implementing now adds complexity without immediate value.

---

## What Is the Core CRM Layer?

### Current Architecture (Single Layer)

```mermaid
┌─────────────────────────────────────────────────┐
│              YOUR APPLICATION                   │
│     (reads crm_deals, crm_contacts, etc.)       │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│              SYNC LAYER (crm_*)                 │
│  crm_deals, crm_contacts, crm_companies,        │
│  crm_activities                                 │
│  Each row: (integration_id, external_id) = PK   │
└─────────────────────┬───────────────────────────┘
                      │
          ┌───────────┴───────────┐
          │                       │
    ┌─────▼─────┐           ┌─────▼─────┐
    │  HubSpot  │           │ Salesforce │
    │  (Org A)  │           │  (Org B)   │
    └───────────┘           └────────────┘
```

**Key characteristic:** Each record is owned by ONE integration. Works perfectly when each org uses ONE CRM.

### Proposed Architecture (Two Layers)

```mermaid
┌─────────────────────────────────────────────────┐
│              YOUR APPLICATION                   │
│     (reads/writes core_* tables)                │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│              CORE LAYER (core_*)                │
│  core_accounts, core_people, core_opportunities │
│  Each row: organisation_id only (no integration)│
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│           CRM OBJECT MAPPINGS                   │
│  Links core records ↔ multiple CRM records      │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│              SYNC LAYER (crm_*)                 │
│  crm_deals, crm_contacts (unchanged)            │
└─────────┬───────────────────────┬───────────────┘
          │                       │
    ┌─────▼─────┐           ┌─────▼─────┐
    │  HubSpot  │           │ Salesforce │
    │  (same    │           │  (same     │
    │   org!)   │           │   org!)    │
    └───────────┘           └────────────┘
```

**Key characteristic:** Core records exist independent of any CRM. One core record can map to multiple CRM records.

---

## Use Cases That Justify the Core Layer

### Use Case 1: Multi-CRM Organizations

**Scenario:** Enterprise customer uses HubSpot for marketing + Salesforce for sales.

**Problem without core layer:**

- Same company "Acme Corp" exists as:
  - `crm_companies` row with `integration_id=HubSpot, external_id="hs_123"`
  - `crm_companies` row with `integration_id=Salesforce, external_id="sf_456"`
- Application sees TWO companies, not one
- Analytics are fragmented
- User has to mentally merge data

**Solution with core layer:**

- One `core_accounts` row for "Acme Corp"
- Two mapping rows linking it to both CRM records
- Application sees ONE company with unified data

**Is this needed now?**

- ❓ How many customers currently use multiple CRMs?
- ❓ Is multi-CRM a sales priority in next 6-12 months?
- If answer is "very few" or "not a priority" → *Defer*

### Use Case 2: "Your Own CRM" Mode

**Scenario:** Customer wants to use the platform without connecting any external CRM.

**Problem without core layer:**

- `crm_deals` requires `integration_id` (not nullable)
- Cannot create a deal without a CRM integration
- Forces customers to connect CRM even if they don't want to

**Solution with core layer:**

- Create `core_opportunities` directly (no integration required)
- Customer can use the platform standalone
- If they connect CRM later, records get mapped

**Is this needed now?**

- ❓ Is "standalone mode" / "freemium without CRM" on the roadmap?
- ❓ Are prospects asking for this?
- If "no" → *Defer*

**Alternative approach (without core layer):**

- Make `integration_id` nullable on `crm_deals` (like we did for `deal_stakeholders`)
- Create a "virtual" internal integration type
- Less architectural change, achieves similar result

### Use Case 3: CRM Migration

**Scenario:** Customer migrating from HubSpot to Salesforce.

**Problem without core layer:**

- Historical data in HubSpot becomes orphaned when integration disconnected
- New Salesforce data has no link to history
- Analytics/reports break across the migration boundary

**Solution with core layer:**

- Core records persist regardless of CRM changes
- Old HubSpot mappings marked inactive
- New Salesforce mappings added to same core records
- Continuous history maintained

**Is this needed now?**

- ❓ How often do customers migrate CRMs?
- ❓ Is preserving history across migration a requirement?
- Typically rare (1-2 times in company lifetime) → *Defer*

### Use Case 4: Write-Back to CRM

**Scenario:** User creates/edits data in your app, changes sync back to CRM.

**Problem without core layer:**

- Direct writes to `crm_deals` can conflict with sync
- No clear "source of truth" for conflict resolution
- Multi-CRM write-back (create in both HubSpot and Salesforce) is complex

**Solution with core layer:**

- User writes to `core_*` tables
- Sync service detects changes and pushes to mapped CRMs
- `sync_direction` field controls which CRMs get updates
- `is_primary_source` helps resolve conflicts

**Is this needed now?**

- ❓ Is write-back functionality planned?
- ❓ Or is the platform read-only from CRM perspective?
- If read-only for foreseeable future → *Defer*

### Use Case 5: Data Quality / Deduplication

**Scenario:** Same contact appears multiple times due to CRM data issues.

**Solution with core layer:**

- Merge duplicate `core_people` records
- Keep `merged_into_id` pointer for audit
- All CRM records still exist but map to survivor

**Is this needed now?**

- ❓ Is dedup a current pain point?
- ❓ Or is CRM data quality "their problem"?
- If not a priority → *Defer*

---

## Arguments FOR Implementing Now

| Argument | Counter-Argument |
|----------|------------------|
| Schema changes are easier before data exists | Migration files work fine; we're already doing schema changes |
| Future-proof architecture | YAGNI (You Aren't Gonna Need It) - adds complexity now for uncertain future |
| Cleaner separation of concerns | Current architecture is simple and works |
| Enables faster feature development later | Only if those features are actually built |

## Arguments FOR Deferring

| Argument | Details |
|----------|---------|
| **No immediate use case** | All use cases are hypothetical/future |
| **Added complexity** | 5 new tables, mapping logic, sync pipelines to build |
| **Development cost** | Significant effort to build sync-to-core pipeline (P3.3), migrate reads (P3.4), add write-back (P3.5) |
| **Maintenance burden** | Two sets of tables to keep in sync |
| **Cognitive load** | Developers need to understand two-layer architecture |
| **Can be added later** | Schema migrations can add these tables when needed |

---

## When Should We Implement This?

### Trigger Conditions (Implement when ANY is true)

1. **Multi-CRM customer signed** - Enterprise deal requires HubSpot + Salesforce support
2. **"Own CRM" feature prioritized** - Product decides to support standalone mode
3. **Write-back feature prioritized** - Product decides users should create/edit CRM data
4. **CRM migration requirement** - Customer needs to preserve history across CRM change

### Implementation Timeline (When Triggered)

| Phase | Effort | What's Involved |
|-------|--------|-----------------|
| P3.1-3.2: Schema + Models | Planned | Create core tables + mapping table |
| P3.3: Sync-to-Core Pipeline | 2-3 weeks | Service to populate core tables from crm_* on sync |
| P3.4: Migrate Reads | 2-4 weeks | Update queries to read from core_*instead of crm_* |
| P3.5: Write-Back | 3-4 weeks | Bidirectional sync, conflict resolution |

**Total estimate if triggered:** 7-11 weeks of development

---

## Recommendation

### Immediate Action: Defer

Keep the current architecture (single CRM sync layer) until a trigger condition is met. This avoids building and maintaining a second set of canonical tables before they provide value.

### Future Triggers

Add to product roadmap discussions:

- "Do we need multi-CRM support?"
- "Do we want standalone/freemium mode?"
- "Do we want write-back to CRM?"

If answer becomes "yes" to any, revisit this document and implement.
