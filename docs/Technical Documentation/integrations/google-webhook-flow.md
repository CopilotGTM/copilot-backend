# Google Gmail and Calendar Webhook Flow

This document defines the service boundaries for Gmail and Google Calendar
webhooks.

## Principle

Google webhooks are triggers only. Webhook payloads must not write calendar or
email data directly. After request validation and subscription verification,
the backend schedules the existing incremental sync tasks.

## Runtime Flow

```text
Gmail connect
  -> InitialSyncService
  -> integrations.sync_email_integration
  -> persist Gmail history cursor metadata
  -> integrations.ensure_google_subscription
  -> GoogleSubscriptionTaskService.ensure_subscription
  -> GoogleSubscriptionService.ensure_subscription_metadata
  -> Gmail POST /gmail/v1/users/me/watch
  -> persist KnowledgeIntegration.connection_metadata.google_webhook_subscriptions.gmail

Google Calendar connect
  -> InitialSyncService
  -> integrations.sync_calendar_integration
  -> persist Google Calendar syncToken metadata
  -> integrations.ensure_google_subscription
  -> GoogleSubscriptionTaskService.ensure_subscription
  -> GoogleSubscriptionService.ensure_subscription_metadata
  -> Calendar POST /calendar/v3/calendars/primary/events/watch
  -> persist KnowledgeIntegration.connection_metadata.google_webhook_subscriptions.calendar

Gmail Pub/Sub push notification
  -> google_webhook_routes.py
  -> GoogleWebhookRequestService.handle_gmail_pubsub
  -> GoogleWebhookIngestionService.ingest_gmail
  -> integrations.sync_email_integration
  -> refresh Gmail history cursor metadata

Google Calendar channel notification
  -> google_webhook_routes.py
  -> GoogleWebhookRequestService.handle_calendar_channel
  -> GoogleWebhookIngestionService.ingest_calendar
  -> integrations.sync_calendar_integration
  -> refresh Google Calendar syncToken metadata

Scheduled renewal
  -> integrations.renew_google_subscriptions_all
  -> GoogleSubscriptionTaskService.dispatch_all_subscription_ensures
  -> integrations.ensure_google_subscription per integration
  -> GoogleSubscriptionTaskService.ensure_subscription
```

## Layer Responsibilities

### Route Layer

File:

```text
src/api/routers/integrations/google_webhook_routes.py
```

Owns only FastAPI concerns:

- endpoint registration
- `kind=gmail|calendar` routing
- query parameter declarations
- HTTP status response
- dependency wiring

It must not own Pub/Sub parsing, Calendar header parsing, subscription
validation, tenant validation, or sync enqueue decisions.

### Request Service

File:

```text
src/service/integrations/webhooks/google/request_service.py
```

Owns HTTP request validation before business ingestion:

- body-size guardrails
- UTF-8 and JSON decoding for Gmail Pub/Sub
- required routing parameter validation
- Gmail Pub/Sub envelope parsing
- Gmail Pub/Sub push token and expected subscription validation
- Google Calendar `X-Goog-*` header parsing

### Typed Notifications

File:

```text
src/service/integrations/webhooks/google/types.py
```

Owns provider payload normalization:

- decodes Gmail Pub/Sub base64 data into `emailAddress` and `historyId`
- accepts Pub/Sub `messageId` and `message_id` variants
- parses Calendar channel headers case-insensitively
- rejects malformed notifications before ingestion

### Ingestion Service

File:

```text
src/service/integrations/webhooks/google/ingestion_service.py
```

Owns webhook business validation and sync triggering:

- tenant-scoped integration lookup
- active integration and provider type validation
- stored subscription metadata lookup
- Gmail account email verification when local metadata has an email
- Calendar `channel_id`, `resource_id`, and `channel_token` verification
- Calendar `sync` handshake acknowledgement without data sync
- duplicate notification suppression
- burst debounce
- enqueueing the existing incremental sync task

### Subscription Metadata

File:

