# Microsoft Graph Webhook Flow

This document defines the service boundaries for Outlook Calendar and Outlook
Email Microsoft Graph webhooks.

## Principle

Microsoft Graph webhooks are triggers only. Webhook payloads must not write
calendar or email data directly. After request validation and subscription
verification, the backend schedules the existing incremental sync tasks.

## Runtime flow

```text
Outlook connect
  -> InitialSyncService
  -> integrations.sync_calendar_integration or integrations.sync_email_integration
  -> persist calendar_delta_link or email_folder_delta_links
  -> integrations.ensure_microsoft_graph_subscription
  -> MicrosoftGraphSubscriptionTaskService.ensure_subscription
  -> MicrosoftGraphSubscriptionService.ensure_subscription_metadata
  -> Microsoft Graph POST/PATCH /subscriptions
  -> persist KnowledgeIntegration.connection_metadata

Microsoft change notification
  -> microsoft_webhook_routes.py
  -> MicrosoftWebhookRequestService.handle_webhook
  -> MicrosoftWebhookIngestionService.ingest
  -> integrations.sync_calendar_integration or integrations.sync_email_integration
  -> refresh calendar_delta_link or email_folder_delta_links

Microsoft lifecycle notification
  -> microsoft_webhook_routes.py
  -> MicrosoftWebhookRequestService.handle_lifecycle
  -> MicrosoftWebhookIngestionService.ingest_lifecycle
  -> immediate existing incremental sync task

Scheduled renewal
  -> integrations.renew_microsoft_graph_subscriptions_all
  -> MicrosoftGraphSubscriptionTaskService.dispatch_all_subscription_ensures
  -> integrations.ensure_microsoft_graph_subscription per integration
  -> MicrosoftGraphSubscriptionTaskService.ensure_subscription
```

## Layer responsibilities

### Route layer

File:

```text
src/api/routers/integrations/microsoft_webhook_routes.py
```

Owns only FastAPI concerns:

- endpoint registration
- query parameter declarations
- Microsoft `validationToken` echo
- HTTP status response
- dependency wiring

It must not own JSON parsing, tenant validation, subscription validation, or
sync enqueue decisions.

### Request service

File:

```text
src/service/integrations/webhooks/microsoft/request_service.py
```

Owns HTTP request validation before business ingestion:

- body-size guardrails
- UTF-8 and JSON decoding
- top-level Graph notification collection validation
- required routing parameter validation
- `kind` parsing

### Ingestion service

File:

```text
src/service/integrations/webhooks/microsoft/ingestion_service.py
```

Owns webhook business validation and sync triggering:

- tenant-scoped integration lookup
- stored subscription metadata lookup
- `subscriptionId` and `clientState` verification
- duplicate notification suppression
- burst debounce
- enqueueing the existing incremental sync task

### Subscription Graph service

File:

```text
src/service/integrations/webhooks/microsoft/subscription_service.py
```

Owns Microsoft Graph subscription API behavior:

- builds `notificationUrl`
- builds `lifecycleNotificationUrl`
- generates and preserves `clientState`
- creates subscriptions
- renews subscriptions
- returns metadata to persist

### Subscription task service

File:

```text
src/service/integrations/webhooks/microsoft/subscription_task_service.py
```

Owns worker-facing orchestration:

- loading eligible Outlook integrations
- mapping integration type to subscription kind
- releasing DB sessions before Graph calls
- persisting metadata in fresh sessions
- dispatching scheduled renewal as integration-scoped ensure tasks
- producing task result dictionaries

### Celery task adapter

File:

```text
src/workers/integrations_worker/tasks/microsoft_graph_subscription_tasks.py
```

Owns only Celery concerns:

- task registration
- retry boundary
- UUID parsing
- queue dispatch for integration-scoped subscription ensure tasks
- calling `MicrosoftGraphSubscriptionTaskService`

## Scheduling policy

Outlook Calendar and Outlook Email are webhook-driven after initial sync. The
scheduled all-org calendar/email polling jobs are not registered by default,
because they create avoidable load once Microsoft Graph webhooks are active.
The all-org task implementations remain available for manual repair/backfill.

## Dependency direction

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
