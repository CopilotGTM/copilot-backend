# Salesforce CRM Integration - Implementation Plan

> **Status**: Phase 1 implemented (provider scaffold + registry wiring)  
> **Target**: December 2025 - January 2026  
> **Author**: Generated from HubSpot Integration Analysis  
> **Last Updated**: December 16, 2025

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current HubSpot Architecture Analysis](#2-current-hubspot-architecture-analysis)
3. [Salesforce Data Model Mapping](#3-salesforce-data-model-mapping)
4. [Implementation Phases](#4-implementation-phases)
5. [Enrichment Strategy](#5-enrichment-strategy)
6. [Technical Implementation Details](#6-technical-implementation-details)
7. [Testing Strategy](#7-testing-strategy)
8. [Rollout Plan](#8-rollout-plan)

---

## 1. Executive Summary

### Objective

Integrate Salesforce CRM as a second CRM provider alongside HubSpot, leveraging the existing provider-agnostic sync architecture.

### Scope

- **Core Entities**: Opportunities (Deals), Contacts, Accounts (Companies), Leads, Users, Tasks
- **Enrichment**: Pipeline/Stage metadata, Owner details, Contact associations
- **Sync Modes**: Initial full sync + Incremental webhook-driven updates

### Key Advantages of Current Architecture

The existing HubSpot integration follows a **provider-agnostic pattern** that makes adding Salesforce straightforward:

| Component | Pattern | Benefit for Salesforce |
|-----------|---------|----------------------|
| Provider Registry | `(category, provider) → fetcher` map | Just add `("crm", "salesforce")` |
| Base Fetcher | Abstract class with pagination | Salesforce fetchers extend it |
| Entity Adapters | Transform raw → model | Salesforce-specific transformers |
| Sync Services | Provider-agnostic upsert | Reuse 100% |
| Enrichment | Post-sync data population | Reuse 100% |

---

## 2. Current HubSpot Architecture Analysis

### 2.1 Data Flow Overview

```
┌─────────────┐    ┌──────────┐    ┌───────────────┐    ┌──────────────┐
│   Nango     │───▶│ Webhook  │───▶│ Celery Task   │───▶│  Dispatcher  │
│  (OAuth +   │    │ Handler  │    │ sync_task.py  │    │  dispatcher  │
│   Proxy)    │    │          │    │               │    │    .py       │
└─────────────┘    └──────────┘    └───────────────┘    └──────┬───────┘
                                                               │
       ┌───────────────────────────────────────────────────────┼───────────────────────────┐
       │                                                       │                           │
       ▼                                                       ▼                           ▼
┌──────────────┐                                      ┌──────────────┐           ┌──────────────┐
│  Registry    │                                      │   Fetcher    │           │   Adapter    │
│  (provider   │ ─────────────────────────────────▶  │  (HubSpot    │  ──────▶ │  (crm_       │
│   lookup)    │                                      │   specific)  │           │   adapter)   │
└──────────────┘                                      └──────────────┘           └──────┬───────┘
                                                                                        │
                                                                                        ▼
                                                                               ┌──────────────┐
                                                                               │ Sync Service │
                                                                               │ (upsert to   │
                                                                               │   DB)        │
                                                                               └──────────────┘
```

### 2.2 Key Files and Their Roles

#### Provider Registry (`providers/registry.py`)

```python
# Current HubSpot registration
_REGISTRY[("crm", "hubspot")] = {
    "pipelines": hubspot_pipelines_fetch,
    "deals": hubspot_deals_fetch,
    "deal_tasks": hubspot_deal_tasks_fetch,
    "contacts": hubspot_contacts_fetch,
    "users": hubspot_users_fetch,
    "companies": hubspot_companies_fetch,
    "owners": hubspot_owners_fetch,
}
```

#### Base Fetcher (`core/base_fetcher.py`)

Abstract class providing:

- `_fetch_paginated()` - Handles cursor-based pagination
- `_fetch_batch()` - Batch requests by ID
- `_extract_records_from_response()` - Override per provider
- `_has_next_page()` / `_get_next_page_params()` - Pagination hooks

#### HubSpot Deal Fetcher Example (`providers/crm/hubspot/fetchers/deal_fetcher.py`)

```python
class HubSpotDealFetcher(BaseFetcher):
    async def fetch_all(self, *, incremental: bool = False) -> list[dict]:
        params = {
            "limit": 50,
            "properties": HUBSPOT_DEAL_PROPERTIES,
            "associations": ["contacts", "companies"],
            "propertiesWithHistory": HUBSPOT_DEAL_PROPERTY_HISTORY,
        }
        active = await self._fetch_paginated("/crm/v3/objects/deals", params)
        archived = await self._fetch_paginated(..., archived=True)
        return [*active, *archived]
```

#### Entity Adapter (`adapters/crm/entity_adapters/deal_adapter.py`)

- Transforms raw provider data to model format
- Auto-syncs pipelines after deals
- Calls enrichment service

#### Sync Service (`service/integrations/crm/entities/deal_sync_service.py`)

- **Provider-agnostic** upsert logic
- Handles participants, stage history
- Calls enrichment after sync

### 2.3 Nango Configuration (From Screenshot)

The Nango dashboard shows Salesforce has these syncs configured:

| Sync Name | Model | Frequency | Records |
|-----------|-------|-----------|---------|
| accounts | Account | 1h | 16 |
| contacts | Contact | 1h | 23 |
| leads | Lead | 1h | 22 |
| opportunities | Opportunity | 1h | 34 |
| tickets | Ticket | 1d | 26 |

**Key Insight**: Nango already provides normalized data models. Our fetchers will call Nango's proxy API to retrieve this data.

---

## 3. Salesforce Data Model Mapping

### 3.1 Entity Mapping: Salesforce → Internal Models

| Salesforce Object | Internal Model | HubSpot Equivalent |
|-------------------|----------------|-------------------|
| Opportunity | `CRMDeal` | Deal |
| Contact | `CRMContact` | Contact |
| Account | `CRMCompany` | Company |
| Lead | `CRMContact` (with `is_lead=True`) | N/A (contacts) |
| User | `CRMUser` | Owner/User |
| Task | `CRMActivity` | Task |
| Event | `CRMActivity` | Meeting |

### 3.2 Field Mapping: Salesforce Opportunity → CRMDeal

| Salesforce Field | CRMDeal Field | Notes |
|------------------|---------------|-------|
| `Id` | `external_id` | Salesforce 18-char ID |
| `Name` | `name` | Deal/Opportunity name |
| `Amount` | `amount` | Deal value |
| `CurrencyIsoCode` | `currency_code` | Multi-currency support |
| `StageName` | `stage_external_id`, `stage_name` | Pipeline stage |
| `CloseDate` | `close_date` | Expected close date |
| `CreatedDate` | `source_created_at` | |
| `LastModifiedDate` | `source_modified_at` | For incremental sync |
| `OwnerId` | `owner_external_id` | → enrich to `owner_name` |
| `AccountId` | `company_external_id` | → enrich to `company_name` |
| `Probability` | `probability` | Win probability % |
| `IsClosed` | infer `status` | + `IsWon` for WON/LOST |
| `ForecastCategory` | `forecast_category` | |
| `Type` | `deal_type` | |
| `Description` | `description` | |
| `LeadSource` | `lead_source` | |
| `ContactId` (lookup) | `primary_contact_external_id` | Primary contact |
| `OpportunityContactRoles` | `participants` | Relationship table |

### 3.3 Field Mapping: Salesforce Contact → CRMContact

| Salesforce Field | CRMContact Field |
|------------------|-----------------|
| `Id` | `external_id` |
| `FirstName` | `first_name` |
| `LastName` | `last_name` |
| `Email` | `email` |
| `Phone` | `phone` |
| `MobilePhone` | `mobile_phone` |
| `Title` | `job_title` |
| `Department` | `department` |
| `AccountId` | `company_external_id` |
| `OwnerId` | `owner_external_id` |
| `MailingStreet/City/State/PostalCode/Country` | `address_*` fields |
| `LeadSource` | `lead_source` |
| `CreatedDate` | `source_created_at` |
| `LastModifiedDate` | `source_modified_at` |

### 3.4 Field Mapping: Salesforce Account → CRMCompany

| Salesforce Field | CRMCompany Field |
|------------------|-----------------|
| `Id` | `external_id` |
| `Name` | `name` |
| `Website` | `domain` (extract domain) |
| `Industry` | `industry` |
| `Type` | `company_type` |
| `Phone` | `phone` |
| `NumberOfEmployees` | `employee_count` |
| `AnnualRevenue` | `annual_revenue` |
| `BillingStreet/City/State/PostalCode/Country` | `address_*` |
| `OwnerId` | `owner_external_id` |
| `Description` | `description` |
| `CreatedDate` | `source_created_at` |
| `LastModifiedDate` | `source_modified_at` |

### 3.5 Field Mapping: Salesforce User → CRMUser

| Salesforce Field | CRMUser Field |
|------------------|--------------|
| `Id` | `external_id` |
| `FirstName` | `first_name` |
| `LastName` | `last_name` |
| `Email` | `email` |
| `Username` | `username` |
| `IsActive` | `is_active` |
| `UserRole.Name` | `role_name` |
| `Profile.Name` | `profile_name` |
| `Title` | `job_title` |

### 3.6 Salesforce-Specific Considerations

#### Leads vs Contacts

Salesforce has separate Lead and Contact objects. After conversion, a Lead becomes a Contact + Account + Opportunity. Strategy:

- Sync Leads as `CRMContact` with `is_lead = True` or `lifecycle_stage = "lead"`
- Include `ConvertedContactId`, `ConvertedAccountId`, `ConvertedOpportunityId` for tracking conversion

#### OpportunityContactRoles (Associations)

Salesforce uses `OpportunityContactRole` to link Contacts to Opportunities with roles:

```json
{
  "Id": "00K...",
  "OpportunityId": "006...",
  "ContactId": "003...",
  "Role": "Decision Maker",
  "IsPrimary": true
}
```

→ Maps to `CRMDealParticipant` with `participant_type=CONTACT`, `role=role_value`

#### Record Types & Custom Objects

Salesforce supports multiple Record Types per object. Consider:

- Store `RecordTypeId` in `properties` JSONB
- Support custom fields via `properties` storage

---

## 4. Implementation Phases

### Phase 1: Core Infrastructure (Week 1)

**Goal**: Set up Salesforce provider skeleton and registry

**Status**: Implemented (scaffold fetchers currently return no records until Phase 2)

- Implemented in: `src/workers/integrations_worker/providers/crm/salesforce/__init__.py`
- Wired in: `src/workers/integrations_worker/providers/registry.py`

#### Tasks

1. **Create Salesforce provider directory structure**

   ```
   src/workers/integrations_worker/providers/crm/salesforce/
   ├── __init__.py           # Export fetchers + wrapper functions
   └── fetchers/
       ├── __init__.py       # Export fetcher classes
       ├── opportunity_fetcher.py
       ├── contact_fetcher.py
       ├── account_fetcher.py
       ├── lead_fetcher.py
       ├── user_fetcher.py
       └── task_fetcher.py
   ```

2. **Register Salesforce in provider registry** (`providers/registry.py`)

   ```python
   _REGISTRY[("crm", "salesforce")] = {
       "opportunities": salesforce_opportunities_fetch,
       "deals": salesforce_opportunities_fetch,  # Alias
       "contacts": salesforce_contacts_fetch,
       "leads": salesforce_leads_fetch,
       "accounts": salesforce_accounts_fetch,
       "companies": salesforce_accounts_fetch,  # Alias
       "users": salesforce_users_fetch,
       "tasks": salesforce_tasks_fetch,
   }
   ```

3. **Verify provider categorization** (`infra/config/provider_categories.py`)
   - Already has: `IntegrationType.SALESFORCE: ("crm", "salesforce")` ✓

4. **Verify Nango mapping** (`infra/config/nango_provider_mapping.py`)
   - Already has: `IntegrationType.SALESFORCE: "salesforce"` ✓

### Phase 2: Fetchers (Week 1-2)

**Goal**: Implement data fetching from Nango proxy

**Status**: Implemented (SOQL fetchers via Nango proxy; mapping/upserts happen in Phase 3)

#### OpportunityFetcher (Priority 1)

```python
# src/workers/integrations_worker/providers/crm/salesforce/fetchers/opportunity_fetcher.py

SALESFORCE_OPPORTUNITY_FIELDS = [
    "Id", "Name", "Amount", "CurrencyIsoCode", "StageName",
    "CloseDate", "CreatedDate", "LastModifiedDate",
    "OwnerId", "AccountId", "Probability", "IsClosed", "IsWon",
    "ForecastCategory", "Type", "Description", "LeadSource",
    # Add custom fields as needed
]

class SalesforceOpportunityFetcher(BaseFetcher):
    """Fetcher for Salesforce Opportunity data via Nango."""

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_sync_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch opportunities from Salesforce via Nango sync.
        
        Nango's 'opportunities' sync returns normalized Opportunity records.
        """
        # Option A: Use Nango sync records endpoint
        records = await self.nango_client.get_records(
            provider_config_key=self.provider_config_key,
            connection_id=self.connection_id,
            model="Opportunity",  # Nango model name
            # Nango handles incremental via delta_token
        )
        return records
        
        # Option B: Use Nango proxy to call Salesforce SOQL
        # (More control but requires building queries)
```

#### Key Difference from HubSpot

- **HubSpot**: Uses `/crm/v3/objects/deals` REST API with properties/associations
- **Salesforce via Nango**: Use Nango's sync records API which returns pre-normalized data
- **Salesforce Direct**: Would use SOQL queries through Nango proxy

#### Recommended Approach

Use **Nango sync records** (`nango.get_records()`) as primary method:

- Nango already handles OAuth refresh, pagination, incremental sync
- Returns normalized data matching Nango model definitions
- Webhook-driven updates already configured

### Phase 3: Transformers (Week 2)

**Goal**: Map Salesforce data to internal models

**Status**: Implemented (provider-side canonicalization to the existing `id` + `properties` payload shape)

- Implemented in: `src/workers/integrations_worker/providers/crm/salesforce/__init__.py`
- Notes: This approach avoids large changes to existing sync services and keeps Salesforce records compatible with the current HubSpot-oriented transformation logic.

Create Salesforce-specific transformation logic in the sync services or adapters:

```python
# In deal_sync_service.py - add provider-specific handling

def _update_deal_record(self, record: CRMDeal, raw: dict, provider: str, ...):
    if provider == "salesforce":
        self._update_deal_from_salesforce(record, raw)
    elif provider == "hubspot":
        self._update_deal_from_hubspot(record, raw)

def _update_deal_from_salesforce(self, record: CRMDeal, raw: dict) -> None:
    """Transform Salesforce Opportunity to CRMDeal."""
    record.name = raw.get("Name")
    record.amount = self._parse_decimal(raw.get("Amount"))
    record.currency_code = raw.get("CurrencyIsoCode")
    record.close_date = self._parse_date(raw.get("CloseDate"))
    record.owner_external_id = raw.get("OwnerId")
    record.company_external_id = raw.get("AccountId")
    record.probability = raw.get("Probability")
    record.description = raw.get("Description")
    record.lead_source = raw.get("LeadSource")
    record.deal_type = raw.get("Type")
    record.forecast_category = raw.get("ForecastCategory")
    
    # Status derivation
    if raw.get("IsClosed"):
        record.status = CRMDealStatus.WON if raw.get("IsWon") else CRMDealStatus.LOST
    else:
        record.status = CRMDealStatus.OPEN
    
    # Stage mapping (requires pipeline sync first)
    record.stage_name = raw.get("StageName")
    
    # Raw data preservation
    record.data_payload = raw
```

### Phase 4: Pipeline & Stage Sync (Week 2-3)

**Goal**: Sync Salesforce Opportunity Stages for enrichment

**Status**: Implemented (SOQL query fetches OpportunityStage records; wrapped as single "salesforce_opportunity" pipeline)

- Implemented in: `src/workers/integrations_worker/providers/crm/salesforce/fetchers/pipeline_fetcher.py`
- Pipeline wrapper in: `src/workers/integrations_worker/providers/crm/salesforce/__init__.py`

Salesforce doesn't have explicit "pipelines" like HubSpot. Instead:

- **OpportunityStage** picklist values define stages
- Different Record Types can have different stage paths

#### Approach

1. **Default Pipeline**: Create a single pipeline per Salesforce org
2. **Stages**: Fetch from Salesforce metadata API or OpportunityStage describe

```python
class SalesforcePipelineFetcher(BaseFetcher):
    """Fetch pipeline/stage metadata from Salesforce."""
    
    async def fetch_all(self, **kwargs) -> list[dict[str, Any]]:
        # Use Nango proxy to call Salesforce describe API
        response = await self.nango_client.proxy_request(
            provider_config_key=self.provider_config_key,
            connection_id=self.connection_id,
            endpoint="/services/data/vXX.0/sobjects/Opportunity/describe",
            method="GET",
        )
        
        # Extract stage picklist
        stage_field = next(
            (f for f in response.get("fields", []) if f["name"] == "StageName"),
            None
        )
        
        if not stage_field:
            return []
        
        # Build pipeline + stages structure
        return [{
            "id": "default",
            "name": "Sales Pipeline",
            "stages": [
                {
                    "id": pv["value"],
                    "name": pv["label"],
                    "probability": pv.get("defaultProbability"),
                    "is_closed": pv.get("closed", False),
                    "is_won": pv.get("won", False),
                    "display_order": idx,
                }
                for idx, pv in enumerate(stage_field.get("picklistValues", []))
                if pv.get("active", True)
            ]
        }]
```

### Phase 5: Association Sync (Week 3)

**Goal**: Sync OpportunityContactRoles for deal participants

**Status**: Implemented (contact roles embedded in deals' associations during fetch)

- Fetcher: `src/workers/integrations_worker/providers/crm/salesforce/fetchers/contact_role_fetcher.py`
- Integration: `fetch_opportunities()` in provider `__init__.py` fetches contact roles and embeds them in the canonical deal's `associations.contacts.results` field
- Uses existing `DealSyncService._extract_contact_participants()` without modification

```python
class SalesforceContactRoleFetcher(BaseFetcher):
    """Fetch OpportunityContactRoles for deal participants."""
    
    async def fetch_all(self, **kwargs) -> list[dict[str, Any]]:
        # SOQL query via Nango proxy
        query = """
            SELECT Id, OpportunityId, ContactId, Role, IsPrimary, CreatedDate
            FROM OpportunityContactRole
        """
        
        response = await self.nango_client.proxy_request(
            provider_config_key=self.provider_config_key,
            connection_id=self.connection_id,
            endpoint=f"/services/data/vXX.0/query?q={urllib.parse.quote(query)}",
            method="GET",
        )
        
        return response.get("records", [])
```

**Integration with DealSyncService:**

- Fetch associations separately or embed in Opportunity query
- Map to `CRMDealParticipant` records with `participant_type=CONTACT`

### Phase 6: Enrichment Integration (Week 3-4)

**Goal**: Ensure enrichment works for Salesforce data

**Status**: Verified compatible (no changes needed - enrichment services are provider-agnostic)

The existing enrichment services are **provider-agnostic**:

```python
# DealEnrichmentService already handles:
# 1. enrich_deals() - Populate owner_name, company_name from related entities
# 2. enrich_participants() - Populate related_email from contacts/users

# No changes needed! Just ensure:
# - Salesforce users/contacts/accounts sync BEFORE or AFTER deals
# - Integration webhooks trigger re-enrichment after contacts sync
```

**Enrichment Flow (Already Implemented in dispatcher.py):**

```
Salesforce Contacts Sync Complete
        ↓
_trigger_deal_tasks_on_initial_sync()
        ↓
_enrich_participants_after_crm_sync()  # Re-enrich with contact emails
        ↓
process_deal_meetings_task  # Match calendar events
process_deal_emails_task    # Match emails
```

---

## 5. Enrichment Strategy

### 5.1 Current Enrichment Architecture

The system has two enrichment services:

#### DealEnrichmentService (`deal_enrichment_service.py`)

Enriches `CRMDeal` records with denormalized data:

- `owner_name`, `owner_email` from `CRMUser` (via `owner_external_id`)
- `company_name`, `company_domain` from `CRMCompany` (via `company_external_id`)
- `primary_contact_name`, `primary_contact_email` from `CRMContact`

#### TaskEnrichmentService (`task_enrichment_service.py`)

Enriches `CRMActivity` records with related entity details.

### 5.2 Pipeline-Based Enrichment

After deals sync, pipeline metadata enriches:

- `pipeline_name` (from `CRMPipeline.name`)
- `stage_name` (from `CRMDealStage.name`)
- `probability` (from stage default probability)

This happens in `deal_adapter.py`:

```python
async def sync_deals(...):
    # 1. Sync deals
    deal_result = await service.sync(...)
    
    # 2. Auto-sync pipelines
    pipeline_result = await _auto_sync_pipelines(...)
    
    # 3. Enrich deals with pipeline metadata
    deals_enriched = await pipeline_service.enrich_deals_with_pipeline_metadata(...)
```

### 5.3 Salesforce-Specific Enrichment Considerations

| Enrichment | Salesforce Source | Notes |
|------------|-------------------|-------|
| Owner Name/Email | User object | Same as HubSpot |
| Company Name | Account object | Same as HubSpot |
| Pipeline/Stage Names | OpportunityStage metadata | Different approach needed |
| Contact Roles | OpportunityContactRole | Explicit role labels |
| Record Type | RecordType object | Salesforce-specific |

### 5.4 Enrichment Timing

```
┌──────────────────────────────────────────────────────────────────┐
│                    INITIAL SYNC ORDER                            │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. Users sync     → CRMUser table populated                    │
│  2. Accounts sync  → CRMCompany table populated                 │
│  3. Contacts sync  → CRMContact table populated                 │
│  4. Pipelines sync → CRMPipeline + CRMDealStage populated       │
│  5. Opportunities  → CRMDeal created + enriched with:           │
│     sync             - owner_name (from CRMUser)                │
│                      - company_name (from CRMCompany)           │
│                      - pipeline_name (from CRMPipeline)         │
│                      - stage_name (from CRMDealStage)           │
│                                                                  │
│  6. Contact Roles  → CRMDealParticipant populated               │
│     sync             - Triggers participant enrichment          │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 6. Technical Implementation Details

### 6.1 File Structure

```
src/workers/integrations_worker/
├── providers/
│   └── crm/
│       ├── hubspot/          # Existing
│       └── salesforce/       # NEW
│           ├── __init__.py
│           └── fetchers/
│               ├── __init__.py
│               ├── opportunity_fetcher.py
│               ├── contact_fetcher.py
│               ├── account_fetcher.py
│               ├── lead_fetcher.py
│               ├── user_fetcher.py
│               ├── task_fetcher.py
│               ├── pipeline_fetcher.py  # Stage metadata
│               └── contact_role_fetcher.py  # Associations
│
├── adapters/
│   └── crm/
│       └── entity_adapters/
│           └── ... (reuse existing, add salesforce transforms)
│
└── services/
    └── dispatcher.py  # Add salesforce-specific routing if needed

src/service/integrations/crm/
├── entities/
│   ├── deal_sync_service.py      # Add Salesforce transform branch
│   ├── contact_sync_service.py   # Add Salesforce transform branch
│   ├── company_sync_service.py   # Add Salesforce transform branch
│   └── user_sync_service.py      # Add Salesforce transform branch
│
└── transformers/                  # NEW (optional - cleaner separation)
    ├── __init__.py
    ├── base_transformer.py
    ├── hubspot_transformer.py
    └── salesforce_transformer.py
```

### 6.2 Registry Updates

```python
# providers/registry.py

from .crm.salesforce import (
    fetch_opportunities as salesforce_opportunities_fetch,
    fetch_contacts as salesforce_contacts_fetch,
    fetch_accounts as salesforce_accounts_fetch,
    fetch_leads as salesforce_leads_fetch,
    fetch_users as salesforce_users_fetch,
    fetch_tasks as salesforce_tasks_fetch,
    fetch_pipelines as salesforce_pipelines_fetch,
)

def _init_defaults() -> None:
    # ... existing registrations ...
    
    # CRM: Salesforce
    _REGISTRY[("crm", "salesforce")] = {
        "opportunities": salesforce_opportunities_fetch,
        "deals": salesforce_opportunities_fetch,  # Alias for consistency
        "contacts": salesforce_contacts_fetch,
        "leads": salesforce_leads_fetch,
        "accounts": salesforce_accounts_fetch,
        "companies": salesforce_accounts_fetch,  # Alias for consistency
        "users": salesforce_users_fetch,
        "tasks": salesforce_tasks_fetch,
        "pipelines": salesforce_pipelines_fetch,
    }
```

### 6.3 Nango Sync Name Mapping

Nango webhook includes `sync_name` which needs to map to our data types:

```python
# In dispatcher.py or a new mapping file

SALESFORCE_SYNC_NAME_MAP = {
    "opportunities": ["opportunities", "deals"],
    "contacts": ["contacts"],
    "accounts": ["accounts", "companies"],
    "leads": ["leads", "contacts"],  # Leads sync to contacts too
    "users": ["users"],
    "tickets": ["tasks"],  # If using Salesforce Cases/Tasks
}
```

### 6.4 Transformer Pattern (Recommended)

For cleaner provider-specific logic, introduce a transformer layer:

```python
# src/service/integrations/crm/transformers/base_transformer.py

from abc import ABC, abstractmethod
from typing import Any
from src.domain.models.organisation_tables.sync.crm import CRMDeal, CRMContact, CRMCompany

class BaseCRMTransformer(ABC):
    """Base class for CRM data transformers."""
    
    @abstractmethod
    def transform_deal(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Transform raw deal data to model fields."""
        pass
    
    @abstractmethod
    def transform_contact(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Transform raw contact data to model fields."""
        pass
    
    @abstractmethod
    def transform_company(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Transform raw company data to model fields."""
        pass
```

```python
# src/service/integrations/crm/transformers/salesforce_transformer.py

from .base_transformer import BaseCRMTransformer

class SalesforceTransformer(BaseCRMTransformer):
    """Salesforce-specific data transformations."""
    
    def transform_deal(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Transform Salesforce Opportunity to deal fields."""
        is_closed = raw.get("IsClosed", False)
        is_won = raw.get("IsWon", False)
        
        if is_closed:
            status = "won" if is_won else "lost"
        else:
            status = "open"
        
        return {
            "external_id": raw.get("Id"),
            "name": raw.get("Name"),
            "amount": raw.get("Amount"),
            "currency_code": raw.get("CurrencyIsoCode"),
            "stage_name": raw.get("StageName"),
            "close_date": raw.get("CloseDate"),
            "owner_external_id": raw.get("OwnerId"),
            "company_external_id": raw.get("AccountId"),
            "probability": raw.get("Probability"),
            "status": status,
            "forecast_category": raw.get("ForecastCategory"),
            "deal_type": raw.get("Type"),
            "description": raw.get("Description"),
            "lead_source": raw.get("LeadSource"),
            "source_created_at": raw.get("CreatedDate"),
            "source_modified_at": raw.get("LastModifiedDate"),
            "data_payload": raw,
        }
```

---

## 7. Testing Strategy

### 7.1 Unit Tests

```python
# tests/service/integrations/crm/test_salesforce_transformer.py

def test_transform_deal_open():
    transformer = SalesforceTransformer()
    raw = {
        "Id": "006ABC123",
        "Name": "Acme Deal",
        "Amount": 50000,
        "StageName": "Negotiation",
        "IsClosed": False,
    }
    result = transformer.transform_deal(raw)
    assert result["external_id"] == "006ABC123"
    assert result["status"] == "open"

def test_transform_deal_won():
    transformer = SalesforceTransformer()
    raw = {"Id": "006ABC123", "IsClosed": True, "IsWon": True}
    result = transformer.transform_deal(raw)
    assert result["status"] == "won"

def test_transform_deal_lost():
    transformer = SalesforceTransformer()
    raw = {"Id": "006ABC123", "IsClosed": True, "IsWon": False}
    result = transformer.transform_deal(raw)
    assert result["status"] == "lost"
```

### 7.2 Integration Tests

```python
# tests/workers/integrations/test_salesforce_sync.py

@pytest.mark.asyncio
async def test_salesforce_opportunity_sync(
    db_session,
    mock_nango_client,
    test_integration,
):
    """Test full Salesforce opportunity sync flow."""
    # Setup mock Nango response
    mock_nango_client.get_records.return_value = [
        {"Id": "006ABC", "Name": "Test Opp", "Amount": 10000, ...},
    ]
    
    # Execute sync
    from src.workers.integrations_worker.services.dispatcher import execute_sync
    result = await execute_sync(SyncJobRequest(
        organisation_id=test_org_id,
        integration_id=test_integration.integration_id,
        category="crm",
        provider="salesforce",
        sync_name="opportunities",
    ))
    
    # Verify
    assert result.status == "success"
    deal = await db_session.execute(
        select(CRMDeal).where(CRMDeal.external_id == "006ABC")
    )
    assert deal.first().name == "Test Opp"
```

### 7.3 End-to-End Tests

1. **OAuth Flow**: Connect Salesforce via Nango Connect
2. **Initial Sync**: Verify all entity types sync correctly
3. **Incremental Sync**: Modify record in Salesforce, verify webhook triggers update
4. **Enrichment**: Verify deals get owner_name, company_name populated
5. **Deal Matching**: Verify calendar events match to Salesforce opportunities

---

## 8. Rollout Plan

### Phase 1: Development (Week 1-2)

- [ ] Create fetcher classes
- [ ] Implement transformers
- [ ] Register in provider registry
- [ ] Unit tests

### Phase 2: Integration Testing (Week 3)

- [ ] Integration tests with mock Nango
- [ ] Test with real Salesforce sandbox
- [ ] Enrichment verification
- [ ] Performance testing

### Phase 3: Staging Deployment (Week 4)

- [ ] Deploy to staging
- [ ] Connect test Salesforce org
- [ ] Full sync validation
- [ ] Webhook verification

### Phase 4: Production Rollout (Week 5)

- [ ] Feature flag deployment
- [ ] Beta customer rollout
- [ ] Monitor error rates
- [ ] General availability

---

## Appendix A: Nango API Reference

### Get Sync Records

```python
records = await nango.get_records(
    provider_config_key="salesforce",
    connection_id="user@company.com",
    model="Opportunity",
    delta=cursor,  # For incremental
)
```

### Proxy Request

```python
response = await nango.proxy_request(
    provider_config_key="salesforce",
    connection_id="user@company.com",
    endpoint="/services/data/v59.0/sobjects/Opportunity/describe",
    method="GET",
)
```

### Trigger Action

```python
result = await nango.action(
    provider_config_key="salesforce",
    connection_id="user@company.com",
    action_name="fetch-opportunities",
)
```

---

## Appendix B: Salesforce API Limits

| Limit Type | Value | Notes |
|------------|-------|-------|
| API Requests/Day | 15,000 - 1,000,000+ | Based on edition |
| SOQL Query Length | 20,000 chars | Use indexed fields |
| Records per Query | 2,000 | Use pagination |
| Batch Size | 200 | For bulk operations |

**Recommendation**: Rely on Nango's sync engine which handles rate limiting and pagination automatically.

---

## Appendix C: Data Model Comparison

```
HubSpot                          Salesforce
─────────────────────────────────────────────────────
Deal                    ←→       Opportunity
Contact                 ←→       Contact + Lead
Company                 ←→       Account
Owner                   ←→       User
Pipeline                ←→       (implicit - one per RecordType)
Pipeline Stage          ←→       OpportunityStage (picklist)
Deal Association        ←→       OpportunityContactRole
Task                    ←→       Task + Event
Note                    ←→       Note (ContentNote)
```

---

**Document Status**: Ready for Implementation Review
