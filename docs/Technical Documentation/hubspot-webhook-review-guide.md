# HubSpot Webhook Review Guide

Use this order to review the flow quickly.

## 1. Public webhook entry

File: `src/api/routers/integrations/hubspot_webhook_routes.py`

Check:
- Verifies HubSpot signature before parsing.
- Parses one or many webhook events.
- Drops unsupported subscriptions.
- Deduplicates events.
- Queues supported events to the integrations worker.

## 2. Worker entry

File: `src/workers/integrations_worker/tasks/crm/hubspot/webhooks/event_task.py`

Check:
- Rebuilds `HubSpotWebhookEvent` from task args.
- Sends the event to the HubSpot dispatcher.

## 3. Provider dispatcher

Files:
- `src/service/integrations/webhooks/common/registry.py`
- `src/service/integrations/webhooks/hubspot/dispatcher.py`
- `src/service/integrations/webhooks/hubspot/subscriptions.py`

Check:
- `common/registry.py` is provider-agnostic.
- `hubspot/subscriptions.py` wires subscription type to handler.
- Future Salesforce/webhook providers should reuse the common registry pattern.

## 4. HubSpot deal handler

File: `src/service/integrations/webhooks/hubspot/deals/handler.py`

Check:
- `deal.creation` and `deal.propertyChange` fetch the full deal from HubSpot.
- `deal.deletion` soft-deletes the local deal.
- Portal id is resolved to matching org integrations before any data write.

## 5. Portal-to-org routing

File: `src/service/integrations/webhooks/hubspot/portal_resolver.py`

Check:
- Scans active approved orgs only.
- Matches HubSpot integrations by `connection_metadata.hubspot_portal_id`.
- Returns only org/integration ids for matched portals.

## 6. Single deal fetch

File: `src/workers/integrations_worker/providers/crm/hubspot/single_deal_fetcher.py`

Check:
- Fetches one full HubSpot deal by object id.
- Uses the same properties needed by the normal sync pipeline.
- Returns `None` for 404 so the handler can soft-delete locally.

## 7. Existing sync pipeline reuse

Files:
- `src/service/integrations/crm/records/hubspot/deals/deal_sync_service.py`
- `src/service/integrations/crm/records/hubspot/deals/deal_writer.py`

Check:
- Webhooks reuse `_persist_raw_deals(...)` instead of duplicating sync logic.
- Deal writer reactivates soft-deleted rows on upsert.
- Full sync and webhook sync now share the same normalization and writer path.

## 8. Tests

Files:
- `tests/api/routers/integrations/test_hubspot_webhook_routes.py`
- `tests/service/integrations/webhooks/hubspot/test_dispatcher.py`
- `tests/service/integrations/webhooks/hubspot/deals/test_handler.py`
- `tests/service/integrations/crm/records/hubspot/deals/test_deal_writer.py`

Review these last to confirm the expected behavior:
- Signature/dedupe/queueing.
- Dispatch routing.
- Deal create/update/delete handling.
- Soft-deleted deal reactivation.
