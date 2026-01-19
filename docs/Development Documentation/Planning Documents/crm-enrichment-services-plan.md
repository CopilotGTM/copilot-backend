# CRM Organization-Level Enrichment Services Plan

> **Date**: January 11, 2026
> **Status**: Approved
> **Author**: Claude Code

---

## Executive Summary

This plan proposes a centralized **CRM Enrichment Architecture** that:

1. Creates a **single orchestrator** for organization-level enrichment
2. Adds **missing enrichment services** (Contact, Company)
3. Maintains **generic (provider-agnostic)** implementation
4. Keeps **backward compatibility** with existing code

---

## 1. Current State Analysis

### Existing Enrichment Services

Three enrichment services exist in `src/service/integrations/crm/entities/`:

| Service | Purpose | Fields Populated |
|---------|---------|------------------|
| `DealEnrichmentService` | Enriches deals + participants | `owner_name`, `owner_email`, `owner_id`, `primary_contact_name`, `company_name`, participant details |
| `OwnerEnrichmentService` | Resolves owner_id FKs | `owner_id`, `owner_name`, `owner_email` on deals, contacts, companies |
| `TaskEnrichmentService` | Enriches activities | `owner_id`, `deal_id`, `contact_id`, `company_id` on CRMDealTask |

### Current Call Sites

1. **Inline in Sync Services**: Called inside `DealSyncService.sync()` and `DealTaskSyncService.sync()`
2. **Dispatcher Functions**: Standalone wrapper functions in `dispatcher.py`:
   - `_enrich_participants_after_crm_sync()`
   - `_enrich_activities_after_crm_sync()`
   - `_enrich_owners_after_users_sync()`

### Current Problems

1. **No centralized orchestration** - enrichment is scattered across sync services and dispatcher
2. **Missing enrichments**:
   - `ContactEnrichmentService` doesn't exist (contacts need company and owner resolution)
   - `CompanyEnrichmentService` doesn't exist (companies need owner resolution)
3. **No org-level entry point** - can't trigger "enrich all entities for this org" easily

---

## 2. Key Design Decision: Generic vs Provider-Specific

### Recommendation: **GENERIC (Single Implementation)**

**Rationale:**

| Factor | Analysis |
|--------|----------|
| Data Format | Both HubSpot and Salesforce data is normalized into same ORM models |
| Enrichment Logic | Uses internal FKs and external IDs - no provider-specific fields |
| Current Implementation | All 3 existing services are already generic |
| Maintenance | Single codebase is easier to maintain |
| Future Providers | New providers (Pipedrive, Zoho) would work without changes |

**The enrichment layer operates AFTER normalization** - by the time we enrich, all provider-specific logic has been handled by fetchers/adapters. Enrichment just links internal entities together.

### Why NOT Provider-Specific?

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA FLOW                                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  HubSpot API ──► HubSpot Fetcher ──► Normalized ORM ──► Enrichment  │
│                      ▲                     ▲               ▲         │
│                      │                     │               │         │
│               Provider-specific      Same format     Generic logic   │
│                                                                      │
│  Salesforce API ─► Salesforce Fetcher ─► Normalized ORM ─► Enrichment│
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

By the enrichment stage, we're working with:

- `CRMDeal`, `CRMContact`, `CRMCompany`, `CRMUser`, `CRMDealTask`
- All use same columns: `external_id`, `owner_external_id`, etc.
- No HubSpot or Salesforce specific fields

---

## 3. Proposed Architecture

### 3.1 New Folder Structure

```
src/service/integrations/crm/
├── entities/                          # Existing sync services (keep as-is)
│   ├── deal_sync_service.py
│   ├── contact_sync_service.py
│   ├── company_sync_service.py
│   └── ...
│
└── enrichment/                        # NEW: Dedicated enrichment module
    ├── __init__.py                    # Exports all enrichment classes
    ├── orchestrator.py                # CRMEnrichmentOrchestrator
    ├── deal_enrichment_service.py     # MOVE from entities/
    ├── contact_enrichment_service.py  # NEW
    ├── company_enrichment_service.py  # NEW
    ├── owner_enrichment_service.py    # MOVE from entities/
    └── task_enrichment_service.py     # MOVE from entities/
```

### 3.2 Class Hierarchy

