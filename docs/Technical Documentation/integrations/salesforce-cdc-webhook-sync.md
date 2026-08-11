# Salesforce CRM CDC Sync

Salesforce CDC is consumed through Pub/Sub API, not an HTTP webhook. One dedicated
`salesforce-cdc-worker` process supervises up to seven customer integrations. Each integration has
one connector task subscribed to the aggregate `/data/ChangeEvents` topic; connecting another
Salesforce customer does not create another Kubernetes Deployment, pod, queue, or OS process.

## Why the old worker stopped after about one hour

The legacy design used bounded Celery subscriber tasks against individual object topics. A task ran
for roughly 55 minutes and then depended on broker handoff to enqueue its successor. Any failed or
lost handoff left that integration without a subscriber. Multiplying seven topics by each customer
also consumed ordinary integrations-worker slots and made a customer-specific worker tempting.

The aggregate worker removes that handoff. Its process is long-lived; only each Salesforce Pub/Sub
session rotates. The default session is 2,700 seconds (45 minutes), after which the connector opens a
new session from the last committed aggregate replay ID inside the same process. Transport failures
use bounded exponential reconnect delay and do not stop the other customer connectors.

## Runtime flow

```text
one Kubernetes Deployment / one pod / one Python process
        |
        v
supervisor discovers aggregate-ready Salesforce integrations every 60 seconds
        |
        +-- connector 1 -> /data/ChangeEvents -> customer 1 aggregate replay
        +-- connector 2 -> /data/ChangeEvents -> customer 2 aggregate replay
        +-- ...
        +-- connector 7 -> /data/ChangeEvents -> customer 7 aggregate replay
        |
        v
decode entityName and every ChangeEventHeader.recordIds entry
        |
        v
process record IDs sequentially through the shared Salesforce CDC processor
        |
        v
commit the aggregate replay ID only after every record succeeds
```

The process owns at most seven connector tasks. A slow or reconnecting customer has an independent
task and cannot cancel its peers. At `7/7`, an eighth integration is marked `capacity_exceeded` and
is not started; the worker never scales itself or creates customer-specific infrastructure.

## State and fencing

State remains in `knowledge_integrations.connection_metadata.salesforce_cdc`. Aggregate mode uses:

```json
{
  "salesforce_cdc": {
    "mode": "aggregate",
    "aggregate": {
      "channel": "/data/ChangeEvents",
      "status": "pending",
      "replay_preset": "EARLIEST",
      "last_replay_id": null,
      "heartbeat_at": null,
      "lease_owner": null,
      "lease_expires_at": null
    }
  }
}
```

The connector acquires a 120-second lease and renews it every 30 seconds. All replay, heartbeat,
disconnect, and release writes are fenced by the lease owner. Aggregate acquisition waits until all
legacy per-object leases are inactive, and a legacy subscriber exits harmlessly when it observes
aggregate mode. This prevents aggregate and legacy streams from overlapping.

Keepalives persist the server replay position and heartbeat. Data-event replay advances only after
successful processing, preserving at-least-once delivery. Upsert and soft-delete paths are
idempotent. A rejected or expired replay ID atomically fences the connector in `repairing` and
publishes one serial full-repair chain. The connector resumes from `EARLIEST` only after repair
succeeds.

## Initial sync and migration

New integrations enter aggregate `pending` state after successful initial sync and are discovered by
the existing worker process. Migration of an existing integration is deliberately separate and
dry-run by default. It does not copy any per-object replay cursor into the aggregate cursor because
Salesforce replay IDs are opaque and topic-specific.

The legacy per-object subscriber remains only as a compatibility and rollback path during rollout.
Its prior bounded-task/requeue ownership is not the steady-state architecture.

## Salesforce references

- [Subscribe RPC](https://developer.salesforce.com/docs/platform/pub-sub-api/references/methods/subscribe-rpc.html)
- [Event message durability](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-message-durability.html)
- [Pub/Sub API allocations](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/allocations.html)
- [Salesforce Pub/Sub API overview](https://developer.salesforce.com/blogs/2021/07/pub-sub-api-building-event-driven-integrations-just-got-even-easier)

Operational checks, alert thresholds, repair, and rollback are documented in
[Salesforce CDC Operations](salesforce-cdc-operations.md).
