# Salesforce CRM CDC Sync

Salesforce does not use a HubSpot-style public HTTP webhook for realtime CRM sync.
The backend runs bounded integrations-worker subscribers against Salesforce Pub/Sub
API single-entity topics:

- `/data/OpportunityChangeEvent`
- `/data/AccountChangeEvent`
- `/data/ContactChangeEvent`
- `/data/UserChangeEvent`
- `/data/TaskChangeEvent`
- `/data/ContentNoteChangeEvent`
- `/data/ContentDocumentLinkChangeEvent`

Each event is decoded from Avro, fanned out across every
`ChangeEventHeader.recordIds` entry, refreshed from Salesforce REST, and persisted
through the same normalizers and writers used by full Salesforce sync.

## Scope

This v1 covers standard Opportunity, Account, Contact, User, Task, and current
Salesforce Lightning Notes CDC channels. Lightning Notes require both
`ContentNote` and `ContentDocumentLink`: `ContentNote` carries note content
updates, while `ContentDocumentLink` carries relationship changes to Account,
Contact, and Opportunity.

- In scope: per-object replay cursor storage, subscriber leasing, keepalive replay
  advancement, single-record fetch, record upsert, Account/User deactivation,
  owner backfill, and Opportunity/Contact/Task/Note soft-delete for Salesforce
  delete events.
- Out of scope: `/data/ChangeEvents`, OpportunityContactRole fanout, durable local
  event handoff, event fingerprints, and separate subscriber task names.

Salesforce setup prerequisite: Account, Contact, Opportunity, User, Task,
ContentNote, and ContentDocumentLink must be selected for Change Data Capture in
Salesforce Setup where those entities are available in the org. The integration
user also needs `View All Users` so User CDC refreshes can read owner records.

## Runtime Flow

```text
Salesforce initial sync success dispatches integrations.run_salesforce_cdc_subscriber
for each enabled CDC channel
        |
        v
subscriber opens the requested /data/*ChangeEvent channel
        |
        v
for each recordId in ChangeEventHeader.recordIds:
    Opportunity DELETE -> CRMDeal.is_deleted = true
    Account DELETE     -> CRMCompany.is_active = false
    Contact DELETE     -> CRMContact.is_deleted = true
    User DELETE        -> CRMUser.is_active = false
    Task DELETE        -> CRMActivity task/todo is_deleted = true
    ContentNote DELETE -> CRMActivity note is_deleted = true
    ContentDocumentLink DELETE -> rehydrate remaining note links or soft-delete note
    other              -> fetch one record -> persist_single_*()
        |
        v
after every record ID in the event succeeds, store replay ID
        |
        v
bounded subscriber run requeues the next subscriber run
```

Replay state advances only after the event's record IDs are processed. If fetch or
persistence fails first, the previous replay ID remains in place and Salesforce can
redeliver the event on the next subscription attempt. Duplicate delivery is
acceptable because the writers are upsert-oriented and delete/deactivate handling is
idempotent.

The first subscriber run starts from Salesforce's latest replay position when no
local cursor exists. The initial sync is the source of truth for pre-subscription
state; starting from latest avoids replaying up to 72 hours of retained events on
new connections.

Transient token, Pub/Sub, fetch, or persistence failures mark the CDC state as
errored and enqueue a delayed subscriber retry without advancing replay. If
Salesforce rejects the stored replay ID, the subscriber clears that channel's local
cursor and queues the matching full repair sync:

- Opportunity -> `integrations.sync_salesforce_deals`
- Account -> `integrations.sync_salesforce_companies`
- Contact -> `integrations.sync_salesforce_contacts`
- User -> `integrations.sync_salesforce_users`
- Task -> `integrations.sync_salesforce_tasks`
- ContentNote -> `integrations.sync_salesforce_notes`
- ContentDocumentLink -> `integrations.sync_salesforce_notes`

The repair sync restarts the same CDC channel after it completes successfully.

## State

Each Salesforce integration stores its CDC checkpoint and lease state in
`knowledge_integrations.connection_metadata.salesforce_cdc.<object>`, where
`<object>` is `opportunity`, `account`, `contact`, `user`, `task`,
`content_note`, or `content_document_link`.

Each channel has an independent replay cursor and lease. No separate CDC
subscription table or CDC event table exists in v1.

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

CDC discovery expects:

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
    },
    "account": {
      "enabled": true,
      "channel": "/data/AccountChangeEvent",
      "last_replay_id": "opaque-base64-replay-id"
    },
    "contact": {
      "enabled": true,
      "channel": "/data/ContactChangeEvent",
      "last_replay_id": "opaque-base64-replay-id"
    },
    "user": {
      "enabled": true,
      "channel": "/data/UserChangeEvent",
      "last_replay_id": "opaque-base64-replay-id"
    },
    "task": {
      "enabled": true,
      "channel": "/data/TaskChangeEvent",
      "last_replay_id": "opaque-base64-replay-id"
    },
    "content_note": {
      "enabled": true,
      "channel": "/data/ContentNoteChangeEvent",
      "last_replay_id": "opaque-base64-replay-id"
    },
    "content_document_link": {
      "enabled": true,
      "channel": "/data/ContentDocumentLinkChangeEvent",
      "last_replay_id": "opaque-base64-replay-id"
    }
  }
}
```

Existing integrations with only the `opportunity` entry remain Opportunity-only.
New reconnects receive Account, Contact, Opportunity, User, Task, ContentNote, and
ContentDocumentLink entries by default.

The default Pub/Sub endpoint is `api.pubsub.salesforce.com:443`; the global
Salesforce Pub/Sub host is accepted on ports `443` and `7443`.