```
                    ┌─────────────────────────────────────┐
                    │     CRMEnrichmentOrchestrator       │
                    │     (Organization-level entry)      │
                    └───────────────┬─────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │                          │                          │
         ▼                          ▼                          ▼
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│DealEnrichment   │      │ContactEnrichment│      │CompanyEnrichment│
│Service          │      │Service (NEW)    │      │Service (NEW)    │
└─────────────────┘      └─────────────────┘      └─────────────────┘
         │                          │                          │
         │                          │                          │
         ▼                          ▼                          ▼
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│OwnerEnrichment  │      │TaskEnrichment   │      │(Uses Owner      │
│Service          │      │Service          │      │ Enrichment)     │
└─────────────────┘      └─────────────────┘      └─────────────────┘
```

---

## 4. Detailed Class Specifications

### 4.1 CRMEnrichmentOrchestrator (NEW)

**Purpose**: Single entry point for org-level enrichment, called from Tier 2 org_tasks.

```python
class CRMEnrichmentOrchestrator:
    """
    Organization-level CRM enrichment coordinator.

    Provides a single entry point to trigger enrichment for all or specific
    entity types within an organization after sync completes.
    """

    def __init__(self, db: AsyncSession, organisation_id: UUID):
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_after_deals_sync(self, integration_id: UUID) -> dict:
        """Called after deals sync - enriches deals + participants."""

    async def enrich_after_contacts_sync(self, integration_id: UUID) -> dict:
        """Called after contacts sync - enriches contacts + re-enriches participants."""

    async def enrich_after_companies_sync(self, integration_id: UUID) -> dict:
        """Called after companies sync - enriches companies + re-enriches deals/contacts."""

    async def enrich_after_users_sync(self, integration_id: UUID) -> dict:
        """Called after users sync - resolves owner_id across all entities."""

    async def enrich_after_activities_sync(self, integration_id: UUID) -> dict:
        """Called after activities sync - enriches tasks with related entity FKs."""

    async def enrich_all(self, integration_id: UUID) -> dict:
        """Full enrichment pass - useful for initial sync or repair."""
```

### 4.2 ContactEnrichmentService (NEW)

**Purpose**: Enriches contacts with company association and owner details.

```python
class ContactEnrichmentService:
    """
    Enriches CRMContact records with related entity information.
    """

    async def enrich_contacts(self, contacts: Iterable[CRMContact]) -> None:
        """Enrich contacts in-place with company and owner details."""

    async def enrich_contacts_for_integration(self, integration_id: UUID) -> int:
        """Fetch and enrich all contacts for a given integration."""

# Fields populated:
# - owner_id (FK to CRMUser)
# - owner_name (denormalized from CRMUser.full_name)
# - owner_email (denormalized from CRMUser.email)
# - company_id (FK to CRMCompany) - may already be set
# - company_name (denormalized from CRMCompany.name)
```

### 4.3 CompanyEnrichmentService (NEW)

**Purpose**: Enriches companies with owner details.

```python
class CompanyEnrichmentService:
    """
    Enriches CRMCompany records with owner information.
    """

    async def enrich_companies(self, companies: Iterable[CRMCompany]) -> None:
        """Enrich companies in-place with owner details."""

    async def enrich_companies_for_integration(self, integration_id: UUID) -> int:
        """Fetch and enrich all companies for a given integration."""

# Fields populated:
# - owner_id (FK to CRMUser)
# - owner_name (denormalized from CRMUser.full_name)
# - owner_email (denormalized from CRMUser.email)
```

### 4.4 Existing Services (Move to enrichment/)

Move existing services with minimal changes:

- `DealEnrichmentService` → keeps current functionality
- `OwnerEnrichmentService` → keeps current functionality
- `TaskEnrichmentService` → keeps current functionality

---

## 5. Enrichment Trigger Matrix

When each enrichment should run:

| After Sync | Enrichment To Run | Reason |
|------------|-------------------|--------|
| **Deals** | `DealEnrichmentService.enrich_deals()` | Populate owner, contact, company names |
| **Deals** | `DealEnrichmentService.enrich_participants()` | Populate participant details |
| **Contacts** | `ContactEnrichmentService.enrich_contacts()` | Populate owner, company |
| **Contacts** | `DealEnrichmentService.enrich_participants()` | Re-enrich now that contacts exist |
| **Contacts** | `TaskEnrichmentService.enrich_tasks()` | Resolve contact_id FKs |
| **Companies** | `CompanyEnrichmentService.enrich_companies()` | Populate owner |
| **Companies** | `DealEnrichmentService.enrich_deals()` | Re-enrich company_name |
| **Companies** | `ContactEnrichmentService.enrich_contacts()` | Re-enrich company_name |
| **Companies** | `TaskEnrichmentService.enrich_tasks()` | Resolve company_id FKs |
| **Users** | `OwnerEnrichmentService.enrich_owners()` | Resolve owner_id for all entities |
| **Activities** | `TaskEnrichmentService.enrich_tasks()` | Populate deal, contact, company FKs |

