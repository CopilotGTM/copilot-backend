# CRM Sync Tasks: Three-Tier Architecture

> **Version**: 1.0
> **Last Updated**: January 11, 2026
> **Status**: Active Implementation Document

---

## Overview

This document explains the **CRM data synchronization** implementation using a **three-tier Celery task architecture**. Unlike calendar or email sync (which sync all data types together), CRM sync uses **per-entity task hierarchies** for granular control over sync frequency and isolation.

**Supported Providers**:
- HubSpot CRM
- Salesforce CRM

**Synced Entities**:
| Entity | Task Prefix | Incremental Support |
|--------|-------------|---------------------|
| Deals | `sync_crm_deals_*` | Yes |
| Contacts | `sync_crm_contacts_*` | Yes |
| Companies | `sync_crm_companies_*` | Yes |
| Activities (Tasks/Notes) | `sync_crm_activities_*` | Yes |
| Pipelines | `sync_crm_pipelines_*` | No (reference data) |
| Users | `sync_crm_users_*` | No (reference data) |

---

## Architecture Diagram

### Per-Entity Three-Tier Pattern

Each CRM entity has its own independent three-tier task hierarchy:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CELERY BEAT SCHEDULER                             │
│                                                                             │
│  Staggered Schedules (to avoid API rate limits):                           │
│  • Deals:      Every 3 minutes (most critical)                             │
│  • Contacts:   Every 3 hours at :20                                        │
│  • Companies:  Every 3 hours at :30                                        │
│  • Activities: Every 1 hour                                                │
│  • Pipelines:  Every 6 hours at :10 (reference data)                       │
│  • Users:      Every 6 hours at :00 (reference data)                       │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
            Each entity has its own Tier 1 task
                                   │
     ┌─────────────────────────────┼─────────────────────────────┐
     │                             │                             │
     ▼                             ▼                             ▼
┌─────────────┐           ┌─────────────┐           ┌─────────────┐
│ sync_crm_   │           │ sync_crm_   │           │ sync_crm_   │
│ deals_      │           │ contacts_   │           │ companies_  │
│ all_orgs    │           │ all_orgs    │           │ all_orgs    │
└──────┬──────┘           └──────┬──────┘           └──────┬──────┘
       │                         │                         │
       ▼                         ▼                         ▼
   (Tier 2)                  (Tier 2)                  (Tier 2)
```

### Detailed Flow for One Entity (Deals)

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: sync_crm_deals_all_orgs_task                                       │
│  File: src/workers/integrations_worker/tasks/crm/deals/all_orgs_task.py     │
│  Task Name: integrations.sync_crm_deals_all_orgs                            │
│                                                                             │
│  Actions:                                                                   │
│  1. Query master DB for all APPROVED organizations                          │
│  2. For each org: dispatch sync_crm_deals_org_task via apply_async()        │
│  3. Return summary with task IDs                                            │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=False, max_retries=0 (coordinator doesn't retry)                    │
│  • Fire-and-forget dispatch to Tier 2                                       │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
              Dispatches one Tier 2 task per organization (async)
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        ▼                          ▼                          ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ sync_crm_deals_   │  │ sync_crm_deals_   │  │ sync_crm_deals_   │
│ org_task          │  │ org_task          │  │ org_task          │
│ (Org A)           │  │ (Org B)           │  │ (Org C)           │
└─────────┬─────────┘  └─────────┬─────────┘  └─────────┬─────────┘
          │                      │                      │
          ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 2: sync_crm_deals_org_task                                            │
│  File: src/workers/integrations_worker/tasks/crm/deals/org_task.py          │
│  Task Name: integrations.sync_crm_deals_org                                 │
│                                                                             │
│  Actions:                                                                   │
│  1. Query org DB for active CRM integrations (HubSpot/Salesforce)           │
│  2. For each integration (inline, NOT async dispatch):                      │
│     a. Get last_sync_timestamp from connection_metadata                     │
│     b. Fetch records via provider fetcher (with timestamp filter)           │
│     c. Call sync service (sync() or sync_incremental())                     │
│     d. Update connection_metadata with new timestamp                        │
│  3. Return org-level aggregated results                                     │
│                                                                             │
│  Characteristics:                                                           │
│  • bind=True, max_retries=2, retry_delay=120s                               │
│  • Inline processing (for loop), NOT async dispatch to Tier 3               │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                    (Manual triggers only)
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 3: sync_crm_deals_integration_task                                    │
│  File: src/workers/integrations_worker/tasks/crm/deals/integration_task.py  │
│  Task Name: integrations.sync_crm_deals_integration                         │
│                                                                             │
│  USE CASES (NOT used by scheduled sync):                                    │
│  • User clicks "Refresh" button on frontend                                 │
│  • Admin manually triggers sync for debugging                               │
│  • API endpoint for on-demand sync                                          │
│                                                                             │
│  Parameters:                                                                │
│  • organisation_id (required)                                               │
│  • integration_id (required)                                                │
│  • force_full_sync (optional) - Ignores last_sync_at if True               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Task Naming Convention

```text
Pattern: integrations.sync_crm_{entity}_{scope}

