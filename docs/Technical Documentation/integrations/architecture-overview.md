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

CRM integrations (HubSpot, Salesforce) are deprecated and excluded from active sync.

## OAuth Model

CopilotGTM uses **first-party custom OAuth** — no third-party proxy (Nango was removed). Each environment (local, dev, prod) has its own Azure AD app registration and Google OAuth client, managed via Terraform.

Credentials are stored in Azure Key Vault (`microsoft-integration-client-id`, `microsoft-integration-client-secret`, `google-integration-client-id`, `google-integration-client-secret`) and loaded at runtime with a TTL cache.

## Directory Structure

```
src/
  service/integrations/
    providers/
      token_resolution.py       # Token fetch, refresh, 401 retry, distributed lock
      google_provider.py        # Google OAuth: authorize, exchange, refresh
      microsoft_provider.py     # Microsoft OAuth: authorize, exchange, refresh
      provider_factory.py       # Route integration_type -> provider
    copilot_callback_service.py # OAuth callback handler (store tokens, trigger sync)
    calendar_sync_service.py    # Calendar event upsert (org-scoped)
    email_sync_service.py       # Email folder/message upsert
    email_delta_sync_service.py # Email sync orchestrator (fresh session pattern)
    messaging_sync_service.py   # Teams channel/chat/message upsert (org-scoped)
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
  -> GET /users/integrations/copilot-oauth/connect?type=outlook_email
  -> Provider generates authorization URL
  -> User consents in Microsoft/Google
  -> Redirect to /users/integrations/copilot-oauth/callback
  -> copilot_callback_service.py:
      1. Exchange auth code for tokens
      2. Store tokens in KnowledgeIntegration.credentials
      3. Dispatch Tier 3 sync task (immediate initial sync)

Celery Beat (scheduled)
  -> Tier 1: all_orgs_task (fan out to orgs)
  -> Tier 2: org_task (sync all integrations for one org)
  -> Provider API fetch (NO db session held)
  -> SyncService writes to DB (fresh session)
```

## Key Design Decisions

**Org-scoped records**: Calendar events, messaging channels/members/messages are org-scoped (not per-integration). Multiple users' integrations contribute to the same canonical records. Per-integration tracking is done via state tables (`calendar_event_user_states`, `messaging_channel_integration_states`).

**Fresh session pattern**: Database sessions are never held during external API calls. Data is extracted into dataclasses, the session is released, the API call is made, then a new session is acquired to write results.

**ON CONFLICT DO UPDATE**: All sync services use PostgreSQL atomic upserts to handle concurrent writes and idempotent re-syncs without N+1 queries.