---

## 6. Usage Pattern

### From Tier 2 Org Tasks

```python
# In src/workers/integrations_worker/tasks/crm/deals/org_task.py

from src.service.integrations.crm.enrichment import CRMEnrichmentOrchestrator

async def _sync_deals_for_integration(...):
    # ... sync logic ...

    # After sync completes, trigger enrichment
    enricher = CRMEnrichmentOrchestrator(db=session, organisation_id=org_id)
    enrichment_result = await enricher.enrich_after_deals_sync(integration_id)

    return {
        "sync_metrics": metrics,
        "enrichment_metrics": enrichment_result
    }
```

---

## 7. Implementation Steps

### Phase 1: Create Enrichment Module Structure

1. Create `src/service/integrations/crm/enrichment/` directory
2. Create `__init__.py` with exports
3. Move existing services (keeping backward compatibility imports)

### Phase 2: Create New Enrichment Services

1. Create `ContactEnrichmentService`
2. Create `CompanyEnrichmentService`

### Phase 3: Create Orchestrator

1. Create `CRMEnrichmentOrchestrator`
2. Implement all `enrich_after_*` methods
3. Wire up to call appropriate enrichment services

### Phase 4: Integration (Later)

1. Update Tier 2 org_tasks to use orchestrator
2. Remove old inline enrichment calls from sync services
3. Update dispatcher.py to use orchestrator (or deprecate)

---

## 8. Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `src/service/integrations/crm/enrichment/__init__.py` | Module exports |
| `src/service/integrations/crm/enrichment/orchestrator.py` | CRMEnrichmentOrchestrator |
| `src/service/integrations/crm/enrichment/contact_enrichment_service.py` | ContactEnrichmentService |
| `src/service/integrations/crm/enrichment/company_enrichment_service.py` | CompanyEnrichmentService |

### Move Files

| From | To |
|------|-----|
| `entities/deal_enrichment_service.py` | `enrichment/deal_enrichment_service.py` |
| `entities/owner_enrichment_service.py` | `enrichment/owner_enrichment_service.py` |
| `entities/task_enrichment_service.py` | `enrichment/task_enrichment_service.py` |

### Update Files

| File | Changes |
|------|---------|
| `src/service/integrations/crm/entities/__init__.py` | Add backward-compat imports |
| `src/workers/integrations_worker/tasks/crm/*/org_task.py` | Use orchestrator (later) |
| `src/workers/integrations_worker/services/dispatcher.py` | Use orchestrator (later) |

---

## 9. Verification

### Unit Tests

- Test each enrichment service independently
- Test orchestrator correctly calls services based on entity type

### Integration Tests

- Sync deals → verify enrichment populates owner_name, company_name
- Sync contacts after deals → verify participant re-enrichment
- Sync users → verify owner_id resolved across all entities

### Manual Testing

```bash
# Trigger single entity sync and check enrichment
celery -A src.workers.integrations_worker.celery_app call \
  integrations.sync_crm_deals_org \
  --kwargs='{"organisation_id": "your-org-uuid"}'

# Check database for enriched fields
SELECT deal_name, owner_name, owner_email, company_name
FROM crm_deals WHERE integration_id = 'your-int-uuid';
```

---

## 10. Summary

| Aspect | Decision |
|--------|----------|
| **Architecture** | Generic (single implementation for all providers) |
| **Location** | New `enrichment/` submodule under CRM |
| **Entry Point** | `CRMEnrichmentOrchestrator` at org level |
| **Separation** | Each entity type has its own enrichment service |
| **Backward Compat** | Keep imports working from old locations |

---

## 11. On-Demand Reference Data Fetching

### Key Insight: Don't Sync Pipelines/Users Proactively

**Pipelines** and **Users** are reference data that rarely changes (every 6-8 months). Syncing them on a schedule is wasteful.

