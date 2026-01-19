# Incremental Sync Implementation Plan: Other CRM Entities

**Version:** 1.0
**Created:** 2026-01-06
**Status:** Planning
**Related:** [Deal Incremental Sync Plan](./incremental-deal-sync-implementation-plan.md)

---

## Executive Summary

This document outlines the plan to extend incremental sync functionality to all remaining CRM entities beyond Deals/Opportunities. The goal is to optimize sync performance by processing only changed records, reducing database load, and improving sync speed.

**Key Difference from Deal Implementation:** These entities do not have application-generated fields that need protection (like `deal_summary_data`), making the implementation simpler.

---

## Current State Analysis

### Entities Requiring Updates

| Entity | Sync Service | HubSpot Fetcher | Salesforce Fetcher | Priority |
|--------|--------------|-----------------|-------------------|----------|
| Contact | `contact_sync_service.py` | `contact_fetcher.py` | `contact_fetcher.py` | High |
| Company | `company_sync_service.py` | `company_fetcher.py` | `account_fetcher.py` | High |
| Deal Task | `deal_task_sync_service.py` | `deal_task_fetcher.py` | `task_fetcher.py` | Medium |
| Owner | `owner_sync_service.py` | `owner_fetcher.py` | (uses user_fetcher) | Low |
| User | `user_sync_service.py` | `user_fetcher.py` | `user_fetcher.py` | Low |
| Pipeline | `pipeline_sync_service.py` | `pipeline_fetcher.py` | `pipeline_fetcher.py` | Low |

### Already Completed (Deal/Opportunity)

- `DealSyncService.sync_incremental()` - Full implementation with change detection
- `DealSyncService._has_meaningful_changes()` - Field comparison logic
- `DealSyncService._update_deal_record_with_protection()` - Protects `deal_summary_data`
- HubSpot `fetch_deals()` - Supports `sync_type` and `response_results`
- Salesforce `fetch_opportunities()` - Supports `sync_type` and `response_results`
- Dispatcher routing for deals/opportunities

---

## Implementation Phases

### Phase 1: Contact & Company (High Priority)

These are the most frequently synced entities after deals and have the highest impact.

#### 1.1 Contact Entity

**Files to Modify:**

| File | Changes Required |
|------|------------------|
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Add `sync_type`, `response_results` to `fetch_contacts()` |
| `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` | Add `sync_type`, `response_results` to `fetch_contacts()` |
| `src/service/integrations/crm/entities/contact_sync_service.py` | Add `sync_incremental()`, `_has_meaningful_changes()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/contact_adapter.py` | Add `sync_type` routing |
| `src/workers/integrations_worker/adapters/crm_adapter.py` | Pass `sync_type` to contact sync |
| `src/workers/integrations_worker/services/dispatcher.py` | Route contacts with sync context |

**Key Fields for Change Detection (Contact):**

```python
def _has_meaningful_changes(self, existing: CRMContact, incoming: dict[str, Any]) -> bool:
    """Check if incoming contact data has meaningful changes."""
    properties = incoming.get("properties") or {}

    # Check source_modified_at
    incoming_modified = self._parse_date(incoming.get("updatedAt"))
    if incoming_modified and existing.source_modified_at:
        if incoming_modified > existing.source_modified_at:
            return True

    # Compare key fields
    checks = [
        (existing.first_name, properties.get("firstname")),
        (existing.last_name, properties.get("lastname")),
        (existing.email, properties.get("email")),
        (existing.phone, properties.get("phone")),
        (existing.job_title, properties.get("jobtitle")),
        (existing.owner_external_id, properties.get("hubspot_owner_id")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False
```

**Special Considerations:**
- Contact has company associations that need to be handled
- Existing `_upsert_contact_company_associations()` should be called for ADDED/UPDATED records

#### 1.2 Company Entity

**Files to Modify:**

| File | Changes Required |
|------|------------------|
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Add `sync_type`, `response_results` to `fetch_companies()` |
| `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` | Add `sync_type`, `response_results` to `fetch_accounts()` |
| `src/service/integrations/crm/entities/company_sync_service.py` | Add `sync_incremental()`, `_has_meaningful_changes()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/company_adapter.py` | Add `sync_type` routing |
| `src/workers/integrations_worker/adapters/crm_adapter.py` | Pass `sync_type` to company sync |
| `src/workers/integrations_worker/services/dispatcher.py` | Route companies with sync context |

