# Integrations Worker

Celery worker for recurring provider sync, token refresh maintenance, and CRM post-connect initial-sync tasks.

This README is intentionally short. The detailed architecture docs live in:

- [docs/Technical Documentation/integrations/README.md](../../../docs/Technical%20Documentation/integrations/README.md)
- [docs/Technical Documentation/integrations/crm-initial-sync.md](../../../docs/Technical%20Documentation/integrations/crm-initial-sync.md)
- [docs/Technical Documentation/integrations/three-tier-sync-architecture.md](../../../docs/Technical%20Documentation/integrations/three-tier-sync-architecture.md)

## What This Worker Owns

- recurring calendar, email, and messaging sync
- recurring token refresh maintenance
- HubSpot CRM post-connect initial sync
- Salesforce CRM post-connect initial sync
- scheduled CRM schema refresh fan-out for HubSpot and Salesforce

It does not own the OAuth callback itself. The API layer accepts the callback, stores credentials, stamps queued metadata, and then dispatches this worker.

## Live Queues

- `integrations`
  - scheduled Tier 1 coordinators
  - Tier 2 per-org sync jobs
- `integrations_priority`
  - single-integration manual refresh jobs
  - HubSpot/Salesforce CRM post-connect chains
  - CRM chain terminators

Queue routing lives in [config.py](./config.py).

## Current Task Families

### Three-tier scheduled sync

- calendar
  - `sync_calendar_all_orgs`
  - `sync_calendar_org`
  - `sync_calendar_integration`
- email
  - `sync_email_all_orgs`
  - `sync_email_org`
  - `sync_email_integration`
- messaging channels
  - `sync_messaging_channels_all_orgs`
  - `sync_messaging_channels_org`
  - `sync_messaging_channels_integration`
- deal messages
  - `sync_deal_messages_all_orgs`
  - `sync_deal_messages_org`
  - `sync_deal_messages_integration`
- token refresh
  - `refresh_tokens`
  - `refresh_tokens_org`
  - `refresh_token`

### CRM post-connect chains

HubSpot:

`schema -> pipelines -> users -> companies -> contacts -> deals -> tasks -> notes -> finalize`

Salesforce:

`schema -> pipelines -> users -> companies -> contacts -> deals -> tasks -> notes -> finalize`

The task modules are under:

- [tasks/crm/hubspot](./tasks/crm/hubspot)
- [tasks/crm/salesforce](./tasks/crm/salesforce)

Chain builders live outside the worker in:

- `src/service/integrations/crm/orchestration/hubspot_initial_sync_chain.py`
- `src/service/integrations/crm/orchestration/salesforce_initial_sync_chain.py`

## Scheduled Jobs

Fixed schedules are owned by Kubernetes CronJobs, not in-process Celery Beat.

Source of truth:

- [scheduled_jobs.py](./scheduled_jobs.py)
- [cron_dispatch.py](./cron_dispatch.py)

Current fixed jobs:

- refresh tokens
- calendar sync
- email sync
- messaging channel sync
- deal message sync
- HubSpot deal schema fan-out
- Salesforce opportunity schema fan-out

`config.py` intentionally keeps `beat_schedule = {}`.

## Provider Fetchers

The worker fetches directly from provider APIs through provider-specific modules under:

- [providers/calendar](./providers/calendar)
- [providers/email](./providers/email)
- [providers/messaging](./providers/messaging)
- [providers/crm/hubspot](./providers/crm/hubspot)
- [providers/crm/salesforce](./providers/crm/salesforce)

HubSpot CRM fetchers currently cover schema, pipelines, owners/users, companies, contacts, deals, tasks, and notes.

Salesforce CRM fetchers currently cover schema, pipelines, users, accounts, contacts, opportunities, opportunity history, opportunity contact roles, tasks, and notes.

## Operational Rules

- never hold a DB session during a provider API call
- Tier 1 jobs fan out; Tier 2 jobs work per-org; Tier 3 jobs are single-integration
- CRM initial-sync status is written only by the chain terminators
- task limits and retry policy are defined centrally in [config.py](./config.py)

## Start Locally

```powershell
celery -A src.workers.integrations_worker.celery_app worker -l info -Q integrations,integrations_priority --pool=solo
```

To dispatch a scheduled job manually:

```powershell
uv run python -m src.workers.integrations_worker.cron_dispatch --job sync-hubspot-deal-schema-all-orgs
```
