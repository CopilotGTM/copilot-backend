# Lean Salesforce Opportunity CDC Plan

Implement Salesforce CDC v1 as one Opportunity-only subscriber path.

## Architecture

- Keep Salesforce Pub/Sub transport, Avro schema cache, generated Pub/Sub proto files,
  `grpcio`, `protobuf`, and `fastavro`.
- Store the Opportunity replay checkpoint and subscriber lease under
  `knowledge_integrations.connection_metadata.salesforce_cdc.opportunity`.
- Do not create `salesforce_cdc_events`, event fingerprints, an outbox dispatcher, or
  a separate Opportunity refresh task.
- Use one Celery task name: `integrations.run_salesforce_cdc_subscriber`.
- Do not add a fixed scheduled job for the subscriber. Start it from Salesforce
  initial-sync success and let each normal bounded run enqueue the next run.

## Runtime

```text
Salesforce initial-sync success dispatches run_salesforce_cdc_subscriber
  -> subscribe to /data/OpportunityChangeEvent
  -> process every recordId in the event
  -> store the latest replay ID after all recordIds succeed, or from keepalive
  -> enqueue the next bounded subscriber run
```

When no local replay cursor exists, the first subscription starts from Salesforce's
latest position. The full initial sync owns pre-subscription state, and latest
avoids replaying up to 72 hours of retained events on a newly connected org.

The subscriber follows Salesforce pull-flow control by topping up only the available
capacity from `pending_num_requested`. Transient token, Pub/Sub, fetch, or persist
failures release the lease and enqueue a delayed subscriber retry without advancing
replay. Replay-expired errors clear the cursor, release the lease, and queue a full
deal repair sync with an opt-in restart flag so CDC resumes only after the repair
sync succeeds.

For non-delete events, fetch the full Opportunity and call
`SalesforceDealRecordSyncService.persist_single_opportunity(...)`. For DELETE events,
soft-delete the matching local `CRMDeal`.

## Verification

- Subscriber tests cover discovery fanout, explicit streaming, multi-record events,
  keepalive replay storage, replay safety on failure, and DELETE soft-delete.
- Focused verification:
  `uv run pytest tests/service/integrations/webhooks/salesforce tests/workers/integrations_worker/tasks/crm/test_salesforce_cdc_subscriber_task.py tests/workers/integrations_worker/providers/crm/salesforce/test_single_opportunity_fetcher.py tests/service/integrations/crm/records/salesforce/deals/test_single_opportunity_persist.py -q`
