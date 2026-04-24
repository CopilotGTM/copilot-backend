# Integration Architecture Overview

## Supported Integrations

| Integration | Provider | Type | Sync Method |
|-------------|----------|------|-------------|
| Outlook Email | Microsoft Graph | Email | Delta Query |
| Outlook Calendar | Microsoft Graph | Calendar | Delta Query (calendarView/delta) |
| Gmail | Google API | Email | History API (historyId) |
| Google Calendar | Google API | Calendar | Incremental sync (syncToken) |
| Teams Channels | Microsoft Graph | Messaging | Delta Query (messages/delta) |
| Teams Chats | Microsoft Graph | Messaging | Pagination with date cutoff |
| Slack Channels | Slack Web API | Messaging | conversations.list |
| Slack Group Chats | Slack Web API | Messaging | conversations.list (mpim) |
| Slack Messages | Slack Web API | Messaging | conversations.history + conversations.replies |

HubSpot and Salesforce custom OAuth are supported through the first-party provider architecture.
Both providers now have active CRM initial-sync chains. HubSpot is live through deals, and
Salesforce is live through contacts. See [crm-initial-sync.md](crm-initial-sync.md) for the
current chain order, metadata contract, and persisted tables.

## OAuth Model

CopilotGTM uses **first-party custom OAuth** for Google, Microsoft, Slack, HubSpot, and Salesforce. Each
environment (local, dev, prod) has its own provider app credentials, managed via Terraform/Key Vault.

Credentials are stored in Azure Key Vault (`microsoft-integration-client-id`,
`microsoft-integration-client-secret`, `google-integration-client-id`,
`google-integration-client-secret`, `slack-client-id`, `slack-client-secret`,
`hubspot-integration-client-id`, `hubspot-integration-client-secret`,
`salesforce-integration-client-id`, `salesforce-integration-client-secret`)
and loaded at runtime with a TTL cache.

## Directory Structure

```
src/
  service/integrations/
    providers/
      token_resolution.py       # Token fetch, refresh, 401 retry, distributed lock
      google_provider.py        # Google OAuth: authorize, exchange, refresh
      hubspot_provider.py       # HubSpot OAuth: authorize, exchange, refresh, revoke
      microsoft_provider.py     # Microsoft OAuth: authorize, exchange, refresh
      salesforce_provider.py    # Salesforce OAuth: authorize, exchange, refresh, revoke
      slack_provider.py         # Slack OAuth: authorize, exchange, refresh, revoke
      provider_factory.py       # Route integration_type -> provider
    copilot_callback_service.py # OAuth callback handler (store tokens, trigger sync)
    calendar_sync_service.py    # Calendar event upsert (org-scoped)
    email_sync_service.py       # Email folder/message upsert
    email_delta_sync_service.py # Email sync orchestrator (fresh session pattern)
    messaging_sync_service.py   # Teams/Slack channel/chat/message upsert (org-scoped)
    crm/
      __init__.py               # CRM integration helpers shared across provider syncs
      orchestration/
        hubspot_initial_sync_chain.py
        salesforce_initial_sync_chain.py
      schema/
        common/
        hubspot/deals/
        salesforce/opportunities/
      records/
        hubspot/
          pipelines/ users/ companies/ contacts/ deals/
        salesforce/
          pipelines/ users/ companies/ contacts/
    custom_token_refresh_service.py  # Scheduled token refresh service
    sync_health.py              # Error classification, circuit breaker, failure tracking
    sync_history_manager.py     # IntegrationSyncHistory record management

  workers/integrations_worker/
    providers/
      _google_api.py            # Google API helpers + 429 retry
      _microsoft_graph.py       # Microsoft Graph helpers + 429 retry
      _delta_sync.py            # Shared delta pagination + 410 expiry handling
      email/
        outlook_email.py        # Outlook email fetcher (delta query)
        gmail_email.py          # Gmail fetcher (history API)
      calendar/
        outlook_calendar.py     # Outlook calendar fetcher (delta query)
        google_calendar.py      # Google Calendar fetcher (sync tokens)
      messaging/
        teams.py                # Teams channels, chats, messages fetcher
        slack.py                # Slack channels, chats, messages fetcher
      crm/
        hubspot/
          schema_fetcher.py
          companies_fetcher.py
          contacts_fetcher.py
          deals_fetcher.py
          owners_fetcher.py
          pipelines_fetcher.py
        salesforce/
          schema_fetcher.py
          pipelines_fetcher.py
          users_fetcher.py
          accounts_fetcher.py
          contacts_fetcher.py
    tasks/
      email/                    # Three-tier email sync tasks
      calendar/                 # Three-tier calendar sync tasks
      messaging/                # Three-tier messaging sync tasks
      crm/
        hubspot/                # HubSpot schema + record-sync tasks + terminators
        salesforce/             # Salesforce schema + record-sync tasks + terminators
      token_refresh.py          # Three-tier token refresh tasks
    scheduled_jobs.py           # Source-of-truth cron metadata for K8s CronJobs
    cron_dispatch.py            # Entry point K8s CronJobs invoke to dispatch Tier 1 tasks
    config.py                   # Task limits + routes (no in-process Beat — beat_schedule is empty by design)
```

## Data Flow

```
User clicks "Connect" in frontend
  -> GET /users/integrations/copilot-oauth/connect?type=hubspot
  -> Provider generates authorization URL
  -> User consents in the provider
  -> Redirect to /users/integrations/copilot-oauth/callback
  -> copilot_callback_service.py:
      1. Exchange auth code for tokens
      2. Store tokens in KnowledgeIntegration.credentials
      3. apply_post_connect_state() stamps queued provider-specific CRM metadata
      4. trigger_initial_sync() dispatches the provider chain:
         - HubSpot -> schema -> pipelines -> users -> companies -> contacts -> deals -> finalize
         - Salesforce -> schema -> pipelines -> users -> companies -> contacts -> finalize

Kubernetes CronJob (scheduled — this repo does NOT use in-process Celery Beat)
  -> `python -m src.workers.integrations_worker.cron_dispatch --job <name>`
  -> dispatch_scheduled_job() looks up the ScheduledJob in scheduled_jobs.py
  -> enqueues Tier 1 task (e.g. integrations.sync_hubspot_deal_schema_all_orgs)
  -> Tier 1 reads master schema, dispatches per-integration Tier 3 tasks
  -> Tier 3 runs fresh-session: extract integration -> fetch (no session) -> persist
  -> SyncService writes to DB (fresh session)
```

## Key Design Decisions

**Org-scoped records**: Calendar events, messaging channels/members/messages are org-scoped
(not per-integration). Multiple users' integrations contribute to the same canonical records.
Per-integration tracking is done via state tables
(`calendar_event_user_states`, `messaging_channel_integration_states`).

**Fresh session pattern**: Database sessions are never held during external API calls. Data is extracted into dataclasses, the session is released, the API call is made, then a new session is acquired to write results.

**ON CONFLICT DO UPDATE**: All sync services use PostgreSQL atomic upserts to handle concurrent writes and idempotent re-syncs without N+1 queries.
