# Google OAuth And Workspace Scope Inventory

Status: repo-derived production inventory; needs Google Cloud Console confirmation before assessor submission
Environment: production
Last reviewed: 2026-07-05

This document is the CASA evidence map for Google data access in CopilotGTM production. It intentionally records repo-proven behavior separately from values that must be copied from Google Cloud Console.

## Console Values To Confirm

Do not paste client secrets here.

| Item | Status | Value |
| --- | --- | --- |
| Google OAuth consent app name | TODO | TODO |
| Google OAuth client ID | TODO | TODO |
| Google OAuth client secret storage | Repo-proven | Azure Key Vault secret name: `google-integration-client-secret` |
| Google OAuth client ID storage | Repo-proven | Azure Key Vault secret name: `google-integration-client-id` |
| Google Cloud project for Google Chat prod app | Repo-proven | `copilotgtm` |
| Google Chat prod app display name | Repo-proven | `Kai` |
| Google Chat prod service account | Repo-proven | `kai-chat-bot-prod@copilotgtm.iam.gserviceaccount.com` |
| CASA / Google notification email | TODO | TODO |
| Authorized assessor path | TODO | TODO |
| Assessor test account | TODO | TODO |

Repo evidence:

- `src/infra/config/configs.py` loads Google OAuth client ID and client secret from env for local dev or Azure Key Vault for production.
- `infra/manifests/google-chat-app/prod/chat-app-config.json` is the production Google Chat console reference.

## Production URLs And Routes

| Surface | Production value | Repo evidence |
| --- | --- | --- |
| API base URL | `https://api.copilotgtm.com` | CASA scope table |
| User app URL | `https://app.copilotgtm.com` | CASA scope table |
| Admin app URL | `https://admin.copilotgtm.com` | CASA scope table |
| Custom OAuth authorize endpoint | `POST /users/integrations/copilot-oauth/authorize` | `src/api/routers/integrations/copilot_oauth_routes.py` mounted by `src/api/fastapi_app.py` |
| Custom OAuth callback endpoint | `GET /users/integrations/copilot-oauth/callback` | `src/api/routers/integrations/copilot_oauth_routes.py`; prod redirect in `infra/terraform/azure/prod/terraform.tfvars` |
| Google Gmail/Calendar webhook endpoint | `POST /users/integrations/google/webhook?kind=...` | `src/api/routers/integrations/google_webhook_routes.py` mounted by `src/api/fastapi_app.py` |
| Google Chat event endpoint | `POST /users/bots/google-chat/events` | `src/api/routers/bots/google_chat_webhook_handler.py` mounted by `src/api/fastapi_app.py` |
| Google Chat prod app URL / audience | `https://api.copilotgtm.com/users/bots/google-chat/events` | `infra/manifests/google-chat-app/prod/chat-app-config.json` |

## OAuth Scopes Requested By Code

These are the exact Google integration scopes defined in `src/infra/config/google_integration_scopes.py`.

| Scope | Applies to | Purpose in product | Repo evidence |
| --- | --- | --- | --- |
| `https://www.googleapis.com/auth/userinfo.email` | Google, Gmail, Google Calendar | Identify the connected Google account email. | `GOOGLE_IDENTITY_SCOPES` |
| `https://www.googleapis.com/auth/userinfo.profile` | Google, Gmail, Google Calendar | Identify the connected Google account profile. | `GOOGLE_IDENTITY_SCOPES` |
| `https://www.googleapis.com/auth/gmail.readonly` | Google, Gmail | Read Gmail messages, labels, snippets, headers, body text/html, sender/recipient fields, thread IDs, label IDs, read/starred/important/draft state, and history IDs for sync. | `GOOGLE_SCOPE_GMAIL_READONLY`; `src/service/integrations/email/normalizer.py` |
| `https://www.googleapis.com/auth/gmail.send` | Google, Gmail | Send user-requested Gmail messages through `users.messages.send`. | `GOOGLE_SCOPE_GMAIL_SEND`; `src/service/email_sends/gmail_client.py` |
| `https://www.googleapis.com/auth/calendar.readonly` | Google, Google Calendar | Read primary calendar events for sync. | `GOOGLE_SCOPE_CALENDAR_READONLY`; `src/service/integrations/calendar/normalizer.py` |
| `https://www.googleapis.com/auth/calendar.events` | Google, Google Calendar | Create Google Calendar events and Meet-backed invites, and create event watch subscriptions. | `GOOGLE_SCOPE_CALENDAR_EVENTS`; `src/service/calendar_invites/google_calendar_client.py`; `src/service/integrations/webhooks/google/subscription_service.py` |
| `https://www.googleapis.com/auth/meetings.space.readonly` | Google, Google Calendar | Read Google Meet meeting-space metadata required by calendar/Meet integration behavior. | `GOOGLE_SCOPE_MEETINGS_SPACE_READONLY`; `GOOGLE_CALENDAR_SCOPES` |

