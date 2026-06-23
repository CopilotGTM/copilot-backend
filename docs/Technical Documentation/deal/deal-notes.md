# Deal Notes (HubSpot + Salesforce)

This document describes how we fetch **CRM notes** that are attached to **deals** (HubSpot) and **opportunities** (Salesforce), and how we store them.

## Overview

- Notes are canonical CRM records in `crm_notes`.
- Notes are fetched from provider APIs, normalized into note-shaped payloads, and projected into `crm_activities` only for feed display.

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

- Table: `crm_notes`
- Model: `CRMNote`

Key columns used:

- `title` (best-effort mapping, e.g., HubSpot notes may have no subject)
- `body_plain`, `body_html`, `body_markdown` (note content)
- `deal_id` (if linked to a deal/opportunity)
- `contact_id`, `company_id` (if available)
- `owner_user_id`, `owner_name`, `owner_email`
- `properties` (provider properties, includes raw/unmapped values)
- Provider identity and raw payloads are stored in `crm_source_object_mappings`.

Persistence path:

- Sync service: `src/service/integrations/crm/entities/notes_sync_service.py` (stores notes into `crm_notes`)
- Projection service: `src/service/crm/activity_feed_projection_service.py` (writes feed rows into `crm_activities` and `crm_activity_links`)

## How Notes Link to Deals / Opportunities

### HubSpot

- HubSpot notes include associations in the response.
- We resolve the first associated deal (if present) through source mappings and set `deal_id`.

### Salesforce

- Salesforce notes have `ParentId`.
- We map `ParentId` into note linkage/source metadata.
- During sync, opportunity `ParentId` values are resolved through `crm_source_object_mappings`
  before setting local `deal_id`.

## Operational Notes / Caveats

- Incremental filtering:
  - HubSpot notes: the fetcher attempts to filter by `hs_lastmodifieddate` (best-effort). Exact semantics may depend on the proxy/provider behavior.
  - Salesforce notes: incremental filtering uses `LastModifiedDate >= last_synced_at`.
- Attachments:
  - HubSpot notes can reference attachments via `hs_attachment_ids`. We currently retain this in `properties` or source mapping payload metadata rather than syncing attachments into a dedicated table.