**New Approach**: Fetch reference data **on-demand** when enrichment detects missing references.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     ON-DEMAND REFERENCE DATA FLOW                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Deal Sync                                                                  │
│     │                                                                       │
│     ▼                                                                       │
│  Deal Enrichment                                                            │
│     │                                                                       │
│     ├─► Check: Does pipeline_external_id exist in crm_pipelines?            │
│     │      │                                                                │
│     │      ├─► YES → Use existing pipeline data                             │
│     │      │                                                                │
│     │      └─► NO → Trigger: fetch_missing_pipeline(external_id)            │
│     │               │                                                       │
│     │               ▼                                                       │
│     │          Fetch from HubSpot/Salesforce API                            │
│     │               │                                                       │
│     │               ▼                                                       │
│     │          Insert into crm_pipelines                                    │
│     │               │                                                       │
│     │               ▼                                                       │
│     │          Continue enrichment                                          │
│     │                                                                       │
│     └─► Check: Does owner_external_id exist in crm_users?                   │
│            │                                                                │
│            ├─► YES → Use existing user data                                 │
│            │                                                                │
│            └─► NO → Trigger: fetch_missing_user(external_id)                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Implementation

```python
class CRMEnrichmentOrchestrator:

    async def _ensure_pipeline_exists(self, pipeline_external_id: str) -> CRMPipeline | None:
        """Check if pipeline exists, fetch on-demand if missing."""
        pipeline = await self._get_pipeline_by_external_id(pipeline_external_id)
        if pipeline:
            return pipeline

        # Pipeline missing - fetch on-demand
        logger.info(f"Pipeline {pipeline_external_id} missing, fetching on-demand")
        return await self._fetch_and_store_pipeline(pipeline_external_id)

    async def _ensure_user_exists(self, user_external_id: str) -> CRMUser | None:
        """Check if user exists, fetch on-demand if missing."""
        user = await self._get_user_by_external_id(user_external_id)
        if user:
            return user

        # User missing - fetch on-demand
        logger.info(f"User {user_external_id} missing, fetching on-demand")
        return await self._fetch_and_store_user(user_external_id)
```

### Provider-Specific Fetch Logic

The **enrichment services are generic**, but the **on-demand fetch logic differs by provider**:

| Reference Data | HubSpot | Salesforce |
|----------------|---------|------------|
| **Pipeline** | GET `/crm/v3/pipelines/deals/{pipelineId}` | SOQL: `SELECT Id, Name FROM Opportunity.StageName` (stages embedded) |
| **User/Owner** | GET `/crm/v3/owners/{ownerId}` | GET `/services/data/vXX.0/sobjects/User/{userId}` |

---

## 12. Provider-Specific Enrichment Trigger Mapping

### Why Mapping Differs

While **enrichment services are generic** (same code), the **triggers** (which enrichments to call after each sync) can differ between providers due to:

- Different data models (HubSpot has separate Owners, Salesforce uses Users)
- Different relationship structures
- Different API capabilities

### HubSpot Enrichment Triggers

| After Sync | Enrichment Called | Includes On-Demand Fetch? |
|------------|-------------------|---------------------------|
| **Deals** | `DealEnrichmentService.enrich_deals()` | Yes - Pipeline, User |
| **Deals** | `DealEnrichmentService.enrich_participants()` | No |
| **Contacts** | `ContactEnrichmentService.enrich_contacts()` | Yes - User |
| **Contacts** | `DealEnrichmentService.enrich_participants()` | No |
| **Contacts** | `TaskEnrichmentService.enrich_tasks()` | No |
| **Companies** | `CompanyEnrichmentService.enrich_companies()` | Yes - User |
| **Companies** | `DealEnrichmentService.enrich_deals()` (company_name) | No |
| **Companies** | `ContactEnrichmentService.enrich_contacts()` (company_name) | No |
| **Activities** | `TaskEnrichmentService.enrich_tasks()` | Yes - User |
| **Pipelines** | *(Not scheduled - fetched on-demand)* | N/A |
| **Users** | *(Not scheduled - fetched on-demand)* | N/A |

### Salesforce Enrichment Triggers