Tier 1 (Global):       integrations.sync_crm_{entity}_all_orgs
Tier 2 (Organization): integrations.sync_crm_{entity}_org
Tier 3 (Integration):  integrations.sync_crm_{entity}_integration

Examples:
├── Deals:
│   ├── integrations.sync_crm_deals_all_orgs
│   ├── integrations.sync_crm_deals_org
│   └── integrations.sync_crm_deals_integration
│
├── Contacts:
│   ├── integrations.sync_crm_contacts_all_orgs
│   ├── integrations.sync_crm_contacts_org
│   └── integrations.sync_crm_contacts_integration
│
├── Companies:
│   ├── integrations.sync_crm_companies_all_orgs
│   ├── integrations.sync_crm_companies_org
│   └── integrations.sync_crm_companies_integration
│
├── Activities:
│   ├── integrations.sync_crm_activities_all_orgs
│   ├── integrations.sync_crm_activities_org
│   └── integrations.sync_crm_activities_integration
│
├── Pipelines:
│   ├── integrations.sync_crm_pipelines_all_orgs
│   ├── integrations.sync_crm_pipelines_org
│   └── integrations.sync_crm_pipelines_integration
│
└── Users:
    ├── integrations.sync_crm_users_all_orgs
    ├── integrations.sync_crm_users_org
    └── integrations.sync_crm_users_integration
```

---

## File Structure

```text
src/workers/integrations_worker/tasks/crm/
├── __init__.py                    # Module exports for all CRM tasks
├── utils.py                       # Shared utilities (timestamp helpers)
│
├── deals/
│   ├── __init__.py
│   ├── all_orgs_task.py           # Tier 1
│   ├── org_task.py                # Tier 2
│   └── integration_task.py        # Tier 3
│
├── contacts/
│   ├── __init__.py
│   ├── all_orgs_task.py           # Tier 1
│   ├── org_task.py                # Tier 2
│   └── integration_task.py        # Tier 3
│
├── companies/
│   ├── __init__.py
│   ├── all_orgs_task.py           # Tier 1
│   ├── org_task.py                # Tier 2
│   └── integration_task.py        # Tier 3
│
├── activities/
│   ├── __init__.py
│   ├── all_orgs_task.py           # Tier 1
│   ├── org_task.py                # Tier 2
│   └── integration_task.py        # Tier 3
│
├── pipelines/
│   ├── __init__.py
│   ├── all_orgs_task.py           # Tier 1
│   ├── org_task.py                # Tier 2
│   └── integration_task.py        # Tier 3
│
└── users/
    ├── __init__.py
    ├── all_orgs_task.py           # Tier 1
    ├── org_task.py                # Tier 2
    └── integration_task.py        # Tier 3
```

---

## Incremental Sync Implementation

### Overview

CRM sync uses **timestamp-based incremental sync** (not delta links like Microsoft Graph). Each entity stores its own `last_sync_timestamp` in the integration's `connection_metadata`.

### Timestamp Storage

```python
# In KnowledgeIntegration.connection_metadata
{
    "last_sync_timestamps": {
        "deals": "2026-01-11T14:30:00",
        "contacts": "2026-01-11T14:25:00",
        "companies": "2026-01-11T14:20:00",
        "activities": "2026-01-11T14:15:00",
        "pipelines": "2026-01-11T12:00:00",
        "users": "2026-01-11T12:00:00"
    }
}
```

### Sync Flow

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  INITIAL SYNC (last_sync_timestamp = None)                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  1. Tier 2 task checks: last_sync_at = get_last_sync_timestamp("deals")     │
│  2. is_initial = True (no timestamp found)                                  │
│  3. Fetch ALL records from CRM provider (no filter)                         │
│  4. Call sync_service.sync() for full upsert                                │
│  5. Store timestamp: update_last_sync_timestamp("deals", sync_start)        │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  INCREMENTAL SYNC (last_sync_timestamp exists)                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  1. Tier 2 task checks: last_sync_at = get_last_sync_timestamp("deals")     │
│  2. is_initial = False (timestamp found: "2026-01-11T14:30:00")             │
│  3. Fetch records modified SINCE last_sync_at from CRM provider             │
│     • HubSpot: POST /search with hs_lastmodifieddate >= timestamp           │
│     • Salesforce: SOQL WHERE LastModifiedDate >= timestamp                  │
│  4. Call sync_service.sync_incremental() for delta upsert                   │
│  5. Update timestamp: update_last_sync_timestamp("deals", sync_start)       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Utility Functions

```python
# src/workers/integrations_worker/tasks/crm/utils.py

