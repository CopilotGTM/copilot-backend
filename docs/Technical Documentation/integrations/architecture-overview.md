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

HubSpot and Salesforce custom OAuth are supported through the first-party provider architecture. CRM sync for both providers remains deferred and is not part of the active sync worker pipeline yet.

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
    tasks/
      email/                    # Three-tier email sync tasks
      calendar/                 # Three-tier calendar sync tasks
      messaging/                # Three-tier messaging sync tasks
      token_refresh.py          # Three-tier token refresh tasks
    config.py                   # Task limits, beat schedule, worker config
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
      3. Dispatch initial sync only for providers with active sync support
         HubSpot and Salesforce currently store
         `initial_sync_status=not_implemented` in `connection_metadata`
         instead of dispatching CRM sync

Celery Beat (scheduled)
  -> Tier 1: all_orgs_task (fan out to orgs)
  -> Tier 2: org_task (sync all integrations for one org)
  -> Provider API fetch (NO db session held)
  -> SyncService writes to DB (fresh session)
```

## Key Design Decisions

**Org-scoped records**: Calendar events, messaging channels/members/messages are org-scoped
(not per-integration). Multiple users' integrations contribute to the same canonical records.
Per-integration tracking is done via state tables
(`calendar_event_user_states`, `messaging_channel_integration_states`).

**Fresh session pattern**: Database sessions are never held during external API calls. Data is extracted into dataclasses, the session is released, the API call is made, then a new session is acquired to write results.

**ON CONFLICT DO UPDATE**: All sync services use PostgreSQL atomic upserts to handle concurrent writes and idempotent re-syncs without N+1 queries.