The combined `IntegrationType.GOOGLE` connection requests all scopes above. The code also supports a reduced Google flow without `gmail.readonly` through `get_google_integration_scopes(..., include_gmail_read_scope=False)`.

## Google Chat App-Auth Scope

Google Chat is not the same OAuth client as the user-owned Gmail/Calendar integration. The production Chat app uses Google app authentication with Workload Identity Federation and the following scope:

| Scope | Applies to | Purpose in product | Repo evidence |
| --- | --- | --- | --- |
| `https://www.googleapis.com/auth/chat.bot` | Google Chat app service account | Send replies to Google Chat spaces/DMs and operate as the Kai Chat app. | `infra/manifests/google-chat-app/prod/chat-app-config.json`; `src/infra/config/configs.py` |

Google Chat inbound authenticity is validated by verifying the bearer token from Google Chat and enforcing the expected audience. There is no Slack-style signing secret for Google Chat in this implementation.

Repo evidence:

- `src/service/bots/auth_validators/google_chat_auth.py`
- `src/api/routers/bots/google_chat_webhook_handler.py`
- `infra/manifests/google-chat-app/README.md`

## Google APIs In Scope

| API / platform | Why it is in scope | Main repo evidence |
| --- | --- | --- |
| Google OAuth 2.0 / UserInfo | Custom OAuth, token exchange/refresh/revoke, user profile lookup. | `src/service/integrations/providers/google_provider.py` |
| Gmail API | Email sync, Gmail watch subscription, and user-requested send. | `src/workers/integrations_worker/providers/_google_api.py`; `src/service/integrations/webhooks/google/subscription_service.py`; `src/service/email_sends/gmail_client.py` |
| Google Calendar API | Calendar event sync, event watch subscription, invite creation, Meet conference creation. | `src/workers/integrations_worker/providers/_google_api.py`; `src/service/integrations/webhooks/google/subscription_service.py`; `src/service/calendar_invites/google_calendar_client.py` |
| Google Meet / meetings space | Calendar integration requests `meetings.space.readonly`; conference data is parsed from Google Calendar events. | `src/infra/config/google_integration_scopes.py`; `src/service/integrations/calendar/normalizer.py` |
| Google Chat API / Workspace Marketplace Chat app | Kai Google Chat app receives events and sends responses as app-auth. | `infra/manifests/google-chat-app/prod/chat-app-config.json`; `src/service/bots/google_chat_bot_service.py` |

## Data Types Accessed

| Area | Data accessed or stored | Repo evidence |
| --- | --- | --- |
| Google identity | Connected account email and profile metadata. | `src/service/integrations/providers/google_provider.py` |
| Gmail sync | Labels/folder metadata, message IDs, thread IDs, sender/recipient/cc/bcc, subject, body text/html, snippet, timestamps, labels, attachment presence, Gmail history IDs, and raw provider payload. | `src/service/integrations/email/normalizer.py`; `src/service/integrations/email/writer.py` |
| Gmail send | Recipient lists, subject, body, optional sender, and provider message ID after send. | `src/service/email_sends/gmail_client.py` |
| Calendar sync | Event IDs, title, description, location, start/end/timezone, organizer email/name, attendee email/name/response status, attendee count, conference link/provider/meeting ID, recurrence/occurrence identifiers, and raw attendee payload. | `src/service/integrations/calendar/normalizer.py`; `src/service/integrations/calendar/writer.py` |
| Calendar invite creation | Event title/body/location/start/end/timezone, attendees, and generated Google Meet conference data. | `src/service/calendar_invites/google_calendar_client.py` |
| Gmail and Calendar webhooks | Gmail Pub/Sub notifications and Calendar channel notifications used to schedule sync; webhooks do not directly write email or event records. | `src/api/routers/integrations/google_webhook_routes.py`; `src/service/integrations/webhooks/google/ingestion_service.py` |
| Google Chat inbound | Space name, thread name, message name, user identifier, display name, email, timezone, and message text used to route bot responses after identity linking. | `src/service/bots/google_chat_bot_service.py` |
| Google Chat outbound | Text response payloads posted to Google Chat spaces/DMs. | `src/service/bots/google_chat_bot_service.py` |

## CASA Readiness Position

Ready for repo-side evidence:

- Production routes and redirect URI are documented.
- Repo-requested Google OAuth scopes are documented.
- Google Chat app-auth scope and prod endpoint are documented.
- Google data types are mapped to code evidence.

Still required before assessor submission:

1. Copy the exact OAuth consent app name and OAuth client ID from Google Cloud Console.
2. Confirm the Google Cloud Console Data Access page exactly matches the scopes above.
3. Save the CASA Tier 2 notification email and assessor path.
4. Create or confirm the assessor test account and test data policy.
5. Export SCA/container and DAST evidence for the same production build.