**Key Fields for Change Detection (Company):**

```python
def _has_meaningful_changes(self, existing: CRMCompany, incoming: dict[str, Any]) -> bool:
    """Check if incoming company data has meaningful changes."""
    properties = incoming.get("properties") or {}

    # Check source_modified_at
    incoming_modified = self._parse_date(incoming.get("updatedAt"))
    if incoming_modified and existing.source_modified_at:
        if incoming_modified > existing.source_modified_at:
            return True

    # Compare key fields
    checks = [
        (existing.name, properties.get("name")),
        (existing.domain, properties.get("domain")),
        (existing.industry, properties.get("industry")),
        (existing.website, properties.get("website")),
        (existing.phone, properties.get("phone")),
        (existing.owner_external_id, properties.get("hubspot_owner_id")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False
```

---

### Phase 2: Deal Task (Medium Priority)

Activity records that are frequently synced with deals.

#### 2.1 Deal Task Entity

**Files to Modify:**

| File | Changes Required |
|------|------------------|
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Add `sync_type`, `response_results` to `fetch_deal_tasks()` |
| `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` | Add `sync_type`, `response_results` to `fetch_tasks()` |
| `src/service/integrations/crm/entities/deal_task_sync_service.py` | Add `sync_incremental()`, `_has_meaningful_changes()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/deal_task_adapter.py` | Add `sync_type` routing |
| `src/workers/integrations_worker/adapters/crm_adapter.py` | Pass `sync_type` to task sync |
| `src/workers/integrations_worker/services/dispatcher.py` | Route tasks with sync context |

**Key Fields for Change Detection (Deal Task):**

```python
def _has_meaningful_changes(self, existing: CRMDealTask, incoming: dict[str, Any]) -> bool:
    """Check if incoming task data has meaningful changes."""
    properties = incoming.get("properties") or {}

    # Check source_modified_at
    incoming_modified = self._parse_date(incoming.get("updatedAt"))
    if incoming_modified and existing.source_modified_at:
        if incoming_modified > existing.source_modified_at:
            return True

    # Compare key fields
    checks = [
        (existing.subject, properties.get("hs_task_subject") or properties.get("Subject")),
        (existing.status, properties.get("hs_task_status") or properties.get("Status")),
        (existing.priority, properties.get("hs_task_priority") or properties.get("Priority")),
        (existing.task_type, properties.get("hs_task_type") or properties.get("Type")),
        (existing.owner_external_id, properties.get("hubspot_owner_id") or properties.get("OwnerId")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False
```

**Special Considerations:**
- HubSpot uses `hs_task_*` properties
- Salesforce uses different field names (Subject, Status, etc.)
- Salesforce also has Cases which map to tasks

---

### Phase 3: Reference Entities (Low Priority)

These entities are relatively static and change infrequently. The performance gain is minimal, but consistency is valuable.

#### 3.1 Owner Entity

**Files to Modify:**

| File | Changes Required |
|------|------------------|
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Add `sync_type`, `response_results` to `fetch_owners()` |
| `src/service/integrations/crm/entities/owner_sync_service.py` | Add `sync_incremental()`, `_has_meaningful_changes()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/owner_adapter.py` | Add `sync_type` routing |

**Note:** HubSpot owners endpoint doesn't support filtering by update date, so incremental sync will rely on Nango's change detection.

**Key Fields for Change Detection (Owner):**

```python
def _has_meaningful_changes(self, existing: CRMUser, incoming: dict[str, Any]) -> bool:
    """Check if incoming owner data has meaningful changes."""
    checks = [
        (existing.email, incoming.get("email")),
        (existing.first_name, incoming.get("firstName")),
        (existing.last_name, incoming.get("lastName")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False
```

#### 3.2 User Entity

**Files to Modify:**

