# CRM Integration Flow Charts

This document visualizes the internal flow of CRM integration services, showing how data moves from the provider (via Nango) through our internal services and enrichment processes.

## 1. HubSpot Deal Sync Flow

This flow illustrates how HubSpot deals are fetched, transformed, and enriched.

```mermaid
sequenceDiagram
    autonumber
    participant Nango as Nango Webhook
    participant Dispatcher as Dispatcher
    participant Fetcher as HubSpotDealFetcher
    participant Adapter as CRMAdapter
    participant Service as DealSyncService
    participant DB as Database
    participant Enricher as DealEnrichmentService

    Nango->>Dispatcher: Trigger Sync (deals)
    Dispatcher->>Fetcher: fetch_all()
    Fetcher-->>Dispatcher: Raw Deal Records
    Dispatcher->>Adapter: sync_crm_data()
    Adapter->>Service: sync()
    
    rect rgb(240, 248, 255)
        Note over Service, DB: Initialization
        Service->>DB: Load Existing Deals (by external_id)
        Service->>DB: Load Pipeline & Stage Maps
    end
    
    rect rgb(255, 250, 240)
        Note over Service: Processing Loop
        loop For Each Deal
            Service->>Service: _update_deal_record()
            Note right of Service: Transform properties<br/>Map pipeline/stage
            Service->>Service: _extract_participants()
            Service->>Service: _extract_stage_history()
        end
    end
    
    rect rgb(240, 255, 240)
        Note over Service, Enricher: Deal Enrichment
        Service->>Enricher: enrich_deals()
        Enricher->>DB: Load Users (Owners)
        Enricher->>DB: Load Contacts
        Enricher->>DB: Load Companies
        Enricher-->>Service: Enriched Deals
        Note right of Service: Populates owner_name, owner_email,<br/>owner_id, etc.
    end
    
    Service->>DB: Commit Deals
    
    rect rgb(255, 240, 245)
        Note over Service, Enricher: Related Entities
        Service->>Service: _upsert_participants()
        Service->>Enricher: enrich_participants()
        Enricher->>DB: Load Contact/Company details
        Enricher-->>Service: Enriched Participants
        Service->>Service: _upsert_stage_history()
    end
    
    Service->>DB: Commit All
```

## 2. HubSpot Contact Sync Flow

This flow shows the synchronization of contact records.

```mermaid
sequenceDiagram
    autonumber
    participant Nango as Nango Webhook
    participant Dispatcher as Dispatcher
    participant Fetcher as HubSpotContactFetcher
    participant Adapter as CRMAdapter
    participant Service as ContactSyncService
    participant DB as Database

    Nango->>Dispatcher: Trigger Sync (contacts)
    Dispatcher->>Fetcher: fetch_all()
    Fetcher-->>Dispatcher: Raw Contact Records
    Dispatcher->>Adapter: sync_crm_data()
    Adapter->>Service: sync()
    
    rect rgb(240, 248, 255)
        Note over Service, DB: Initialization
        Service->>DB: Load Existing Contacts
        Service->>DB: Pre-load Referenced Companies
        Note right of Service: Optimizes company linking
    end
    
    rect rgb(255, 250, 240)
        Note over Service: Processing Loop
        loop For Each Contact
            Service->>Service: _extract_contact_company_external_ids()
            Service->>Service: Update Record
            Note right of Service: Map properties<br/>Link to Company
        end
    end
    
    Service->>DB: Commit Contacts
```

## 3. HubSpot Task Sync Flow

This flow details how tasks (calls, meetings, emails) are synced and linked to deals.

```mermaid
sequenceDiagram
    autonumber
    participant Nango as Nango Webhook
    participant Dispatcher as Dispatcher
    participant Fetcher as HubSpotDealTaskFetcher
    participant Adapter as CRMAdapter
    participant Service as DealTaskSyncService
    participant DB as Database
    participant Enricher as TaskEnrichmentService

    Nango->>Dispatcher: Trigger Sync (deal_tasks)
    Dispatcher->>Fetcher: fetch_all()
    Fetcher-->>Dispatcher: Raw Task Records
    Dispatcher->>Adapter: sync_crm_data()
    Adapter->>Service: sync()
    
    rect rgb(240, 248, 255)
        Note over Service, DB: Initialization
        Service->>DB: Load Existing Tasks
        Service->>DB: Load Deal Map
        Note right of Service: Needed to link tasks to deals
    end
    
    rect rgb(255, 250, 240)
        Note over Service: Processing Loop
        loop For Each Task
            Service->>Service: _update_task_record()
            Note right of Service: Map status, type, priority<br/>Resolve Deal ID
        end
    end
    
    Service->>DB: Commit Tasks
    
    rect rgb(240, 255, 240)
        Note over Service, Enricher: Task Enrichment
        Service->>Enricher: enrich_tasks_for_integration()
        Enricher->>DB: Load Users (Owners)
        Enricher->>DB: Load Deals
        Enricher->>DB: Load Contacts
        Enricher->>DB: Load Companies
        Enricher-->>Service: Enriched Tasks
        Note right of Service: Populates owner details,<br/>links to related objects
    end
    
    Service->>DB: Commit All
```