```text
src/service/integrations/webhooks/google/metadata.py
```

Owns the canonical metadata shape under:

```text
KnowledgeIntegration.connection_metadata.google_webhook_subscriptions
```

Stored keys:

- `gmail`: Gmail watch state, including `watch_history_id`, `topic_name`,
  label filters, and expiration
- `calendar`: Google Calendar channel state, including `channel_id`,
  `resource_id`, `resource_uri`, `channel_token`, callback address, and
  expiration

### Subscription API Service

File:

```text
src/service/integrations/webhooks/google/subscription_service.py
```

Owns Google API subscription behavior:

- validates integration type against requested subscription kind
- reuses unexpired subscription metadata when outside the renewal buffer
- reads `GOOGLE_GMAIL_PUBSUB_TOPIC` for Gmail watch creation
- reads `GOOGLE_WEBHOOK_BASE_URL` for Calendar callback URL creation
- creates Gmail watches with `users.watch`
- creates Calendar event channels with `events.watch`
- parses provider expiration values from epoch milliseconds
- returns updated metadata to persist

### Subscription Task Service

File:

```text
src/service/integrations/webhooks/google/subscription_task_service.py
```

Owns worker-facing orchestration:

- loading eligible Gmail and Google Calendar integrations
- mapping integration type to subscription kind
- releasing DB sessions before Google API calls
- persisting metadata in fresh sessions
- dispatching scheduled renewal as integration-scoped ensure tasks
- producing task result dictionaries

### Celery Task Adapter

File:

```text
src/workers/integrations_worker/tasks/google_subscription_tasks.py
```

Owns only Celery concerns:

- task registration
- retry boundary
- UUID parsing
- queue dispatch for integration-scoped subscription ensure tasks
- calling `GoogleSubscriptionTaskService`

## Endpoints

```text
POST /users/integrations/google/webhook?kind=gmail&token=...
POST /users/integrations/google/webhook?kind=calendar&organisation_id=...&integration_id=...
```

`kind` is explicit because Gmail and Calendar use different provider delivery
mechanisms and validation rules. Gmail Pub/Sub push endpoints are static, so
Gmail routing uses the decoded `emailAddress` to query the indexed
`public.organisation_integration_details.integration_email` locator. Every
candidate is then reloaded from its tenant schema and must still be an active
Google parent with Gmail enabled. Calendar channel URLs are per watch, so
Calendar also carries `organisation_id` and `integration_id`. A stale or
unavailable Gmail candidate is isolated and does not block another organisation
that connected the same mailbox.

Gmail Pub/Sub push requests must include the configured shared token in the
endpoint query string and the Pub/Sub envelope must match
`GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION`. Missing Gmail Pub/Sub auth configuration
fails closed before ingestion.

## Environment

```text
GOOGLE_WEBHOOK_BASE_URL=https://<public-api-host>
GOOGLE_GMAIL_PUBSUB_TOPIC=projects/<project>/topics/<topic>
GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/<subscription>
GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN=<shared-push-token>
```

The Pub/Sub push endpoint should be configured as:

```text
https://<public-api-host>/users/integrations/google/webhook?kind=gmail&token=<shared-push-token>
```

## Scheduling Policy

Gmail and Google Calendar are webhook-driven after initial sync. Webhooks
enqueue the same single-integration sync tasks used by scheduled/manual sync:

- Gmail: `integrations.sync_email_integration`
- Google Calendar: `integrations.sync_calendar_integration`

`integrations.renew_google_subscriptions_all` periodically fans out
`integrations.ensure_google_subscription` for each active Gmail or Google
Calendar integration. The integration-scoped ensure task runs on
`integrations_priority`.

## Dependency Direction

Allowed:

```text
workers -> service -> infra/domain/provider helpers
```

Avoid:

```text
service -> workers
```

Keeping services independent from Celery allows the same subscription workflow
to be reused by repair scripts, admin endpoints, or focused tests without
importing worker task modules.
