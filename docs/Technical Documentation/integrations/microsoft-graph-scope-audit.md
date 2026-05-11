# Microsoft Graph Integration Scope Audit

Last reviewed: 2026-05-11

Scope of this audit: the Microsoft integration app registration and backend integration
flows for Outlook email, Outlook calendar, Teams message sync, and Teams transcript sync.
This does not cover the separate Teams bot app registration.

## Executive Summary

The Microsoft integration app registration is over-permissioned for the current backend
integration code. The active code path uses delegated Graph access for signed-in users.
The Terraform integration app also declares application permissions for call/media and
tenant-wide reads, but the integration workers reviewed here do not use app-only Graph
access.

Recommended direction:

1. Keep `offline_access`, `Mail.Read`, `Calendars.Read`, `OnlineMeetings.Read`,
   `Team.ReadBasic.All`, `Channel.ReadBasic.All`, and `Chat.Read`.
2. Add/request `User.Read` for `/me` profile lookup, and stop using `User.Read.All`
   for that purpose.
3. Remove delegated write/admin scopes from the integration app unless a new feature
   proves it needs them: `ChannelSettings.ReadWrite.All`, `Directory.ReadWrite.All`,
   and `Group.ReadWrite.All`.
4. Remove unused application permissions from the integration app unless they belong
   to a separate app-only feature: `Calls.*`, application `Directory.Read.All`,
   application `Group.Read.All`, application `OnlineMeetings.Read.All`, and
   application `User.Read.All`.
5. Fix metadata drift: `Files.Read.All` and `MailboxSettings.Read` appear in backend
   integration metadata but are not used by the current Graph callers reviewed here.

## Evidence Map

| Layer | Source | What it currently says |
|---|---|---|
| Infra app registration | `infra/terraform/azure/modules/microsoft_oauth_app/main.tf` | Declares the Microsoft integration app Graph scopes. |
| OAuth request builder | `src/service/integrations/providers/microsoft_provider.py` | Requests broad scope bundles by integration type. Outlook email and calendar currently request Teams-related scopes too. |
| Scope validation | `src/service/integrations/scope_validation.py` | Enforces the same broad Microsoft scope bundles after callback. |
| Metadata shown to product/API | `src/infra/config/integrations_metadata.py` | Shows narrower or different scopes; includes `Files.Read.All` and `MailboxSettings.Read`, which are not requested by `MicrosoftProvider`. |
| Runtime Graph callers | `src/workers/integrations_worker/providers/*`, `src/service/integrations/transcripts/teams_provider.py` | Uses `/me` delegated Graph endpoints for mail, calendar, Teams channels/chats, and transcripts. |

Microsoft's permissions reference says least privilege is the best practice and marks
each permission's admin-consent requirement. In that reference, delegated `Mail.Read`,
`Calendars.Read`, `Chat.Read`, `Channel.ReadBasic.All`, `Team.ReadBasic.All`,
`User.Read`, and `offline_access` do not require admin consent. Delegated
`ChannelMessage.Read.All`, `OnlineMeetingTranscript.Read.All`, directory, group,
settings, and all application permissions listed below do require admin consent.

## Scopes Currently Used By Integration Code

These scopes map to Graph endpoints that exist in the current integration code.

