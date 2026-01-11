# HubSpot CRM Sync Architecture

> **Version**: 1.1
> **Last Updated**: January 12, 2026
> **Status**: Production

---

## Table of Contents

1. [Overview](#1-overview)
2. [Three-Tier Task Architecture](#2-three-tier-task-architecture)
3. [Initial Sync on Connection](#3-initial-sync-on-connection)
4. [Enrichment](#4-enrichment)
5. [Entity Types](#5-entity-types)
6. [Incremental Sync Implementation](#6-incremental-sync-implementation)
7. [HubSpot API Patterns](#7-hubspot-api-patterns)
8. [Code Structure](#8-code-structure)
9. [Configuration](#9-configuration)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Overview

### Purpose

This document describes the HubSpot CRM sync architecture, including the three-tier task pattern, incremental sync implementation, and HubSpot-specific API patterns.

### Supported Entities

| Entity | Sync Type | Endpoint | Incremental Support |
|--------|-----------|----------|---------------------|
| Contacts | Incremental | POST `/crm/v3/objects/contacts/search` | Yes |
| Companies | Incremental | POST `/crm/v3/objects/companies/search` | Yes |
| Deals | Incremental | POST `/crm/v3/objects/deals/search` | Yes |
| Activities (Tasks) | Incremental | POST `/crm/v3/objects/tasks/search` | Yes |
| Notes | Incremental | POST `/crm/v3/objects/notes/search` | Yes |
| Pipelines | Full Sync | GET `/crm/v3/pipelines/deals` | No (reference data) |
| Users | Full Sync | GET `/crm/v3/owners` | No (reference data) |

---

## 2. Three-Tier Task Architecture

All CRM sync tasks follow a three-tier pattern for scalability and isolation:

```
Celery Beat (scheduled)
    │
    ▼
┌─────────────────────────────────────────┐
│  Tier 1: sync_crm_{entity}_all_orgs     │  ← Coordinator
│  - Queries all approved organizations   │
│  - Dispatches Tier 2 tasks              │
└─────────────────────────────────────────┘
    │
    ▼ (one task per org)
┌─────────────────────────────────────────┐
│  Tier 2: sync_crm_{entity}_org          │  ← Per-Organization
│  - Syncs all integrations for one org   │
│  - Calls sync service for each          │
└─────────────────────────────────────────┘
    │
    ▼ (inline, not separate task)
┌─────────────────────────────────────────┐
│  Tier 3: sync_crm_{entity}_integration  │  ← Manual Trigger
│  - For on-demand/manual refresh         │
│  - Single integration sync              │
└─────────────────────────────────────────┘
```

### Task Naming Convention

```
integrations.sync_crm_{entity}_all_orgs      # Tier 1
integrations.sync_crm_{entity}_org           # Tier 2
integrations.sync_crm_{entity}_integration   # Tier 3
```

### Task Files Location

```
src/workers/integrations_worker/tasks/crm/
├── __init__.py
├── utils.py                    # Shared utilities
├── deals/
│   ├── all_orgs_task.py       # Tier 1
│   ├── org_task.py            # Tier 2
│   └── integration_task.py    # Tier 3
├── contacts/
│   └── ...
├── companies/
│   └── ...
├── activities/
│   └── ...
├── pipelines/
│   └── ...
└── users/
    └── ...
```

---

## 3. Initial Sync on Connection

When a user connects HubSpot via OAuth, the system triggers an immediate initial sync using Celery chord pattern to ensure all data is fetched and enriched correctly.

### 3.1 Trigger Flow

```
User completes OAuth
        │
        ▼
┌─────────────────────────────────────────┐
│  Nango Webhook: AUTH_SUCCESS            │
│  (nango_webhook_service.py)             │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│  _trigger_crm_initial_sync()            │
│  - Dispatches Celery chord              │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│  Celery Group (parallel)                │
│  - sync_crm_companies_org (skip_enrich) │
│  - sync_crm_contacts_org (skip_enrich)  │
│  - sync_crm_deals_org (skip_enrich)     │
│  - sync_crm_users_org (skip_enrich)     │
│  - sync_crm_pipelines_org               │
│  - sync_crm_activities_org (skip_enrich)│
└─────────────────────────────────────────┘
        │
        ▼ (only after ALL syncs complete)
┌─────────────────────────────────────────┐
│  enrich_crm_after_initial_sync_task     │
│  - Runs CRMEnrichmentOrchestrator       │
│  - Enriches all entities                │
└─────────────────────────────────────────┘
```

### 3.2 Why Celery Chord?

**Problem**: If sync tasks and enrichment run independently:
- Enrichment might run before syncs complete
- Missing data = incomplete enrichment (no owner_name, company_name, etc.)

**Solution**: Celery chord guarantees callback runs ONLY after all group tasks complete:

```python
from celery import chord, group

sync_group = group(
    sync_crm_companies_org_task.s(org_id, skip_enrichment=True),
    sync_crm_contacts_org_task.s(org_id, skip_enrichment=True),
    sync_crm_deals_org_task.s(org_id, skip_enrichment=True),
    sync_crm_users_org_task.s(org_id, skip_enrichment=True),
    sync_crm_pipelines_org_task.s(org_id),
    sync_crm_activities_org_task.s(org_id, skip_enrichment=True),
)

# .si() = immutable signature (ignores group results)
chord(sync_group)(
    enrich_crm_after_initial_sync_task.si(org_id, integration_id)
)
```

### 3.3 skip_enrichment Flag

During initial sync, each org_task receives `skip_enrichment=True`:
- Prevents inline enrichment after each entity sync
- Avoids enrichment collisions when tasks run in parallel
- Single enrichment pass runs after ALL syncs complete

---

## 4. Enrichment

After sync completes, enrichment populates denormalized fields for efficient querying.

### 4.1 Enrichment Architecture

```
┌─────────────────────────────────────────┐
│     CRMEnrichmentOrchestrator           │
│     (Organization-level coordinator)    │
└───────────────┬─────────────────────────┘
                │
    ┌───────────┼───────────┬───────────┐
    ▼           ▼           ▼           ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│ Deal   │ │ Task   │ │Contact │ │Company │
│Enricher│ │Enricher│ │Enricher│ │Enricher│
└────────┘ └────────┘ └────────┘ └────────┘
```

### 4.2 DealEnrichmentService

Populates deal fields from related entities:

| Source Entity | Fields Populated on Deal |
|---------------|--------------------------|
| CRMUser (owner) | `owner_id`, `owner_name`, `owner_email` |
| CRMContact | `primary_contact_name`, `primary_contact_email` |
| CRMCompany | `company_name`, `company_domain` |
| CRMPipeline | `pipeline_id`, `pipeline_name`, `pipeline_label` |
| CRMDealStage | `stage_id`, `stage_name`, `stage_label` |

Also enriches `CRMDealParticipant` records with `related_name`, `related_email`.

### 4.3 TaskEnrichmentService

Populates activity/task fields:

| Source Entity | Fields Populated on CRMDealTask |
|---------------|--------------------------------|
| CRMUser | `owner_id`, `owner_name`, `owner_email` |
| CRMDeal | `deal_id` (from `deal_external_id`) |
| CRMContact | `contact_id` (from `contact_external_id`) |
| CRMCompany | `company_id` (from `company_external_id`) |

### 4.4 When Enrichment Runs

| Trigger | Enrichment Method |
|---------|-------------------|
| After deals sync | `enrich_after_deals_sync()` |
| After contacts sync | `enrich_after_contacts_sync()` |
| After companies sync | `enrich_after_companies_sync()` |
| After users sync | `enrich_after_users_sync()` |
| After activities sync | `enrich_after_activities_sync()` |
| Initial sync complete | `enrich_all()` |

### 4.5 Enrichment Files

```
src/service/integrations/crm/enrichment/
├── __init__.py
├── orchestrator.py              # CRMEnrichmentOrchestrator
├── deal_enrichment_service.py   # Deals + participants
├── task_enrichment_service.py   # Activities/tasks
├── contact_enrichment_service.py
├── company_enrichment_service.py
└── owner_enrichment_service.py
```

---

## 5. Entity Types

### 5.1 Incremental Entities

These entities support incremental sync using `hs_lastmodifieddate` filter:

| Entity | Filter Property | Timestamp Format |
|--------|-----------------|------------------|
| Contacts | `hs_lastmodifieddate` | Unix ms |
| Companies | `hs_lastmodifieddate` | Unix ms |
| Deals | `hs_lastmodifieddate` | Unix ms |
| Activities | `hs_lastmodifieddate` | Unix ms |
| Notes | `hs_lastmodifieddate` | Unix ms |

### 5.2 Reference Data Entities

These entities are always fetched fully (no incremental support):

| Entity | Reason |
|--------|--------|
| Pipelines | HubSpot API doesn't support date filtering |
| Users/Owners | HubSpot API doesn't support date filtering |

---

## 6. Incremental Sync Implementation

### 6.1 Timestamp Storage

Last sync timestamps are stored per-entity in `connection_metadata`:

```python
# In KnowledgeIntegration.connection_metadata
{
    "last_sync_timestamps": {
        "contacts": "2026-01-11T14:30:00",
        "deals": "2026-01-11T14:25:00",
        "companies": "2026-01-11T14:20:00",
        "activities": "2026-01-11T14:15:00"
    }
}
```

### 6.2 Utility Functions

```python
# src/workers/integrations_worker/tasks/crm/utils.py

def get_last_sync_timestamp(integration, entity_type: str) -> datetime | None:
    """Get last sync timestamp for an entity type."""

def update_last_sync_timestamp(integration, entity_type: str, timestamp: datetime):
    """Update last sync timestamp for an entity type."""
```

### 6.3 Sync Flow

```python
# In org_task.py

# 1. Get last sync timestamp
last_sync_at = get_last_sync_timestamp(integration, "contacts")
is_initial = last_sync_at is None

# 2. Fetch records (fetcher handles incremental logic)
records = await fetcher(
    nango=nango,
    connection_id=connection_id,
    is_initial=is_initial,
    last_synced_at=last_sync_at,  # Passed to HubSpot provider
    ...
)

# 3. Sync to database
if is_initial:
    metrics = await sync_service.sync(...)
else:
    metrics = await sync_service.sync_incremental(...)

# 4. Update timestamp
update_last_sync_timestamp(integration, "contacts", sync_start)
```

---

## 7. HubSpot API Patterns

### 7.1 Critical: POST /search for Incremental Sync

**HubSpot requires POST requests to the `/search` endpoint for filtering.** GET requests with `filterGroups` as query parameters are ignored.

#### Incorrect (Does NOT work):
```
GET /crm/v3/objects/contacts?filterGroups={...}
```

#### Correct:
```
POST /crm/v3/objects/contacts/search
Content-Type: application/json

{
    "filterGroups": [{
        "filters": [{
            "propertyName": "hs_lastmodifieddate",
            "operator": "GTE",
            "value": "1736600000000"
        }]
    }],
    "properties": ["email", "firstname", ...],
    "limit": 100
}
```

### 7.2 Timestamp Format

HubSpot expects **Unix timestamp in milliseconds**:

```python
# Convert datetime to Unix milliseconds
last_sync_time_ms = int(last_synced_at.timestamp() * 1000)

# Example: 2026-01-11T14:30:00 → 1768156200000
```

### 7.3 Search Endpoint Limitations

- Maximum **10,000 results** per query
- Maximum **100 records per page**
- Pagination uses `after` cursor in response

### 7.4 Fetcher Implementation

```python
# src/workers/integrations_worker/providers/crm/hubspot/fetchers/contact_fetcher.py

async def fetch_all(self, *, incremental: bool = False, last_sync_time: str | None = None):
    if incremental and last_sync_time:
        # Use POST /search for incremental
        return await self._fetch_incremental(last_sync_time=last_sync_time)

    # Use GET for full sync
    return await self._fetch_paginated(endpoint="/crm/v3/objects/contacts", ...)

async def _fetch_incremental(self, last_sync_time: str, limit: int = 100):
    """Fetch using POST /search endpoint."""
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "hs_lastmodifieddate",
                "operator": "GTE",
                "value": last_sync_time,  # Unix ms as string
            }]
        }],
        "properties": HUBSPOT_CONTACT_PROPERTIES,
        "limit": min(limit, 100),
    }

    response = await self.nango_client.proxy_request(
        endpoint="/crm/v3/objects/contacts/search",
        method="POST",
        data=body,
    )
```

### 7.5 Provider Wrapper

```python
# src/workers/integrations_worker/providers/crm/hubspot/__init__.py

async def fetch_contacts(nango, connection_id, is_initial, last_synced_at, ...):
    fetcher = HubSpotContactFetcher(nango, provider_config_key, connection_id)

    # Convert datetime to Unix milliseconds
    last_sync_time_ms = int(last_synced_at.timestamp() * 1000) if last_synced_at else None

    records = await fetcher.fetch_all(
        incremental=not is_initial,
        last_sync_time=str(last_sync_time_ms) if last_sync_time_ms else None,
    )

    return records
```

---

## 8. Code Structure

### 8.1 Fetchers

```
src/workers/integrations_worker/providers/crm/hubspot/fetchers/
├── __init__.py
├── contact_fetcher.py      # POST /search for incremental
├── company_fetcher.py      # POST /search for incremental
├── deal_fetcher.py         # POST /search for incremental
├── deal_task_fetcher.py    # POST /search for incremental
├── note_fetcher.py         # POST /search for incremental
├── pipeline_fetcher.py     # GET only (no incremental)
├── user_fetcher.py         # GET only (no incremental)
└── owner_fetcher.py        # GET only (no incremental)
```

### 8.2 Sync Services

```
src/service/integrations/crm/entities/
├── contact_sync_service.py
├── company_sync_service.py
├── deal_sync_service.py
├── deal_task_sync_service.py
├── pipeline_sync_service.py
└── user_sync_service.py
```

Each service has:
- `sync()` - Full sync (upsert all records)
- `sync_incremental()` - Incremental sync (upsert changed, handle deletes)

---

## 9. Configuration

### 9.1 Task Routes

```python
# src/workers/integrations_worker/config.py

self.task_routes = {
    "integrations.sync_crm_contacts_all_orgs": {"queue": "integrations"},
    "integrations.sync_crm_contacts_org": {"queue": "integrations"},
    "integrations.sync_crm_contacts_integration": {"queue": "integrations_priority"},
    # ... same pattern for other entities
}
```

### 9.2 Beat Schedule

```python
self.beat_schedule = {
    "sync-crm-deals-all-orgs": {
        "task": "integrations.sync_crm_deals_all_orgs",
        "schedule": crontab(minute="*/3"),  # Every 3 minutes
    },
    "sync-crm-contacts-all-orgs": {
        "task": "integrations.sync_crm_contacts_all_orgs",
        "schedule": crontab(minute=20, hour="*/3"),  # Every 3 hours
    },
    # ...
}
```

### 9.3 Recommended Sync Frequencies

| Entity | Frequency | Rationale |
|--------|-----------|-----------|
| Deals | Every 3-5 minutes | Most critical, changes frequently |
| Contacts | Every 3 hours | Medium frequency |
| Companies | Every 3 hours | Medium frequency |
| Activities | Every 1 hour | Task updates |
| Pipelines | Every 6 hours | Rarely changes |
| Users | Every 6 hours | Rarely changes |

---

## 10. Troubleshooting

### 10.1 All Records Fetched on Incremental Sync

**Symptom**: `is_initial=False` but all 75 records fetched

**Cause**: Using GET endpoint or wrong timestamp format

**Solution**:
1. Verify fetcher uses POST `/search` endpoint
2. Verify timestamp is Unix milliseconds (not ISO string)

### 10.2 Datetime Timezone Errors

**Symptom**: `can't subtract offset-naive and offset-aware datetimes`

**Cause**: Mixing `datetime.now(UTC)` with database columns

**Solution**: Use `datetime.utcnow()` for database timestamps (naive UTC)

```python
# Correct
sync_start = datetime.utcnow()

# Incorrect
sync_start = datetime.now(UTC)
```

### 10.3 filterGroups Ignored

**Symptom**: Filter appears in URL but not applied

**Cause**: Using GET instead of POST

**Solution**: Use POST to `/search` endpoint with filter in body

### 10.4 Search Limit Exceeded

**Symptom**: Warning about 10,000 results limit

**Cause**: Too many records modified since last sync

**Solution**: Reduce sync interval to avoid hitting limit

---

## Appendix: Entity Property Lists

### Contacts Properties

```python
HUBSPOT_CONTACT_PROPERTIES = [
    "email", "firstname", "lastname", "jobtitle", "company",
    "phone", "lifecyclestage", "hubspot_owner_id",
    "createdate", "hs_lastmodifieddate", "hs_object_id",
    # ... see contact_fetcher.py for full list
]
```

### Deals Properties

```python
HUBSPOT_DEAL_PROPERTIES = [
    "dealname", "amount", "pipeline", "dealstage", "closedate",
    "hubspot_owner_id", "createdate", "hs_lastmodifieddate",
    # ... see deal_fetcher.py for full list
]
```