| File | Changes Required |
|------|------------------|
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Add `sync_type`, `response_results` to `fetch_users()` |
| `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` | Add `sync_type`, `response_results` to `fetch_users()` |
| `src/service/integrations/crm/entities/user_sync_service.py` | Add `sync_incremental()`, `_has_meaningful_changes()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/user_adapter.py` | Add `sync_type` routing |

**Key Fields for Change Detection (User):**

```python
def _has_meaningful_changes(self, existing: CRMUser, incoming: dict[str, Any]) -> bool:
    """Check if incoming user data has meaningful changes."""
    # Flexible field extraction (same pattern as sync service)
    email = incoming.get("email") or incoming.get("Email")
    first_name = incoming.get("firstName") or incoming.get("FirstName") or incoming.get("first_name")
    last_name = incoming.get("lastName") or incoming.get("LastName") or incoming.get("last_name")

    checks = [
        (existing.email, email),
        (existing.first_name, first_name),
        (existing.last_name, last_name),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False
```

#### 3.3 Pipeline Entity

**Files to Modify:**

| File | Changes Required |
|------|------------------|
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Add `sync_type`, `response_results` to `fetch_pipelines()` |
| `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` | Add `sync_type`, `response_results` to `fetch_pipelines()` |
| `src/service/integrations/crm/entities/pipeline_sync_service.py` | Add `sync_incremental()`, `_has_meaningful_changes()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/pipeline_adapter.py` | Add `sync_type` routing |

**Note:**
- HubSpot pipelines API doesn't support filtering by date
- Salesforce OpportunityStage is fairly static
- This is lowest priority due to infrequent changes

**Key Fields for Change Detection (Pipeline):**

```python
def _has_meaningful_changes(self, existing: CRMPipeline, incoming: dict[str, Any]) -> bool:
    """Check if incoming pipeline data has meaningful changes."""
    checks = [
        (existing.label, incoming.get("label")),
        (existing.display_order, incoming.get("displayOrder")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False
```

---

## Dispatcher Updates

The dispatcher needs to be updated to pass sync context to all entity types, not just deals.

**File:** `src/workers/integrations_worker/services/dispatcher.py`

```python
# In _execute_category_sync method
# Extend the sync_type/response_results passing to other entity types

if request.category == "crm":
    entity_types_with_incremental = {
        "deals", "opportunities",  # Already done
        "contacts",
        "companies", "accounts",
        "tasks", "deal_tasks",
        "owners",
        "users",
        "pipelines",
    }

    if sync_data_type in entity_types_with_incremental:
        fetch_kwargs["sync_type"] = request.sync_type
        fetch_kwargs["response_results"] = request.response_results
```

---

## CRM Adapter Updates

**File:** `src/workers/integrations_worker/adapters/crm_adapter.py`

The `sync_crm_data()` method needs to pass `sync_type` to all entity sync methods:

```python
async def sync_crm_data(
    self,
    *,
    sync_type: str | None = None,  # Add parameter
    # ... existing parameters
) -> dict[str, Any]:
    # Pass sync_type to each entity sync call
    if "contacts" in data and data["contacts"]:
        contact_result = await self._sync_contacts(
            db=db,
            contacts=data["contacts"],
            sync_type=sync_type,  # Pass through
        )

    if "companies" in data and data["companies"]:
        company_result = await self._sync_companies(
            db=db,
            companies=data["companies"],
            sync_type=sync_type,  # Pass through
        )

    # ... similar for other entities
```

---

## Effort Estimation

### Lines of Code by Phase

| Phase | Entity | Fetcher Changes | Sync Service | Adapter | Tests | Total |
|-------|--------|-----------------|--------------|---------|-------|-------|
| 1 | Contact | ~40 | ~100 | ~15 | ~100 | ~255 |
| 1 | Company | ~40 | ~80 | ~15 | ~80 | ~215 |
| 2 | Deal Task | ~40 | ~100 | ~15 | ~100 | ~255 |
| 3 | Owner | ~20 | ~60 | ~10 | ~60 | ~150 |
| 3 | User | ~40 | ~70 | ~10 | ~70 | ~190 |
| 3 | Pipeline | ~40 | ~80 | ~10 | ~80 | ~210 |
| - | Dispatcher/CRM Adapter | - | - | ~50 | - | ~50 |
| **Total** | | **~220** | **~490** | **~125** | **~490** | **~1,325** |

