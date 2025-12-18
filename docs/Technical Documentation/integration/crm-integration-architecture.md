# CRM Integration Architecture

> **Version**: 1.0  
> **Last Updated**: December 17, 2025  
> **Status**: Production (HubSpot), Phase 1 (Salesforce)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Database Tables](#2-database-tables)
3. [Sync Services](#3-sync-services)
4. [Provider Registry](#4-provider-registry)
5. [HubSpot Integration (Reference Implementation)](#5-hubspot-integration-reference-implementation)
6. [Salesforce Integration](#6-salesforce-integration)
7. [Post-Sync Enrichment](#7-post-sync-enrichment)
8. [Data Flow Diagrams](#8-data-flow-diagrams)

---

## 1. Overview

### Purpose

The CRM integration architecture enables synchronization of sales data from external CRM providers (HubSpot, Salesforce) into our canonical data model. The design follows a **provider-agnostic pattern** where:

- **Fetchers**: Provider-specific classes that retrieve raw data via Nango proxy
- **Transformers**: Convert provider-specific formats to canonical format
- **Sync Services**: Provider-agnostic services that upsert data to database
- **Enrichment Services**: Post-sync data population from related entities

### Supported Providers

| Provider | Status | Entities | Method |
| --- | --- | --- | --- |
| HubSpot | ✅ Production | Deals, Contacts, Companies, Users, Tasks, Pipelines | Nango Sync + API |
| Salesforce | 🔄 Phase 1 | Opportunities, Contacts, Accounts, Users, Tasks, Cases | SOQL via Nango Proxy |

---

## 2. Database Tables

All CRM tables are in the `crm` schema within organisation-specific databases.

### 2.1 Core Entity Tables

| Table | Model | Purpose |
| --- | --- | --- |
| `crm_deals` | `CRMDeal` | Normalized deal/opportunity records |
| `crm_contacts` | `CRMContact` | Contact profiles from CRM |
| `crm_companies` | `CRMCompany` | Company/Account records |
| `crm_users` | `CRMUser` | Internal CRM users (owners/team members) |
| `crm_activities` | `CRMActivity` | Tasks, calls, meetings, emails, notes |
| `crm_pipelines` | `CRMPipeline` | Sales pipeline definitions |
| `crm_deal_stages` | `CRMDealStage` | Pipeline stages with probability |

### 2.2 Relationship Tables

| Table | Model | Purpose |
| --- | --- | --- |
| `crm_deal_participants` | `CRMDealParticipant` | Deal ↔ Contact/Company/User links |
| `crm_deal_stage_history` | `CRMDealStageHistory` | Stage transition tracking |
| `crm_object_associations` | `CRMObjectAssociation` | Generic object associations |

### 2.3 Table Details

#### `crm_deals` - Deal/Opportunity Records

```text
┌─────────────────────────────────────────────────────────────────────┐
│ crm_deals                                                            │
├─────────────────────────────────────────────────────────────────────┤
│ Core Identity:                                                       │
│   • deal_id (UUID, PK)      - Internal identifier                   │
│   • external_id (String)    - Provider ID (e.g., HubSpot deal ID)   │
│   • integration_id (UUID)   - FK to knowledge_integrations          │
│                                                                      │
│ Business Context:                                                    │
│   • name                    - Deal name/title                       │
│   • amount, currency        - Deal value                            │
│   • deal_status             - OPEN, WON, LOST, CLOSED               │
│   • probability             - Win probability (0-100)               │
│   • close_date              - Expected/actual close                 │
│                                                                      │
│ Pipeline/Stage:                                                      │
│   • pipeline_id (FK)        - Resolved pipeline reference           │
│   • stage_id (FK)           - Resolved stage reference              │
│   • pipeline_external_id    - Provider pipeline ID                  │
│   • stage_external_id       - Provider stage ID                     │
│                                                                      │
│ Owner (Denormalized):                                               │
│   • owner_external_id       - Provider user ID                      │
│   • owner_id (FK)           - Resolved CRMUser reference            │
│   • owner_name, owner_email - Cached owner details                  │
│                                                                      │
│ Company (Denormalized):                                             │
│   • company_external_id     - Associated company ID                 │
│   • company_name, company_domain                                    │
│                                                                      │
│ Payloads:                                                           │
│   • properties (JSONB)      - All provider properties               │
│   • data_payload (JSONB)    - Raw API response for debugging        │
└─────────────────────────────────────────────────────────────────────┘
```

#### `crm_contacts` - Contact Records

```text
┌─────────────────────────────────────────────────────────────────────┐
│ crm_contacts                                                         │
├─────────────────────────────────────────────────────────────────────┤
│ Identity: contact_id, external_id, integration_id                   │
│                                                                      │
│ Profile:                                                            │
│   • first_name, last_name, full_name                               │
│   • email, phone, mobile_phone                                      │
│   • job_title, company_name                                         │
│                                                                      │
│ Classification:                                                      │
│   • lifecycle_stage         - lead, subscriber, customer, etc.     │
│   • lead_status             - Provider lead status                  │
│                                                                      │
│ Owner: owner_external_id, owner_email, owner_name, owner_id         │
└─────────────────────────────────────────────────────────────────────┘
```

#### `crm_companies` - Company/Account Records

```text
┌─────────────────────────────────────────────────────────────────────┐
│ crm_companies                                                        │
├─────────────────────────────────────────────────────────────────────┤
│ Identity: company_id, external_id, integration_id                   │
│                                                                      │
│ Core:                                                               │
│   • name, domain, website                                           │
│   • description, industry, company_type                             │
│   • lifecycle_stage                                                 │
│                                                                      │
│ Metrics:                                                            │
│   • number_of_employees                                             │
│   • annual_revenue                                                  │
│                                                                      │
│ Owner: owner_external_id, owner_email, owner_name, owner_id         │
└─────────────────────────────────────────────────────────────────────┘
```

#### `crm_users` - Internal Users/Owners

```text
┌─────────────────────────────────────────────────────────────────────┐
│ crm_users                                                            │
├─────────────────────────────────────────────────────────────────────┤
│ Purpose: Store CRM internal team members who own records            │
│                                                                      │
│ Identity: user_id, external_id, integration_id                      │
│                                                                      │
│ Profile:                                                            │
│   • email                   - User email (for matching)             │
│   • first_name, last_name, full_name                               │
│   • job_title, user_role                                           │
│   • time_zone, locale                                               │
│                                                                      │
│ Status:                                                             │
│   • is_active               - Whether user is active in CRM         │
│   • last_login_at           - Last login timestamp                  │
│                                                                      │
│ Team:                                                               │
│   • primary_team_id         - Primary team ID                       │
│   • team_ids (JSONB)        - All team memberships                  │
│                                                                      │
│ Note: HubSpot has "Owners" endpoint, Salesforce uses "User" object  │
└─────────────────────────────────────────────────────────────────────┘
```

#### `crm_activities` - Tasks/Calls/Meetings/Notes

```text
┌─────────────────────────────────────────────────────────────────────┐
│ crm_activities (alias: CRMDealTask)                                  │
├─────────────────────────────────────────────────────────────────────┤
│ Identity: activity_id, external_id, integration_id                  │
│                                                                      │
│ Activity Core:                                                       │
│   • subject, description    - Activity content                      │
│   • activity_type           - TASK, TODO, CALL, MEETING, EMAIL, NOTE│
│   • status                  - NOT_STARTED, IN_PROGRESS, COMPLETED   │
│   • priority                - NONE, LOW, MEDIUM, HIGH, URGENT       │
│                                                                      │
│ Timing:                                                             │
│   • due_date                - When activity is due                  │
│   • completed_at            - Completion timestamp                  │
│   • reminder_at             - Reminder timestamp                    │
│                                                                      │
│ Polymorphic Links (can link to any object):                         │
│   • related_object_type     - DEAL, CONTACT, COMPANY, etc.         │
│   • related_external_id     - Provider ID of related object         │
│   • related_local_id        - Resolved internal UUID                │
│                                                                      │
│ Specific Links (for common queries + FK integrity):                 │
│   • deal_id, deal_external_id                                       │
│   • contact_id, contact_external_id                                 │
│   • company_id, company_external_id                                 │
│                                                                      │
│ Owner:                                                              │
│   • owner_external_id       - Assigned user ID                      │
│   • owner_id (FK)           - Resolved CRMUser reference            │
│   • owner_name, owner_email - Cached details                        │
└─────────────────────────────────────────────────────────────────────┘
```

#### `crm_deal_participants` - Deal Relationships

```text
┌─────────────────────────────────────────────────────────────────────┐
│ crm_deal_participants                                                │
├─────────────────────────────────────────────────────────────────────┤
│ Purpose: Link deals to contacts, companies, and users               │
│                                                                      │
│ Identity: participant_id, external_id, integration_id               │
│                                                                      │
│ Linkage:                                                            │
│   • deal_id (FK)            - Parent deal                           │
│   • participant_type        - CONTACT, COMPANY, USER, TICKET        │
│   • relationship_role       - e.g., "Decision Maker", "Champion"    │
│                                                                      │
│ Related Entity:                                                      │
│   • related_external_id     - Provider ID of contact/company        │
│   • related_local_id        - Resolved internal UUID                │
│   • related_name            - Cached display name                   │
│   • related_email           - Cached email (for matching)           │
│                                                                      │
│ Note: Populated via HubSpot associations or Salesforce Contact Roles│
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Sync Services

Located in `src/service/integrations/crm/entities/`

### 3.1 Service Overview

| Service | Model | Responsibilities |
| --- | --- | --- |
| `DealSyncService` | `CRMDeal` | Deals + participants + stage history + enrichment |
| `ContactSyncService` | `CRMContact` | Contacts with owner resolution |
| `CompanySyncService` | `CRMCompany` | Companies/Accounts with owner resolution |
| `UserSyncService` | `CRMUser` | Internal users/owners |
| `DealTaskSyncService` | `CRMActivity` | Tasks, calls, meetings, tickets |
| `PipelineSyncService` | `CRMPipeline` + `CRMDealStage` | Pipeline metadata |
| `OwnerSyncService` | `CRMUser` | Legacy alias for users |

### 3.2 DealSyncService Flow

```text
DealSyncService.sync()
│
├── 1. Load existing deals by external_id
├── 2. Load reference data (pipelines, stages)
│
├── 3. For each raw deal:
│   ├── Get or create CRMDeal record
│   ├── _update_deal_record() - Transform fields
│   ├── Extract participants from associations
│   └── Extract stage history entries
│
├── 4. Enrich deals (DealEnrichmentService)
│   ├── Load CRMUser by owner_external_id → populate owner_name/email/id
│   ├── Load CRMContact by primary_contact_external_id
│   └── Load CRMCompany by company_external_id
│
├── 5. Commit deals
│
├── 6. Upsert participants (CRMDealParticipant)
│   └── Enrich with contact/company emails
│
└── 7. Upsert stage history (CRMDealStageHistory)
```

### 3.3 DealTaskSyncService Flow

```text
DealTaskSyncService.sync()
│
├── 1. Load existing tasks by external_id
├── 2. Load deal map for association resolution
│
├── 3. For each raw task:
│   ├── Get or create CRMActivity record
│   ├── _update_task_record() - Transform fields
│   │   ├── Map status: NOT_STARTED, IN_PROGRESS, COMPLETED
│   │   ├── Map type: TASK, TODO, CALL, MEETING, EMAIL
│   │   └── Map priority: LOW, MEDIUM, HIGH
│   │
│   └── Resolve deal_id from associations
│       ├── HubSpot: associations.deals.results[].id
│       └── Salesforce: WhatId (if prefix "006" = Opportunity)
│
├── 4. Enrich tasks (TaskEnrichmentService)
│   ├── Load CRMUser by owner_external_id → owner_name/email
│   ├── Load CRMDeal by deal_external_id → deal_id
│   ├── Load CRMContact by contact_external_id → contact_id
│   └── Load CRMCompany by company_external_id → company_id
│
└── 5. Commit tasks
```

---

## 4. Provider Registry

Located in `src/workers/integrations_worker/providers/registry.py`

### 4.1 Registry Structure

```python
_REGISTRY: dict[tuple[str, str], dict[str, FetchFn]] = {
    ("crm", "hubspot"): {
        "pipelines": hubspot_pipelines_fetch,
        "deals": hubspot_deals_fetch,
        "deal_tasks": hubspot_deal_tasks_fetch,
        "contacts": hubspot_contacts_fetch,
        "users": hubspot_users_fetch,
        "companies": hubspot_companies_fetch,
        "owners": hubspot_owners_fetch,
    },
    ("crm", "salesforce"): {
        "pipelines": salesforce_pipelines_fetch,
        "deals": salesforce_deals_fetch,
        "opportunities": salesforce_deals_fetch,  # Alias
        "deal_tasks": salesforce_tasks_fetch,
        "tickets": salesforce_tickets_fetch,      # Cases
        "contacts": salesforce_contacts_fetch,
        "users": salesforce_users_fetch,
        "accounts": salesforce_accounts_fetch,
        "companies": salesforce_accounts_fetch,   # Alias
        "leads": salesforce_leads_fetch,
    },
}
```

### 4.2 Fetcher Resolution

```text
get_fetcher(category, provider, data_type)
│
├── Look up (category, provider) → entry dict
├── Look up data_type in entry
│   ├── Direct match → return fetcher
│   └── Alias fallback (e.g., "tasks" → "deal_tasks")
└── Return fetcher function or None
```

---

## 5. HubSpot Integration (Reference Implementation)

### 5.1 Data Mapping

| HubSpot Entity | Our Table | Nango Sync Name |
| --- | --- | --- |
| Deals | `crm_deals` | `deals` |
| Contacts | `crm_contacts` | `contacts` |
| Companies | `crm_companies` | `companies` |
| Owners | `crm_users` | `owners` |
| Tasks | `crm_activities` | `deal_tasks` |
| Pipelines | `crm_pipelines` + `crm_deal_stages` | `pipelines` |

### 5.2 HubSpot Fetchers

Located in `src/workers/integrations_worker/providers/crm/hubspot/fetchers/`

| Fetcher | API Endpoint | Purpose |
| --- | --- | --- |
| `HubSpotDealFetcher` | `/crm/v3/objects/deals` | Fetch deals with properties |
| `HubSpotContactFetcher` | `/crm/v3/objects/contacts` | Fetch contacts |
| `HubSpotCompanyFetcher` | `/crm/v3/objects/companies` | Fetch companies |
| `HubSpotUserFetcher` | `/crm/v3/owners` | Fetch owners/users |
| `HubSpotDealTaskFetcher` | `/crm/v3/objects/tasks` | Fetch tasks |
| `HubSpotPipelineFetcher` | `/crm/v3/pipelines/deals` | Fetch pipeline definitions |

### 5.3 HubSpot Deal Sync Flow

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Nango Webhook: { sync_name: "deals", provider: "hubspot" }           │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Dispatcher: get_fetcher("crm", "hubspot", "deals")                   │
│           → HubSpotDealFetcher                                       │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│ HubSpotDealFetcher.fetch_all()                                       │
│                                                                      │
│ GET /crm/v3/objects/deals                                            │
│   ?properties=dealname,amount,closedate,pipeline,dealstage,...       │
│   &associations=contacts,companies                                   │
│                                                                      │
│ Returns:                                                             │
│ {                                                                    │
│   "id": "12345678",                                                 │
│   "properties": {                                                    │
│     "dealname": "Enterprise License",                                │
│     "amount": "50000",                                               │
│     "dealstage": "closedwon",                                       │
│     "pipeline": "default",                                           │
│     "hubspot_owner_id": "1234567"                                   │
│   },                                                                 │
│   "associations": {                                                  │
│     "contacts": { "results": [{"id": "101"}, {"id": "102"}] },      │
│     "companies": { "results": [{"id": "201"}] }                     │
│   }                                                                  │
│ }                                                                    │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│ DealSyncService.sync()                                               │
│                                                                      │
│ 1. Create/Update CRMDeal:                                           │
│    • external_id = "12345678"                                       │
│    • name = "Enterprise License"                                     │
│    • amount = 50000.00                                               │
│    • stage_external_id = "closedwon"                                │
│    • pipeline_external_id = "default"                               │
│    • owner_external_id = "1234567"                                  │
│                                                                      │
│ 2. Create CRMDealParticipants:                                      │
│    • (deal_id, CONTACT, "101")                                      │
│    • (deal_id, CONTACT, "102")                                      │
│    • (deal_id, COMPANY, "201")                                      │
│                                                                      │
│ 3. Enrichment (DealEnrichmentService):                              │
│    • Look up CRMUser where external_id = "1234567"                  │
│    • Populate: owner_name, owner_email, owner_id                    │
│    • Resolve pipeline_id, stage_id from external IDs                │
│                                                                      │
│ 4. Participant Enrichment:                                          │
│    • Look up CRMContact where external_id IN ("101", "102")         │
│    • Populate: related_email, related_name                          │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.4 HubSpot Task Sync Flow

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Nango Webhook: { sync_name: "deal_tasks", provider: "hubspot" }      │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│ HubSpotDealTaskFetcher.fetch_all()                                   │
│                                                                      │
│ GET /crm/v3/objects/tasks                                            │
│   ?properties=hs_task_subject,hs_task_body,hs_task_status,...       │
│   &associations=deals,contacts,companies                             │
│                                                                      │
│ Returns:                                                             │
│ {                                                                    │
│   "id": "98765",                                                    │
│   "properties": {                                                    │
│     "hs_task_subject": "Follow up call",                            │
│     "hs_task_status": "NOT_STARTED",                                │
│     "hs_task_type": "CALL",                                         │
│     "hs_task_priority": "HIGH",                                     │
│     "hubspot_owner_id": "1234567"                                   │
│   },                                                                 │
│   "associations": {                                                  │
│     "deals": { "results": [{"id": "12345678"}] }                    │
│   }                                                                  │
│ }                                                                    │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│ DealTaskSyncService.sync()                                           │
│                                                                      │
│ 1. Create/Update CRMActivity:                                       │
│    • external_id = "98765"                                          │
│    • subject = "Follow up call"                                     │
│    • status = NOT_STARTED                                           │
│    • activity_type = CALL                                           │
│    • priority = HIGH                                                │
│    • owner_external_id = "1234567"                                  │
│    • deal_external_id = "12345678"                                  │
│                                                                      │
│ 2. Enrichment (TaskEnrichmentService):                              │
│    • Look up CRMDeal where external_id = "12345678"                 │
│    • Populate: deal_id (FK reference)                               │
│    • Look up CRMUser where external_id = "1234567"                  │
│    • Populate: owner_name, owner_email, owner_id                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Salesforce Integration

### 6.1 Data Mapping

| Salesforce Object | Our Table | Nango Sync Name | SOQL Query |
| --- | --- | --- | --- |
| Opportunity | `crm_deals` | `opportunities` | `SELECT ... FROM Opportunity` |
| Contact | `crm_contacts` | `contacts` | `SELECT ... FROM Contact` |
| Account | `crm_companies` | `accounts` | `SELECT ... FROM Account` |
| Lead | (separate) | `leads` | `SELECT ... FROM Lead` |
| User | `crm_users` | *(auto-synced)* | `SELECT ... FROM User` |
| Task | `crm_activities` | `tasks` | `SELECT ... FROM Task` |
| Case | `crm_activities` | `tickets` | `SELECT ... FROM Case` |

### 6.2 Salesforce Fetchers

Located in `src/workers/integrations_worker/providers/crm/salesforce/fetchers/`

| Fetcher | SOQL Table | Key Fields |
| --- | --- | --- |
| `SalesforceOpportunityFetcher` | `Opportunity` | + `Owner.Name`, `Owner.Email` |
| `SalesforceContactFetcher` | `Contact` | Standard contact fields |
| `SalesforceAccountFetcher` | `Account` | Standard account fields |
| `SalesforceUserFetcher` | `User` | Internal users |
| `SalesforceTaskFetcher` | `Task` | `WhatId` (→ Opportunity), `WhoId` (→ Contact) |
| `SalesforceCaseFetcher` | `Case` | Support tickets |
| `SalesforcePipelineFetcher` | `OpportunityStage` | Pipeline stages |

### 6.3 Salesforce ID Prefixes

Salesforce uses 3-character prefixes to identify object types:

| Prefix | Object | Example |
| --- | --- | --- |
| `006` | Opportunity | `006xxx...` → Deal |
| `001` | Account | `001xxx...` → Company |
| `003` | Contact | `003xxx...` → Contact |
| `00Q` | Lead | `00Qxxx...` → Lead |
| `005` | User | `005xxx...` → User/Owner |
| `500` | Case | `500xxx...` → Ticket |

**Used for**: Resolving polymorphic fields like `WhatId` and `WhoId` on Tasks.

### 6.4 Salesforce Users = Owners

**Important**: In Salesforce, **Users ARE Owners** - there is no separate "owner" entity.

- The `OwnerId` field on any Salesforce object (Account, Contact, Opportunity, etc.) references a `User` record
- User ID prefix is `005` (same as Owner ID)
- We store `OwnerId` → `owner_external_id` and resolve it to `owner_id` FK via `crm_users`

This is different from HubSpot which has a separate `/owners` endpoint, but the data model is effectively the same - both resolve to `crm_users`.

### 6.5 Salesforce Owner Resolution

Unlike HubSpot (which has a dedicated `/owners` endpoint), Salesforce embeds owner details via SOQL relationship queries:

```sql
SELECT Id, Name, Amount, StageName, OwnerId,
       Owner.Id, Owner.Name, Owner.Email, Owner.FirstName, Owner.LastName
FROM Opportunity
```

This returns:

```json
{
  "Id": "006xxx",
  "Name": "Big Deal",
  "OwnerId": "005xxx",
  "Owner": {
    "Id": "005xxx",
    "Name": "John Smith",
    "Email": "john@company.com"
  }
}
```

### 6.5 Salesforce Users Auto-Sync

Since Nango doesn't have a built-in `users` sync for Salesforce, users are automatically synced alongside other CRM syncs:

```text
Salesforce CRM Sync (opportunities/contacts/accounts)
│
├── Main sync completes successfully
│
└── _sync_salesforce_users_alongside_crm()
    │
    ├── Check Redis lock (5-minute TTL)
    │   └── Skip if already synced recently
    │
    ├── SalesforceUserFetcher.fetch_all()
    │   └── SOQL: SELECT Id, Name, Email, IsActive, ... FROM User
    │
    └── UserSyncService.sync()
        └── Upsert to crm_users
```

### 6.6 Salesforce Task Resolution

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Salesforce Task Record                                               │
│                                                                      │
│ {                                                                    │
│   "Id": "00Txxx",                                                   │
│   "Subject": "Follow up call",                                      │
│   "WhatId": "006xxx",     ← Polymorphic! Could be Opportunity/Account│
│   "WhoId": "003xxx",      ← Contact/Lead                            │
│   "OwnerId": "005xxx",                                              │
│   "Owner": { "Name": "John", "Email": "john@co.com" }               │
│ }                                                                    │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│ DealTaskSyncService._update_task_record()                            │
│                                                                      │
│ WhatId Resolution (Salesforce ID prefix detection):                  │
│   if WhatId.startswith("006"):   # Opportunity                       │
│       deal_external_id = WhatId                                      │
│   elif WhatId.startswith("001"): # Account                           │
│       company_external_id = WhatId                                   │
│                                                                      │
│ WhoId Resolution:                                                    │
│   if WhoId.startswith("003"):    # Contact                           │
│       contact_external_id = WhoId                                    │
│   elif WhoId.startswith("00Q"):  # Lead                              │
│       (stored for future use)                                        │
└──────────────────────────────────────────────────────────────────────┘
```

### 6.7 Salesforce vs HubSpot: Key Differences

| Aspect | HubSpot | Salesforce |
| --- | --- | --- |
| **API Style** | REST with pagination | SOQL queries |
| **Users/Owners** | Dedicated `/owners` endpoint | Query `User` object |
| **Tasks** | `tasks` object | `Task` object (activities) |
| **Tickets** | `tickets` object | `Case` object |
| **Associations** | `associations` block in response | `WhatId`/`WhoId` polymorphic fields |
| **Object Types** | Explicit in associations | ID prefix detection |
| **Owner Details** | Requires separate users sync | Embedded via relationship query |

---

## 7. Post-Sync Enrichment

### 7.1 Enrichment Services

| Service | Purpose | When Triggered |
| --- | --- | --- |
| `DealEnrichmentService` | Populate deal owner/contact/company details + owner_id FK | After deal sync |
| `TaskEnrichmentService` | Populate activity owner/deal/contact/company + owner_id FK | After activity sync |
| `OwnerEnrichmentService` | Resolve owner_id FK for companies/contacts/deals | After users sync |

### 7.2 Enrichment Flow

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Dispatcher: Post-Sync Triggers                                       │
│ (_trigger_deal_tasks_on_initial_sync)                               │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
    ┌────────────────────┼────────────────────┐
    │                    │                    │
    ▼                    ▼                    ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐
│ contacts    │  │ companies   │  │ users               │
│ sync        │  │ sync        │  │ sync                │
└──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘
       │                │                     │
       └────────────────┴─────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Re-Enrich Participants (_enrich_participants_after_crm_sync)         │
│                                                                      │
│ • Look up CRMContact by external_id                                 │
│ • Populate related_email, related_name on CRMDealParticipant        │
│ • Enables deal↔email/meeting matching                               │
└──────────────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Re-Enrich Activities (_enrich_activities_after_crm_sync)             │
│                                                                      │
│ • Look up CRMDeal by deal_external_id → populate deal_id            │
│ • Look up CRMContact by contact_external_id → populate contact_id   │
│ • Look up CRMCompany by company_external_id → populate company_id   │
│ • Look up CRMUser by owner_external_id → populate owner details     │
│   + owner_id FK                                                      │
└──────────────────────────────────────────────────────────────────────┘
                        │
                        ▼ (Only after users sync)
┌──────────────────────────────────────────────────────────────────────┐
│ Re-Enrich Owner IDs (_enrich_owners_after_users_sync)               │
│                                                                      │
│ • Look up CRMUser by owner_external_id                              │
│ • Update CRMCompany.owner_id FK                                     │
│ • Update CRMContact.owner_id FK                                     │
│ • Update CRMDeal.owner_id FK                                        │
│                                                                      │
│ NOTE: In Salesforce (and HubSpot), Users ARE Owners - no separate   │
│ owner concept. OwnerId on any object points to a User record.       │
└──────────────────────────────────────────────────────────────────────┘
```

### 7.3 Why Enrichment is Needed

**Problem**: CRM webhooks arrive independently and in any order.

```text
Timeline Example:
─────────────────────────────────────────────────────────────────────
T1: deals sync     → CRMDeal created with owner_external_id="12345"
                     but owner_name=NULL, owner_id=NULL (users not synced yet)
                     
T2: contacts sync  → CRMContact created
                     CRMDealParticipant has related_external_id
                     but related_email=NULL (contact didn't exist at T1)
                     
T3: users sync     → CRMUser created with external_id="12345"
                     NOW we can enrich deal with owner details
                     
T4: Enrichment     → Update CRMDeal.owner_name from CRMUser
                     Update CRMDeal.owner_id FK from CRMUser.user_id
                     Update CRMCompany.owner_id, CRMContact.owner_id
                     Update CRMDealParticipant.related_email from CRMContact
─────────────────────────────────────────────────────────────────────
```

---

## 8. Data Flow Diagrams

### 8.1 Complete CRM Sync Flow

```text
┌────────────────────────────────────────────────────────────────────────┐
│                              NANGO                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│  │   HubSpot    │  │  Salesforce  │  │   Other      │                  │
│  │   OAuth      │  │    OAuth     │  │   CRMs       │                  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                  │
│         │                 │                 │                          │
│         └─────────────────┼─────────────────┘                          │
│                           │                                            │
│                    Webhook POST                                        │
└───────────────────────────┼────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ WEBHOOK HANDLER (nango_webhook_service.py)                             │
│                                                                        │
│ 1. Verify HMAC signature                                               │
│ 2. Extract: connection_id, sync_name, provider                         │
│ 3. Look up organisation_id, integration_id                             │
│ 4. Enqueue Celery task                                                 │
└───────────────────────────┼────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ CELERY WORKER (sync_tasks.py)                                          │
│                                                                        │
│ Task: integrations.sync_integration_data                               │
│ Calls: execute_sync(request)                                           │
└───────────────────────────┼────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ DISPATCHER (dispatcher.py)                                             │
│                                                                        │
│ 1. get_fetcher(category, provider, sync_name)                          │
│    └→ Returns provider-specific fetcher function                       │
│                                                                        │
│ 2. fetcher(nango, connection_id, ...) → raw records                    │
│    ├→ HubSpot: REST API calls                                          │
│    └→ Salesforce: SOQL queries via Nango proxy                         │
│                                                                        │
│ 3. _dispatch_category("crm", ...) → _handle_crm()                      │
└───────────────────────────┼────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ CRM ADAPTER (crm_adapter.py)                                           │
│                                                                        │
│ sync_crm_data(service, integration, data_type, records, provider)      │
│ └→ service.sync(integration, records, provider)                        │
└───────────────────────────┼────────────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            │               │               │
            ▼               ▼               ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ DealSync      │  │ ContactSync   │  │ TaskSync      │
│ Service       │  │ Service       │  │ Service       │
│               │  │               │  │               │
│ • Transform   │  │ • Transform   │  │ • Transform   │
│ • Upsert      │  │ • Upsert      │  │ • Upsert      │
│ • Participants│  │ • Enrich      │  │ • Enrich      │
│ • History     │  │               │  │               │
│ • Enrich      │  │               │  │               │
└───────┬───────┘  └───────┬───────┘  └───────┬───────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────────────┐
│ DATABASE (Organisation-specific PostgreSQL)                            │
│                                                                        │
│ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐       │
│ │ crm_deals   │ │crm_contacts │ │crm_companies│ │ crm_users   │       │
│ └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘       │
│                                                                        │
│ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐       │
│ │crm_activities│ │crm_pipelines│ │crm_stages   │ │crm_participants│   │
│ └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘       │
└────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Entity Relationship Diagram

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        CRM DATA MODEL                                   │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  CRMPipeline │◄────────│ CRMDealStage │         │   CRMUser    │
│              │ 1    n  │              │         │              │
│ • pipeline_id│         │ • stage_id   │         │ • user_id    │
│ • name       │         │ • name       │         │ • email      │
│ • is_default │         │ • probability│         │ • full_name  │
└──────────────┘         └──────────────┘         │ • is_active  │
                                │                 └──────┬───────┘
                                │                        │
                                │ FK                     │ FK (owner_id)
                                ▼                        │
                         ┌──────────────┐               │
                         │   CRMDeal    │◄──────────────┘
                         │              │
                         │ • deal_id    │
                         │ • name       │
                         │ • amount     │
                         │ • stage_id   │────────────────────┐
                         │ • pipeline_id│                    │
                         │ • owner_id   │                    │
                         └──────┬───────┘                    │
                                │                            │
                                │ FK                         │
                                ▼                            │
                   ┌────────────────────────┐               │
                   │  CRMDealParticipant    │               │
                   │                        │               │
                   │ • participant_id       │               │
                   │ • deal_id (FK)         │               │
                   │ • participant_type     │               │
                   │ • related_external_id  │               │
                   │ • related_email        │               │
                   └────────────────────────┘               │
                                                            │
┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  CRMContact  │    │  CRMCompany  │    │ CRMActivity  │◄──┘
│              │    │              │    │              │ FK (deal_id)
│ • contact_id │    │ • company_id │    │ • activity_id│
│ • email      │    │ • name       │    │ • subject    │
│ • full_name  │    │ • domain     │    │ • status     │
│ • owner_id   │    │ • owner_id   │    │ • deal_id    │
└──────────────┘    └──────────────┘    │ • contact_id │
                                        │ • company_id │
                                        │ • owner_id   │
                                        └──────────────┘

Legend:
  ─────  Reference (FK)
  ◄────  Direction of FK
  1   n  Cardinality
```

---

## Quick Reference

### Sync Names → Services

| Nango sync_name | Service | Model | Table |
| --- | --- | --- | --- |
| `deals` | `DealSyncService` | `CRMDeal` | `crm_deals` |
| `opportunities` | `DealSyncService` | `CRMDeal` | `crm_deals` |
| `contacts` | `ContactSyncService` | `CRMContact` | `crm_contacts` |
| `companies` | `CompanySyncService` | `CRMCompany` | `crm_companies` |
| `accounts` | `CompanySyncService` | `CRMCompany` | `crm_companies` |
| `users` | `UserSyncService` | `CRMUser` | `crm_users` |
| `owners` | `OwnerSyncService` | `CRMUser` | `crm_users` |
| `deal_tasks` | `DealTaskSyncService` | `CRMActivity` | `crm_activities` |
| `tasks` | `DealTaskSyncService` | `CRMActivity` | `crm_activities` |
| `tickets` | `DealTaskSyncService` | `CRMActivity` | `crm_activities` |
| `pipelines` | `PipelineSyncService` | `CRMPipeline` | `crm_pipelines` |

### Key File Locations

```text
src/
├── domain/models/organisation_tables/sync/crm/
│   ├── deals.py          # CRMDeal, CRMPipeline, CRMDealStage, CRMDealParticipant, CRMDealStageHistory
│   ├── contacts.py       # CRMContact
│   ├── companies.py      # CRMCompany
│   ├── users.py          # CRMUser
│   ├── activities.py     # CRMActivity (CRMDealTask)
│   └── associations.py   # CRMObjectAssociation
│
├── service/integrations/crm/entities/
│   ├── deal_sync_service.py
│   ├── deal_enrichment_service.py
│   ├── contact_sync_service.py
│   ├── company_sync_service.py
│   ├── user_sync_service.py
│   ├── deal_task_sync_service.py
│   ├── task_enrichment_service.py
│   └── pipeline_sync_service.py
│
└── workers/integrations_worker/
    ├── providers/
    │   ├── registry.py                    # Provider → Fetcher mapping
    │   └── crm/
    │       ├── hubspot/
    │       │   ├── __init__.py            # Canonical transformers
    │       │   └── fetchers/              # API fetchers
    │       └── salesforce/
    │           ├── __init__.py            # Canonical transformers
    │           └── fetchers/              # SOQL fetchers
    └── services/
        └── dispatcher.py                  # Sync orchestration
```

---

*Document maintained by: Engineering Team*  
*Last reviewed: December 17, 2025*
