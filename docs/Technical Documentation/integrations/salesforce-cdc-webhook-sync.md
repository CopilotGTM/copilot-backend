# Salesforce Opportunity CDC Sync

Salesforce does not use a HubSpot-style public HTTP webhook for the first realtime
Opportunity sync slice. The backend runs a bounded integrations-worker subscriber
against Salesforce Pub/Sub API topic `/data/OpportunityChangeEvent`, decodes Avro
payloads, fetches the changed Opportunity snapshot, and reuses the existing
Salesforce deal persistence path.

## Scope

This v1 is intentionally Opportunity-only.

- In scope: `OpportunityChangeEvent`, replay cursor storage, subscriber leasing,
  keepalive replay advancement, full Opportunity fetch, deal upsert, and idempotent
  local soft-delete for Salesforce delete events.
- Out of scope: Accounts, Contacts, OpportunityContactRole fanout, durable local
  event handoff, event fingerprints, separate dispatchers, and separate refresh
  tasks.

## Runtime Flow

```text
Salesforce initial sync success dispatches integrations.run_salesforce_cdc_subscriber
        |
        v
subscriber opens /data/OpportunityChangeEvent for the requested integration
        |
        v
for each recordId in ChangeEventHeader.recordIds:
    DELETE -> soft-delete matching CRMDeal
    other  -> fetch full Opportunity -> persist_single_opportunity()
        |
        v
after every record ID in the event succeeds, store replay ID
        |
        v
bounded subscriber run requeues the next subscriber run
```

Replay state advances only after the event's record IDs are processed. If fetch or
persistence fails first, the previous replay ID remains in place and Salesforce can
redeliver the event on the next subscription attempt. Duplicate delivery is acceptable
because the deal writer is upsert-oriented and soft-delete is idempotent.

The first subscriber run starts from Salesforce's latest replay position when no
local cursor exists. The initial sync is the source of truth for pre-subscription
state; starting from latest avoids replaying up to 72 hours of retained events on
new connections.

Transient token, Pub/Sub, fetch, or persistence failures mark the CDC state as
errored and enqueue a delayed subscriber retry without advancing replay. If
Salesforce rejects the stored replay ID, the subscriber clears the local cursor,
queues a full Salesforce deal repair sync, and the repair sync restarts CDC after
it completes successfully.

## State

Each Salesforce integration stores its CDC checkpoint and lease state in
`knowledge_integrations.connection_metadata.salesforce_cdc.opportunity`.

No separate CDC subscription table or CDC event table exists in v1.

## Task Contract

The single Celery task is `integrations.run_salesforce_cdc_subscriber`.

It requires explicit `organisation_id` and `integration_id`. With those IDs, it
acquires a subscription lease, opens Pub/Sub, processes events, stores keepalive
replay positions, stores committed replay IDs after successful processing, respects
Salesforce `pending_num_requested` flow-control top-ups, releases its lease before
handoff, and requeues the next bounded subscriber run when the stream exits
normally.

There is no fixed scheduled job for this subscriber. Salesforce initial-sync success
starts the first run, and each normal bounded run schedules the next one. Lease
state in `connection_metadata` prevents duplicate active subscribers for one
integration/channel.

## Required Integration Metadata

Opportunity CDC discovery expects:

```json
{
  "salesforce_organization_id": "00D...",
  "salesforce_instance_url": "https://example.my.salesforce.com",
  "salesforce_cdc": {
    "opportunity": {
      "enabled": true,
      "channel": "/data/OpportunityChangeEvent",
      "last_replay_id": "opaque-base64-replay-id",
      "lease_owner": "worker-id",
      "lease_expires_at": "2026-05-31T03:30:00+00:00"
    }
  }
}
```

`channel` defaults to `/data/OpportunityChangeEvent` when omitted. The default Pub/Sub
endpoint is `api.pubsub.salesforce.com:443`; the global Salesforce Pub/Sub host is
accepted on ports `443` and `7443`.