| Scope | Type | Used for | Runtime evidence | Admin consent required? | Keep? |
|---|---|---|---|---|---|
| `offline_access` | Delegated | Refresh tokens for background sync. | Requested for all Microsoft integration OAuth flows. | No | Yes |
| `Mail.Read` | Delegated | Outlook folders, messages delta, and message body backfill. | `/me/mailFolders`, `/me/mailFolders/{folder_id}/messages/delta`, `$batch` `/me/messages/{id}`. | No | Yes |
| `Calendars.Read` | Delegated | Outlook calendar delta and event hydration. | `/me/calendarView/delta`, `/me/events/{id}`, `/me/calendar/events/{id}`. | No | Yes |
| `Team.ReadBasic.All` | Delegated | List teams joined by signed-in user. | `/me/joinedTeams`. | No | Yes |
| `Channel.ReadBasic.All` | Delegated | List channels under joined teams. | `/teams/{teamId}/channels`. | No | Yes |
| `ChannelMessage.Read.All` | Delegated | Read channel messages. | `/teams/{teamId}/channels/{channelId}/messages/delta`. | Yes | No; channel-message sync is skipped unless a legacy/manual grant has this scope |
| `Chat.Read` | Delegated | List group/meeting chats and read chat messages. | `/me/chats`, `/chats/{chatId}/messages`. | No | Yes |
| `OnlineMeetings.Read` | Delegated | Resolve Teams meeting metadata by join URL. | `/me/onlineMeetings?$filter=JoinWebUrl...`. | No | Yes |
| `OnlineMeetingTranscript.Read.All` | Delegated | List and fetch Teams meeting transcripts. | `/me/onlineMeetings/{id}/transcripts` and transcript content endpoint. | Yes | No; transcript content fetch is bypassed unless already granted |
| `User.Read` | Delegated | Read signed-in user's profile/email during OAuth callback. | `GET /me` in `MicrosoftProvider.get_user_info()`. | No | Yes, but currently not requested by backend `_get_scopes()` |

## Scopes Currently Requested But Not Needed By The Matching Integration Flow

These are requested by `MicrosoftProvider._get_scopes()` or enforced by scope
validation, but they do not match the current integration's own Graph callers.

| Scope | Type | Requested by | Why it looks unnecessary now | Admin consent required? | Recommendation |
|---|---|---|---|---|---|
| `Calendars.Read` | Delegated | Outlook email OAuth flow | Outlook email code only calls mail endpoints. Calendar sync should request it separately. | No | Remove from Outlook email scope bundle |
| `Chat.Read` | Delegated | Outlook email and Outlook calendar OAuth flows | Outlook email/calendar code paths do not read Teams chats. | No | Keep only on Teams integration |
| `ChannelMessage.Read.All` | Delegated | Outlook email OAuth flow | Outlook email code does not read Teams channel messages. | Yes | Keep only on Teams integration |
| `Team.ReadBasic.All` | Delegated | Outlook email OAuth flow | Outlook email code does not list teams. | No | Keep only on Teams integration |
| `User.Read.All` | Delegated | Outlook email and Outlook calendar OAuth flows | `/me` profile lookup only needs `User.Read`; no current caller reads all users' full profiles. | Yes | Replace with `User.Read` |
| `Mail.Read` | Delegated | Outlook calendar OAuth flow | Outlook calendar code only calls calendar/event endpoints. | No | Remove from Outlook calendar scope bundle |
| `ChannelSettings.Read.All` | Delegated | Teams OAuth flow | Current Teams sync lists teams/channels and reads messages/chats; it does not read channel settings. | Yes | Remove |
| `ChannelSettings.ReadWrite.All` | Delegated | Teams OAuth flow | Current Teams sync does not write channel settings. | Yes | Remove |
| `Group.Read.All` | Delegated | Teams OAuth flow | Current code does not call group APIs. | Yes | Remove unless a group endpoint is added |
| `Group.ReadWrite.All` | Delegated | Teams OAuth flow | Current code does not write groups. | Yes | Remove |
| `Directory.Read.All` | Delegated | Teams OAuth flow | Current code does not call directory APIs. | Yes | Remove unless a directory lookup is added |
| `Directory.ReadWrite.All` | Delegated | Teams OAuth flow | Current code does not write directory data. | Yes | Remove |

## Infra App Registration Permissions Not Used By Integration Code

These permissions are declared in the integration app registration Terraform module,
but the reviewed integration flows do not use app-only Graph access.

