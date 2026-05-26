# Integration App Manifests

This folder stores deployable third-party app manifests that are safe to keep
with backend infrastructure documentation.

## Tracked Here

- `slack-app/`: Slack app manifests for local, dev, and prod.
- `teams-app/`: Microsoft Teams app manifests, icons, and package build script
  for local, dev, and prod.
- `google-chat-app/`: Google Chat app config snapshots and setup notes.

These files describe app surfaces, callback URLs, scopes, domains, icons, and
bot metadata. They must not contain client secrets, signing secrets, bot tokens,
or OAuth access tokens.

## Not Tracked Here

Salesforce External Client App package working files are not stored here. The
local Salesforce packaging workspace under `.tmp-salesforce/` is generated and
ignored because retrieved Salesforce metadata can include global OAuth settings
and CLI/package build state that should not become backend source of truth.

The stable Salesforce package IDs and install URLs are documented in:

```text
docs/Technical Documentation/integrations/salesforce-external-client-app-packaging.md
```

If we later want Salesforce package source in Git, create a dedicated package
source folder or repository with only packageable, non-secret metadata:

- `ExternalClientApplication`
- `ExtlClntAppOauthSettings`

Do not include Salesforce client secrets, CLI auth output, or retrieved global
OAuth settings.
