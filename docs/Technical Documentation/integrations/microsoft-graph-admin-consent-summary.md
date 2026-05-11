# Microsoft Graph Admin Consent Summary

Last reviewed: 2026-05-11

This table reflects the current Microsoft integration target scopes in
`src/infra/config/microsoft_integration_scopes.py`.

| Permission | Used by | Admin consent required? |
|---|---|---|
| `offline_access` | Outlook Email, Outlook Calendar, Teams | No |
| `User.Read` | Outlook Email, Outlook Calendar, Teams | No |
| `Mail.Read` | Outlook Email | No |
| `Calendars.Read` | Outlook Calendar | No |
| `OnlineMeetings.Read` | Outlook Calendar / meeting metadata resolution | No |
| `Team.ReadBasic.All` | Teams | No |
| `Channel.ReadBasic.All` | Teams | No |
| `Chat.Read` | Teams | No |

No permission in the current target scope set requires admin consent.

Teams Graph transcript content fetch is currently bypassed unless a legacy/manual grant
already contains `OnlineMeetingTranscript.Read.All`.

Source: Microsoft Graph permissions reference:
<https://learn.microsoft.com/en-us/graph/permissions-reference?view=graph-rest-1.0>
