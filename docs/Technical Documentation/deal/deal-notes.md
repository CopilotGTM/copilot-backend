# Deal Notes (HubSpot + Salesforce)

This document describes how we fetch **CRM notes** that are attached to **deals** (HubSpot) and **opportunities** (Salesforce), and how we store them.

## Overview

- Notes are treated as **CRM activities** in our canonical sync model.
- Notes are fetched from the provider APIs, normalized into the existing “activity/task” payload shape, and persisted into the organisation DB.

## What We Fetch

### HubSpot (Linking)

- Source API: `GET /crm/v3/objects/notes`
- Fields we request (properties):
  - `hs_note_body` (note content)
  - `hs_timestamp`, `hs_createdate`, `hs_lastmodifieddate`
  - `hubspot_owner_id`, `hubspot_owner_email`, `hubspot_owner_name`
  - `hs_object_id`
- Associations requested:
  - `deals`, `contacts`, `companies`

Implementation:

- Fetcher: `src/workers/integrations_worker/providers/crm/hubspot/fetchers/note_fetcher.py`
- Wrapper/normalization: `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` (`fetch_notes`)

### Salesforce (Linking)

- Source API: Salesforce SOQL query against the legacy `Note` object
- Fields selected:
  - `Id`, `Title`, `Body`, `OwnerId`, `ParentId`, `CreatedDate`, `LastModifiedDate`, `IsDeleted`
  - Owner relationship fields where available (`Owner.Name`, `Owner.Email`, etc.)

Implementation:

- Fetcher: `src/workers/integrations_worker/providers/crm/salesforce/fetchers/note_fetcher.py`
- Wrapper/normalization: `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` (`fetch_notes`)

Note:

- Salesforce also supports Enhanced Notes (`ContentNote` / `ContentDocumentLink`). This implementation currently targets the legacy `Note` object.

## How Notes Are “Fetched With” Deals / Opportunities

Nango may not emit a separate `syncName=notes` webhook.

To ensure notes are fetched alongside deal/opportunity sync, the integrations worker **enqueues a second internal sync** after a successful:

- `syncName=deals` (HubSpot)
- `syncName=opportunities` (Salesforce)

Implementation:

- Trigger: `src/workers/integrations_worker/services/dispatcher.py` (`_trigger_crm_notes_after_deals_sync`)
- The trigger enqueues the existing Celery task `integrations.sync_integration_data` with `sync_name="notes"`.

## Where Notes Are Stored

Notes are stored in the organisation database in:

- Table: `crm_activities`
- Model: `CRMActivity` (backwards-compat alias: `CRMDealTask = CRMActivity`)

Key columns used:

- `activity_type` (set to `NOTE` via normalization)
- `subject` (best-effort mapping, e.g., HubSpot notes may have no subject)
- `body` (note content)
- `deal_external_id` and `deal_id` (if linked to a deal/opportunity)
- `contact_external_id`, `company_external_id` (if available)
- `owner_external_id`, `owner_name`, `owner_email`
- `properties` (provider properties, includes raw/unmapped values)
- `data_payload` (raw provider payload for debugging)

Persistence path:

- Sync service: `src/service/integrations/crm/entities/deal_task_sync_service.py` (stores activities into `crm_activities`)

## How Notes Link to Deals / Opportunities

### HubSpot

- HubSpot notes include associations in the response.
- We resolve the first associated deal (if present) and set:
  - `deal_external_id`
  - `deal_id` (resolved from the deal map if the deal already exists)

### Salesforce

- Salesforce notes have `ParentId`.
- We map `ParentId` into the activity linkage field `what_id`.
- During sync, we link notes to opportunities by Salesforce ID prefix:
  - `006...` = Opportunity → `deal_external_id`

## Operational Notes / Caveats

- Incremental filtering:
  - HubSpot notes: the fetcher attempts to filter by `hs_lastmodifieddate` (best-effort). Exact semantics may depend on the proxy/provider behavior.
  - Salesforce notes: incremental filtering uses `LastModifiedDate >= last_synced_at`.
- Attachments:
  - HubSpot notes can reference attachments via `hs_attachment_ids`. We currently retain this in `properties`/`data_payload` rather than syncing attachments into a dedicated table.