## 4. Salesforce Deal (Opportunity) Sync Flow

Salesforce follows a similar pattern but uses SOQL queries and handles "Opportunities".

```mermaid
sequenceDiagram
    autonumber
    participant Nango as Nango Webhook
    participant Dispatcher as Dispatcher
    participant Fetcher as SalesforceOpportunityFetcher
    participant Adapter as CRMAdapter
    participant Service as DealSyncService
    participant DB as Database
    participant Enricher as DealEnrichmentService

    Nango->>Dispatcher: Trigger Sync (opportunities)
    Dispatcher->>Fetcher: fetch_all()
    Fetcher->>Nango: SOQL Query (Opportunity)
    Note right of Fetcher: SELECT Id, Name, Amount, StageName...
    Nango-->>Fetcher: Records
    Fetcher-->>Dispatcher: Raw Opportunity Records
    Dispatcher->>Adapter: sync_crm_data()
    Adapter->>Service: sync()
    
    rect rgb(240, 248, 255)
        Note over Service, DB: Initialization
        Service->>DB: Load Existing Deals
        Service->>DB: Load Pipeline/Stage Maps
    end
    
    rect rgb(255, 250, 240)
        Note over Service: Processing Loop
        loop For Each Opportunity
            Service->>Service: _update_deal_record()
            Note right of Service: Map Salesforce StageName<br/>to internal Stage ID
            Service->>Service: _extract_participants()
            Service->>Service: _extract_stage_history()
        end
    end
    
    rect rgb(240, 255, 240)
        Note over Service, Enricher: Enrichment
        Service->>Enricher: enrich_deals()
        Note right of Service: Resolves OwnerId (User)<br/>and AccountId (Company)
    end
    
    Service->>DB: Commit Deals
    
    rect rgb(255, 240, 245)
        Note over Service, Enricher: Related Entities
        Service->>Service: _upsert_participants()
        Service->>Enricher: enrich_participants()
        Service->>Service: _upsert_stage_history()
    end
    
    Service->>DB: Commit All
```

## 5. Salesforce Contact Sync Flow

```mermaid
sequenceDiagram
    autonumber
    participant Nango as Nango Webhook
    participant Dispatcher as Dispatcher
    participant Fetcher as SalesforceContactFetcher
    participant Adapter as CRMAdapter
    participant Service as ContactSyncService
    participant DB as Database

    Nango->>Dispatcher: Trigger Sync (contacts)
    Dispatcher->>Fetcher: fetch_all()
    Fetcher->>Nango: SOQL Query (Contact)
    Nango-->>Fetcher: Records
    Dispatcher->>Adapter: sync_crm_data()
    Adapter->>Service: sync()
    
    rect rgb(240, 248, 255)
        Note over Service, DB: Initialization
        Service->>DB: Load Existing Contacts
        Service->>DB: Pre-load Referenced Accounts (Companies)
    end
    
    rect rgb(255, 250, 240)
        Note over Service: Processing Loop
        loop For Each Contact
            Service->>Service: _extract_contact_company_external_ids()
            Service->>Service: Update Record
            Note right of Service: Link to Account (Company)
        end
    end
    
    Service->>DB: Commit Contacts
```

## 6. Salesforce Task Sync Flow

Salesforce tasks use polymorphic `WhatId` and `WhoId` fields which are resolved during sync.

```mermaid
sequenceDiagram
    autonumber
    participant Nango as Nango Webhook
    participant Dispatcher as Dispatcher
    participant Fetcher as SalesforceTaskFetcher
    participant Adapter as CRMAdapter
    participant Service as DealTaskSyncService
    participant DB as Database
    participant Enricher as TaskEnrichmentService

    Nango->>Dispatcher: Trigger Sync (tasks)
    Dispatcher->>Fetcher: fetch_all()
    Fetcher->>Nango: SOQL Query (Task)
    Nango-->>Fetcher: Records
    Dispatcher->>Adapter: sync_crm_data()
    Adapter->>Service: sync()
    
    rect rgb(240, 248, 255)
        Note over Service, DB: Initialization
        Service->>DB: Load Existing Tasks
        Service->>DB: Load Deal Map
    end
    
    rect rgb(255, 250, 240)
        Note over Service: Processing Loop
        loop For Each Task
            Service->>Service: _update_task_record()
            Note right of Service: Check WhatId prefix (006=Opp)<br/>Check WhoId prefix (003=Contact)
        end
    end
    
    Service->>DB: Commit Tasks
    
    rect rgb(240, 255, 240)
        Note over Service, Enricher: Task Enrichment
        Service->>Enricher: enrich_tasks_for_integration()
        Enricher->>DB: Load Users/Deals/Contacts
        Enricher-->>Service: Enriched Tasks
    end
    
    Service->>DB: Commit All
```