| Scope | Type | Declared in infra? | Runtime evidence found? | Admin consent required? | Recommendation |
|---|---|---:|---|---|---|
| `Calls.AccessMedia.All` | Application | Yes | No integration worker uses Graph app-only call media. | Yes | Remove from integration app unless owned by a separate app-only feature |
| `Calls.JoinGroupCall.All` | Application | Yes | No integration worker joins calls as an app. | Yes | Remove from integration app unless owned by a separate app-only feature |
| `Calls.JoinGroupCallAsGuest.All` | Application | Yes | No integration worker joins meetings as a guest app. | Yes | Remove from integration app unless owned by a separate app-only feature |
| `Directory.Read.All` | Application | Yes | No integration worker uses app-only directory reads. | Yes | Remove from integration app unless owned by a separate app-only feature |
| `Group.Read.All` | Application | Yes | No integration worker uses app-only group reads. | Yes | Remove from integration app unless owned by a separate app-only feature |
| `OnlineMeetings.Read.All` | Application | Yes | Transcript provider uses delegated `/me/onlineMeetings`, not app-only tenant-wide meeting reads. | Yes | Remove from integration app unless app-only transcript access is intentionally planned |
| `User.Read.All` | Application | Yes | No integration worker uses app-only user profile reads. | Yes | Remove from integration app |

## Metadata Drift Noted

These entries are not the app-registration truth, but they can mislead UI/API consumers
or future implementation work.

| Scope | Where it appears | Current issue | Admin consent required? | Recommendation |
|---|---|---|---|---|
| `Files.Read.All` | Teams integration metadata | Metadata says Teams sync needs file access, but the current Teams integration provider reviewed here does not call file APIs. The integration Terraform module also does not declare this delegated scope. | No for delegated; yes for application | Remove from metadata or document the separate file-ingestion owner |
| `MailboxSettings.Read` | Outlook email metadata | Metadata says Outlook email needs mailbox settings, but current Outlook email provider calls folders/messages only. `MicrosoftProvider` does not request it. | No | Remove from metadata unless mailbox settings are added |
| `User.Read` | Infra app registration only | The app registration declares it and `/me` needs it, but backend OAuth bundles currently request `User.Read.All` instead. | No | Request `User.Read` in backend OAuth bundles |

## Per-Integration Target Scope Set

This is the least-privilege target based on current backend Graph usage.

| Integration | Target scopes | Admin-consent scopes in target |
|---|---|---|
| Outlook Email | `offline_access`, `User.Read`, `Mail.Read` | None |
| Outlook Calendar | `offline_access`, `User.Read`, `Calendars.Read`, `OnlineMeetings.Read` | None |
| Teams messages/chats | `offline_access`, `User.Read`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `Chat.Read` | None |
| Future separate Teams transcripts integration | `offline_access`, `User.Read`, `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All` | `OnlineMeetingTranscript.Read.All` |

## Notes For Later Review

- If Teams file ingestion is intentionally part of this same integration app, audit the
  file-ingestion path separately before deciding on `Files.Read.All` or a narrower file
  permission. The current Teams sync provider reviewed here does not prove that need.
- If app-only transcript/call capture is planned, keep it in a separate design decision.
  The current transcript provider uses delegated `/me/onlineMeetings` endpoints.
- The Teams bot app registration is intentionally excluded. Do not remove bot app
  permissions based on this integration-app audit.

## Sources

- Repo: `D:\CopilotGTM\copilot-infra\terraform\azure\modules\microsoft_oauth_app\main.tf`
- Repo: `src/service/integrations/providers/microsoft_provider.py`
- Repo: `src/service/integrations/scope_validation.py`
- Repo: `src/infra/config/integrations_metadata.py`
- Repo: `src/workers/integrations_worker/providers/email/outlook_email.py`
- Repo: `src/workers/integrations_worker/providers/calendar/outlook_calendar.py`
- Repo: `src/workers/integrations_worker/providers/messaging/teams.py`
- Repo: `src/service/integrations/transcripts/teams_provider.py`
- Microsoft Learn: <https://learn.microsoft.com/en-us/graph/permissions-reference?view=graph-rest-1.0>
- Microsoft Learn: <https://learn.microsoft.com/en-us/graph/permissions-overview>
