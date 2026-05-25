# Microsoft Graph Admin Consent Summary

Last reviewed: 2026-05-22

This table reflects the current Microsoft integration target scopes in
`src/infra/config/microsoft_integration_scopes.py`.

| Permission | Used by | Admin consent required? |
|---|---|---|
| `offline_access` | Outlook Email, Outlook Calendar, Teams | No |
| `User.Read` | Outlook Email, Outlook Calendar, Teams | No |
| `Mail.Read` | Outlook Email | No |
| `Mail.Send` | Outlook Email | No |
| `Calendars.ReadWrite` | Outlook Calendar | No |
| `OnlineMeetings.Read` | Outlook Calendar / meeting metadata resolution | No |
| `Team.ReadBasic.All` | Teams | No |
| `Channel.ReadBasic.All` | Teams | No |
| `Chat.Read` | Teams | No |

No delegated permission in the current target scope set requires admin consent by
itself. Some customer tenants still disable user consent or require administrator
approval for all third-party apps. In that case, CopilotGTM uses Microsoft's
tenant admin-consent endpoint once for the shared Microsoft scope bundle and then
asks the user to retry the normal OAuth connect flow so user-delegated tokens can
be issued.

The normal Microsoft OAuth flow should still be tried first. A missing stored grant
does not always mean admin consent is required, because tenant user-consent policies
vary. When Microsoft rejects the normal OAuth callback with an admin-approval error,
the callback redirect is annotated with `action_required=admin_consent_required` so
the frontend can surface the explicit admin-consent action.

The consent result is stored once per organisation under
`preferences.integrations.microsoft.admin_consent`, together with the current scope
bundle and a scope-version hash for audit/debugging. The available-integrations API
does not expose this as a pre-check, because CopilotGTM only asks for admin consent
after Microsoft rejects the normal OAuth attempt with an admin-approval error.

Teams Graph transcript content fetch is currently bypassed unless a legacy/manual grant
already contains `OnlineMeetingTranscript.Read.All`.

Source: Microsoft Graph permissions reference:
<https://learn.microsoft.com/en-us/graph/permissions-reference?view=graph-rest-1.0>
