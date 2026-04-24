# CRM Initial Sync (HubSpot + Salesforce)

This document describes the **live CRM initial-sync flow** that runs after a HubSpot or Salesforce OAuth connection succeeds.

## What Runs After Connect

`InitialSyncService` does two things:

1. Stamps queued metadata into `KnowledgeIntegration.connection_metadata`
2. Dispatches the provider-specific Celery chain

Only the chain terminators write `initial_sync_status`.

## Chain Order

### HubSpot

`schema -> pipelines -> users -> companies -> contacts -> deals -> finalize`

### Salesforce

`schema -> pipelines -> users -> companies -> contacts -> deals -> finalize`

Code:

- `src/service/integrations/initial_sync_service.py`
- `src/service/integrations/crm/orchestration/hubspot_initial_sync_chain.py`
- `src/service/integrations/crm/orchestration/salesforce_initial_sync_chain.py`

## What Gets Captured

### Connection metadata

Queued state after connect:

- HubSpot:
  - `schema_sync.deals.status = "queued"`
  - `record_sync.pipelines/users/companies/contacts/deals.status = "queued"`
- Salesforce:
  - `schema_sync.opportunities.status = "queued"`
  - `record_sync.pipelines/users/companies/contacts/deals.status = "queued"`

Per-phase tasks update:

- `connection_metadata.schema_sync.<object>`
- `connection_metadata.record_sync.<phase>`

These entries hold status, last synced timestamp, object-specific counters, and error details.

### Persisted CRM tables

HubSpot currently writes:

- `crm_custom_field_definitions`
- `crm_pipelines`
- `crm_deal_stages`
- `crm_users`
- `crm_companies`
- `crm_contacts`
- `crm_deals`
- `crm_deal_stage_history`
- `crm_deal_participants`
- `crm_object_associations`

Salesforce currently writes:

- `crm_custom_field_definitions` for Opportunity schema
- `crm_pipelines`
- `crm_deal_stages`
- `crm_users`
- `crm_companies`
- `crm_contacts`
- `crm_deals`
- `crm_deal_stage_history`
- `crm_deal_participants`
- `crm_object_associations`

Rows are provider-isolated by `integration_id`.

## How a Phase Runs

Each CRM phase follows the same shape:

1. Load integration + token info in a short org DB session
2. Release the session
3. Fetch from the provider API
4. Normalize in pure Python
5. Open a fresh org DB session
6. Write/upsert rows and any deal FK backfill
7. Commit sync history + phase metadata

This keeps DB sessions out of long external API calls.

## Current Capture Boundaries

- HubSpot deals are the only live CRM fact-table sync today.
- Salesforce is live through **deals**.
- Secondary relationship modeling like `AccountContactRelation` is deferred.

## Task Families

CRM work here is not the generic three-tier email/calendar pattern.

- Post-connect chains are built from provider-specific Tier 3 tasks
- HubSpot also has `sync_hubspot_deal_schema_all_orgs` for the 14-day schema refresh
- Salesforce currently has `sync_salesforce_opportunity_schema_all_orgs` for Opportunity schema refresh

Entry point:

- `src/workers/integrations_worker/tasks/crm/`