def get_last_sync_timestamp(integration, entity_type: str) -> datetime | None:
    """Get last sync timestamp for a specific entity type."""
    metadata = integration.connection_metadata or {}
    timestamps = metadata.get("last_sync_timestamps", {})
    ts_str = timestamps.get(entity_type)
    return datetime.fromisoformat(ts_str) if ts_str else None

def update_last_sync_timestamp(integration, entity_type: str, timestamp: datetime):
    """Update last sync timestamp for a specific entity type."""
    if integration.connection_metadata is None:
        integration.connection_metadata = {}
    if "last_sync_timestamps" not in integration.connection_metadata:
        integration.connection_metadata["last_sync_timestamps"] = {}
    integration.connection_metadata["last_sync_timestamps"][entity_type] = timestamp.isoformat()
```

---

## Provider-Specific Details

### HubSpot

- **Incremental Filter**: `hs_lastmodifieddate` (Unix milliseconds)
- **API Pattern**: POST to `/crm/v3/objects/{entity}/search`
- **Full Documentation**: [HubSpot CRM Sync Architecture](./hubspot-crm-sync.md)

### Salesforce

- **Incremental Filter**: `LastModifiedDate` (ISO 8601)
- **API Pattern**: SOQL query with WHERE clause
- **Full Documentation**: (Coming soon)

---

## Entity Dependencies

CRM entities have cross-references that are resolved by **post-sync enrichment**, not sync order:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  ENTITY RELATIONSHIPS                                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Deals ──────► Pipelines (stage mapping)                                    │
│        ├────► Contacts (participants)                                       │
│        ├────► Companies (account association)                               │
│        └────► Users (owner assignment)                                      │
│                                                                             │
│  Contacts ───► Companies (company association)                              │
│           └──► Users (owner assignment)                                     │
│                                                                             │
│  Companies ──► Users (owner assignment)                                     │
│                                                                             │
│  Activities ─► Deals (linked activities)                                    │
│            ├─► Contacts (related contacts)                                  │
│            └─► Users (owner assignment)                                     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Enrichment Services

After sync, enrichment services populate denormalized fields:

| Trigger | Service | Purpose |
|---------|---------|---------|
| After Users sync | `DealEnrichmentService` | Populate `owner_name`, `owner_email` |
| After Contacts sync | `DealEnrichmentService` | Populate participant names |
| After Companies sync | `DealEnrichmentService` | Populate company names |
| After Pipelines sync | `DealSyncService` | Populate `pipeline_name`, `stage_name` |

---

## Celery Configuration

### Task Routes (`config.py`)

```python
self.task_routes = {
    # CRM Deals sync tasks
    "integrations.sync_crm_deals_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_deals_org": {"queue": "integrations"},
    "integrations.sync_crm_deals_integration": {"queue": "integrations_priority"},

    # CRM Contacts sync tasks
    "integrations.sync_crm_contacts_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_contacts_org": {"queue": "integrations"},
    "integrations.sync_crm_contacts_integration": {"queue": "integrations_priority"},

    # CRM Companies sync tasks
    "integrations.sync_crm_companies_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_companies_org": {"queue": "integrations"},
    "integrations.sync_crm_companies_integration": {"queue": "integrations_priority"},

    # CRM Activities sync tasks
    "integrations.sync_crm_activities_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_activities_org": {"queue": "integrations"},
    "integrations.sync_crm_activities_integration": {"queue": "integrations_priority"},

    # CRM Pipelines sync tasks
    "integrations.sync_crm_pipelines_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_pipelines_org": {"queue": "integrations"},
    "integrations.sync_crm_pipelines_integration": {"queue": "integrations_priority"},

    # CRM Users sync tasks
    "integrations.sync_crm_users_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_users_org": {"queue": "integrations"},
    "integrations.sync_crm_users_integration": {"queue": "integrations_priority"},
}
```

### Recommended Beat Schedule (`config.py`)

```python
self.beat_schedule = {
    # CRM Users sync (every 3 hours at :00) - reference data, sync first
    "sync-crm-users-all-orgs": {
        "task": "integrations.sync_crm_users_all_orgs",
        "schedule": crontab(minute=0, hour="*/3"),
    },

    # CRM Pipelines sync (every 3 hours at :10) - reference data
    "sync-crm-pipelines-all-orgs": {
        "task": "integrations.sync_crm_pipelines_all_orgs",
        "schedule": crontab(minute=10, hour="*/3"),
    },

    # CRM Contacts sync (every 3 hours at :20)
    "sync-crm-contacts-all-orgs": {
        "task": "integrations.sync_crm_contacts_all_orgs",
        "schedule": crontab(minute=20, hour="*/3"),
    },

    # CRM Companies sync (every 3 hours at :30)
    "sync-crm-companies-all-orgs": {
        "task": "integrations.sync_crm_companies_all_orgs",
        "schedule": crontab(minute=30, hour="*/3"),
    },

    # CRM Deals sync (every 3-5 minutes) - most critical
    "sync-crm-deals-all-orgs": {
        "task": "integrations.sync_crm_deals_all_orgs",
        "schedule": crontab(minute="*/3"),
    },

    # CRM Activities sync (every 1 hour)
    "sync-crm-activities-all-orgs": {
        "task": "integrations.sync_crm_activities_all_orgs",
        "schedule": crontab(minute=0),
    },
}
```

---

## Result Formats

### Tier 1 Result

```python
{
    "status": "dispatched",
    "started_at": "2026-01-11T10:00:00+00:00",
    "completed_at": "2026-01-11T10:00:02+00:00",
    "organisations_dispatched": 15
}
```

### Tier 2 Result

```python
{
    "status": "completed",  # or "partial", "failed"
    "organisation_id": "uuid-1",
    "started_at": "2026-01-11T10:00:05+00:00",
    "completed_at": "2026-01-11T10:01:30+00:00",
    "summary": {
        "total_integrations": 2,
        "successful": 2,
        "failed": 0,
        "total_records_synced": 150
    },
    "integration_results": [
        {
            "integration_id": "int-1",
            "integration_type": "hubspot",
            "status": "success",
            "records_fetched": 75,
            "records_synced": 75,
            "is_initial": False,
            "metrics": {"deals": 75}
        },
        {
            "integration_id": "int-2",
            "integration_type": "salesforce",
            "status": "success",
            "records_fetched": 75,
            "records_synced": 75,
            "is_initial": False,
            "metrics": {"deals": 75}
        }
    ]
}
```

### Tier 3 Result

```python
{
    "status": "success",
    "organisation_id": "uuid-1",
    "integration_id": "int-1",
    "integration_type": "hubspot",
    "started_at": "2026-01-11T10:00:00+00:00",
    "completed_at": "2026-01-11T10:00:45+00:00",
    "records_fetched": 75,
    "records_synced": 75,
    "is_initial": False
}
```

---

## Sync Services

Each CRM entity has a dedicated sync service:

| Entity | Service | Location |
|--------|---------|----------|
| Deals | `DealSyncService` | `src/service/integrations/crm/entities/deal_sync_service.py` |
| Contacts | `ContactSyncService` | `src/service/integrations/crm/entities/contact_sync_service.py` |
| Companies | `CompanySyncService` | `src/service/integrations/crm/entities/company_sync_service.py` |
| Activities | `DealTaskSyncService` | `src/service/integrations/crm/entities/deal_task_sync_service.py` |
| Pipelines | `PipelineSyncService` | `src/service/integrations/crm/entities/pipeline_sync_service.py` |
| Users | `UserSyncService` | `src/service/integrations/crm/entities/user_sync_service.py` |

### Service Methods

Each service provides:

| Method | Purpose |
|--------|---------|
| `sync()` | Full sync - upsert all records |
| `sync_incremental()` | Incremental sync - upsert changed records, handle deletes |

---

## Provider Fetchers

### HubSpot Fetchers

| Entity | Fetcher | Location |
|--------|---------|----------|
| Deals | `HubSpotDealFetcher` | `providers/crm/hubspot/fetchers/deal_fetcher.py` |
| Contacts | `HubSpotContactFetcher` | `providers/crm/hubspot/fetchers/contact_fetcher.py` |
| Companies | `HubSpotCompanyFetcher` | `providers/crm/hubspot/fetchers/company_fetcher.py` |
| Activities | `HubSpotDealTaskFetcher` | `providers/crm/hubspot/fetchers/deal_task_fetcher.py` |
| Pipelines | `HubSpotPipelineFetcher` | `providers/crm/hubspot/fetchers/pipeline_fetcher.py` |
| Users | `HubSpotOwnerFetcher` | `providers/crm/hubspot/fetchers/owner_fetcher.py` |

### Salesforce Fetchers

| Entity | Fetcher | Location |
|--------|---------|----------|
| Deals | `SalesforceOpportunityFetcher` | `providers/crm/salesforce/fetchers/opportunity_fetcher.py` |
| Contacts | `SalesforceContactFetcher` | `providers/crm/salesforce/fetchers/contact_fetcher.py` |
| Companies | `SalesforceAccountFetcher` | `providers/crm/salesforce/fetchers/account_fetcher.py` |
| Activities | `SalesforceTaskFetcher` | `providers/crm/salesforce/fetchers/task_fetcher.py` |
| Pipelines | `SalesforcePipelineFetcher` | `providers/crm/salesforce/fetchers/pipeline_fetcher.py` |
| Users | `SalesforceUserFetcher` | `providers/crm/salesforce/fetchers/user_fetcher.py` |

---

## Usage Examples

```python
# Tier 1: Sync all organizations (usually via Beat, but can be manual)
from src.workers.integrations_worker.tasks.crm.deals import sync_crm_deals_all_orgs_task
result = sync_crm_deals_all_orgs_task.delay()