| After Sync | Enrichment Called | Includes On-Demand Fetch? |
|------------|-------------------|---------------------------|
| **Opportunities** | `DealEnrichmentService.enrich_deals()` | Yes - User |
| **Opportunities** | `DealEnrichmentService.enrich_participants()` | No |
| **Contacts** | `ContactEnrichmentService.enrich_contacts()` | Yes - User |
| **Contacts** | `DealEnrichmentService.enrich_participants()` | No |
| **Contacts** | `TaskEnrichmentService.enrich_tasks()` | No |
| **Accounts** | `CompanyEnrichmentService.enrich_companies()` | Yes - User |
| **Accounts** | `DealEnrichmentService.enrich_deals()` (company_name) | No |
| **Accounts** | `ContactEnrichmentService.enrich_contacts()` (company_name) | No |
| **Tasks** | `TaskEnrichmentService.enrich_tasks()` | Yes - User |
| **Pipelines** | *(Salesforce has no separate pipeline - stages are inline)* | N/A |
| **Users** | *(Not scheduled - fetched on-demand)* | N/A |

### Key Differences

| Aspect | HubSpot | Salesforce |
|--------|---------|------------|
| **Pipeline Handling** | Separate entity - fetch on-demand by pipeline ID | No separate entity - stages embedded in Opportunity |
| **User/Owner Model** | "Owners" are a subset of users | "Users" are all Salesforce users |
| **Pipeline Fetch** | GET `/crm/v3/pipelines/deals/{id}` | Not applicable |
| **User Fetch** | GET `/crm/v3/owners/{id}` | GET `/sobjects/User/{id}` |

---

## 13. Orchestrator Method Mapping

### `enrich_after_deals_sync()`

| Step | HubSpot | Salesforce |
|------|---------|------------|
| 1 | Check/fetch missing pipelines | *(skip - no separate pipeline)* |
| 2 | Check/fetch missing owners | Check/fetch missing users |
| 3 | `DealEnrichmentService.enrich_deals()` | `DealEnrichmentService.enrich_deals()` |
| 4 | `DealEnrichmentService.enrich_participants()` | `DealEnrichmentService.enrich_participants()` |

### `enrich_after_contacts_sync()`

| Step | HubSpot | Salesforce |
|------|---------|------------|
| 1 | Check/fetch missing owners | Check/fetch missing users |
| 2 | `ContactEnrichmentService.enrich_contacts()` | `ContactEnrichmentService.enrich_contacts()` |
| 3 | `DealEnrichmentService.enrich_participants()` | `DealEnrichmentService.enrich_participants()` |
| 4 | `TaskEnrichmentService.enrich_tasks()` | `TaskEnrichmentService.enrich_tasks()` |

### `enrich_after_companies_sync()`

| Step | HubSpot | Salesforce |
|------|---------|------------|
| 1 | Check/fetch missing owners | Check/fetch missing users |
| 2 | `CompanyEnrichmentService.enrich_companies()` | `CompanyEnrichmentService.enrich_companies()` |
| 3 | `DealEnrichmentService.enrich_deals()` | `DealEnrichmentService.enrich_deals()` |
| 4 | `ContactEnrichmentService.enrich_contacts()` | `ContactEnrichmentService.enrich_contacts()` |
| 5 | `TaskEnrichmentService.enrich_tasks()` | `TaskEnrichmentService.enrich_tasks()` |

### `enrich_after_activities_sync()`

| Step | HubSpot | Salesforce |
|------|---------|------------|
| 1 | Check/fetch missing owners | Check/fetch missing users |
| 2 | `TaskEnrichmentService.enrich_tasks()` | `TaskEnrichmentService.enrich_tasks()` |

---

## 14. Updated Summary

| Aspect | Decision |
|--------|----------|
| **Architecture** | Generic enrichment services, provider-specific trigger logic |
| **Reference Data** | On-demand fetch (pipelines, users) - NOT scheduled |
| **Location** | New `enrichment/` submodule under CRM |
| **Entry Point** | `CRMEnrichmentOrchestrator.enrich_after_{entity}_sync()` |
| **Provider Handling** | Orchestrator has provider-aware trigger logic |
| **Separation** | Each entity type has its own enrichment service |

---

## Related Documentation

- [CRM Sync Flow](../Technical%20Documentation/integration/sync_tasks/crm-sync-flow.md) - CRM three-tier sync architecture
- [HubSpot CRM Sync](../Technical%20Documentation/integration/sync_tasks/hubspot-crm-sync.md) - HubSpot-specific patterns
- [Scheduled Sync Architecture](../Technical%20Documentation/integration/sync_tasks/scheduled-sync-architecture.md) - Three-tier pattern overview