### Files to Modify Summary

| Category | Files | Description |
|----------|-------|-------------|
| HubSpot Fetchers | 6 | Add sync_type/response_results to fetch functions |
| Salesforce Fetchers | 6 | Add sync_type/response_results to fetch functions |
| Sync Services | 6 | Add sync_incremental() and _has_meaningful_changes() |
| Entity Adapters | 6 | Add sync_type routing logic |
| CRM Adapter | 1 | Pass sync_type to all entity methods |
| Dispatcher | 1 | Extend sync context to all entity types |
| Test Files | 6 | Unit tests for each entity's incremental logic |
| **Total** | **32** | |

---

## Testing Strategy

### Unit Tests per Entity

Each entity should have tests covering:

1. **Change Detection Tests**
   - Detects name/primary field changes
   - Detects key field changes (email, phone, etc.)
   - Detects source_modified_at changes
   - Returns false when no changes
   - Handles None values gracefully

2. **Routing Tests**
   - ADDED action creates new records
   - UPDATED action updates existing records
   - DELETED action soft-deletes records
   - UNCHANGED action is skipped

3. **Integration Tests**
   - Mixed action scenario
   - Action counts correlation with webhook results

### Test File Locations

```
tests/
├── service/
│   └── integrations/
│       └── crm/
│           └── entities/
│               ├── test_contact_sync_service.py
│               ├── test_company_sync_service.py
│               ├── test_deal_task_sync_service.py
│               ├── test_owner_sync_service.py
│               ├── test_user_sync_service.py
│               └── test_pipeline_sync_service.py
└── workers/
    └── integrations_worker/
        └── adapters/
            └── crm/
                └── entity_adapters/
                    └── test_entity_adapters_routing.py
```

---

## Implementation Order Recommendation

### Recommended Order (by Impact and Complexity)

1. **Contact** - High sync volume, straightforward fields
2. **Company** - High sync volume, simple structure
3. **Deal Task** - Medium volume, multi-provider complexity
4. **User** - Low volume but consistent pattern
5. **Owner** - Very low volume, simple
6. **Pipeline** - Lowest priority, static data

### Dependencies

```
Dispatcher Update (once)
       │
       ├── Contact ──────────────────┐
       │                              │
       ├── Company ──────────────────┤
       │                              │
       ├── Deal Task ────────────────┤
       │                              │
       ├── Owner ────────────────────┼──> CRM Adapter Update (once)
       │                              │
       ├── User ─────────────────────┤
       │                              │
       └── Pipeline ─────────────────┘
```

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| HubSpot API limitations (no date filtering for owners/pipelines) | Low | Rely on Nango's change detection |
| Multi-provider field name differences | Medium | Reuse existing field extraction patterns |
| Test complexity without Supabase | Low | Use standalone implementations like Deal tests |
| Breaking existing sync behavior | High | Add feature flag for gradual rollout |

---

## Success Criteria

1. All 6 entities support incremental sync with action-based routing
2. No regression in existing full sync functionality
3. Webhook `response_results` counts match processed record counts
4. Test coverage > 80% for incremental logic
5. Logging provides visibility into sync action distribution

---

## Open Questions

1. **Feature flag:** Should we add a feature flag to gradually enable incremental sync per entity?
2. **Metrics:** Should we track incremental vs full sync performance in `IntegrationSyncHistory`?
3. **Batch size:** Should different entities have different batch sizes for incremental processing?

---

## Appendix: Sync Metadata Utilities (Already Implemented)

The following utilities from `src/workers/integrations_worker/utils/sync_metadata.py` are ready for use:

```python
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,           # Enum: ADDED, UPDATED, DELETED, UNCHANGED
    SyncActionCounts,     # Tracks counts per action type
    SyncContext,          # Dataclass with sync_type and response_results
    classify_records,     # Adds _sync_metadata to records
    extract_sync_action,  # Gets SyncAction from record
    determine_sync_action,  # Determines action for single record
)
```

These utilities handle:
- Record classification based on archived status and context
- Adding `_sync_metadata.sync_action` to each record
- Counting actions for reporting
- Extracting sync action from processed records