# Tier 2: Sync specific organization
from src.workers.integrations_worker.tasks.crm.deals import sync_crm_deals_org_task
result = sync_crm_deals_org_task.delay(organisation_id="your-org-uuid")

# Tier 3: Sync specific integration (manual refresh)
from src.workers.integrations_worker.tasks.crm.deals import sync_crm_deals_integration_task
result = sync_crm_deals_integration_task.delay(
    organisation_id="your-org-uuid",
    integration_id="your-integration-uuid"
)
```

---

## Monitoring & Debugging

### Key Log Messages

```text
# Tier 1
"Starting CRM {entity} sync for all organizations (Tier 1)"
"Dispatching CRM {entity} sync to {n} organization(s)"

# Tier 2
"Starting CRM {entity} sync for organization (Tier 2): {org_id}"
"Syncing {entity}: org={org_id} integration={int_id} provider={provider} is_initial={bool}"
"Fetched {n} {entity} for integration {int_id}"

# Incremental
"Incremental sync: last_sync_at={timestamp}"
"Full sync: no previous timestamp found"
```

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| All records fetched on incremental | Wrong timestamp format | Ensure Unix ms for HubSpot |
| Filter ignored | Using GET instead of POST | Use POST `/search` endpoint |
| Timezone errors | Mixing aware/naive datetime | Use `datetime.utcnow()` for DB |

---

## File Reference

| Component | File Path |
|-----------|-----------|
| **CRM Tasks (All)** | `src/workers/integrations_worker/tasks/crm/` |
| **Shared Utilities** | `src/workers/integrations_worker/tasks/crm/utils.py` |
| **HubSpot Fetchers** | `src/workers/integrations_worker/providers/crm/hubspot/fetchers/` |
| **Salesforce Fetchers** | `src/workers/integrations_worker/providers/crm/salesforce/fetchers/` |
| **Sync Services** | `src/service/integrations/crm/entities/` |
| **Celery Config** | `src/workers/integrations_worker/config.py` |

---

## Related Documentation

- [HubSpot CRM Sync Architecture](./hubspot-crm-sync.md) - HubSpot-specific patterns and API details
- [Scheduled Sync Architecture Guide](./scheduled-sync-architecture.md) - Three-tier pattern overview
- [CRM Integration Flow Charts](../crm-integration-flow.md) - Data flow diagrams
- [CRM Integration Architecture](../crm-integration-architecture.md) - System architecture
