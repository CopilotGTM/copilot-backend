# Salesforce External Client App Packaging

This note captures the Salesforce External Client App packaging state created on
May 26, 2026 for CopilotGTM Salesforce OAuth.

## Why Packages Are Needed

Salesforce returned this callback error when connecting the `ameyo--gtmcopilot`
sandbox with the CopilotGTM dev OAuth client:

```text
OAUTH_EC_APP_NOT_FOUND: External client app is not installed in this org
```

The root cause was that `CopilotGTMDev` existed in the CopilotGTM Salesforce
source org, but it was not installed in the target Salesforce sandbox org.
External Client Apps are not automatically available in every Salesforce org
just because they exist in the source org. Target orgs need an installable
package version.

This can look confusing because another Salesforce org may connect successfully.
That does not prove every external customer org can connect. A successful org
can already have a compatible CopilotGTM app installed, can be the app's source
org, or can be using a different Salesforce app distribution state. Any external
Salesforce org that has not installed the CopilotGTM External Client App package
can hit the same `OAUTH_EC_APP_NOT_FOUND` failure.

See also:

```text
docs/Technical Documentation/integrations/provider-admin-installation-prerequisites.md
```

## Salesforce Orgs

| Purpose | Details |
| --- | --- |
| Dev Hub / source org | Alias: `copilot-devhub`; org ID: `00DdM00000uXOg1UAG`; hosts `CopilotGTMDev` and `CopilotGTMProd` External Client Apps |
| Namespace org | Alias: `copilotgtm-namespace`; org ID: `00DgK00000PaqJRUAZ`; owns namespace `copilotgtm` |
| Namespace | `copilotgtm`, linked into the Dev Hub before package creation |

The namespace org should not be enabled as Dev Hub. Its role is only to own the
global namespace prefix used by managed 2GP packages.

## Package Versions

These package IDs and install URLs are not secrets. They are safe to document
and share with customer admins. Client secrets and access tokens must not be
committed.

| CopilotGTM environment | Salesforce app | Package | Package ID | Version | Subscriber Package Version ID | Production install URL |
| --- | --- | --- | --- | --- | --- | --- |
| Dev | `CopilotGTMDev` | `CopilotGTMDevECA` | `0HodM0000003f6fSAA` | `0.1.0.1` | `04tdM000000U44bQAC` | `https://login.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U44bQAC` |
| Prod | `CopilotGTMProd` | `CopilotGTMProdECA` | `0HodM0000003f8HSAQ` | `0.1.0.1` | `04tdM000000U4B3QAK` | `https://login.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U4B3QAK` |

For sandbox installs, use the same package version IDs with the sandbox host:

```text
https://test.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U44bQAC
https://test.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U4B3QAK
```

## What Was Packaged

Salesforce package version creation succeeded only after narrowing package
contents to:

- `ExternalClientApplication`
- `ExtlClntAppOauthSettings`

Salesforce rejected these metadata types for 2GP package version creation:

- `ExtlClntAppGlobalOauthSettings`: distributed through Salesforce global
  replication, not 2GP packaging.
- `ExtlClntAppOauthConfigurablePolicies`: not supported in the package version.

That means package versions install the External Client App shell and OAuth
settings. Global OAuth settings and subscriber policy behavior remain
Salesforce-managed and should be verified/configured in Salesforce, not stored
as backend runtime configuration.

## Install Flow

For a sandbox test:

1. Log in as a Salesforce admin for the target sandbox.
2. Open the dev package sandbox install URL:
   `https://test.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U44bQAC`
3. Install the package.
4. Return to CopilotGTM dev.
5. Connect Salesforce with the target My Domain OAuth host, for example:
   `https://ameyo--gtmcopilot.sandbox.my.salesforce.com`

For production customer onboarding, use the prod package install URL against
`login.salesforce.com`.

## Runtime Configuration

The backend OAuth runtime has two separate concerns:

| Concern | Current source | Secret? | Notes |
| --- | --- | --- | --- |
| Salesforce OAuth client ID | Azure Key Vault secret `salesforce-integration-client-id` | No, but do not casually expose it |
| Salesforce OAuth client secret | Azure Key Vault secret `salesforce-integration-client-secret` | Yes |
| OAuth callback URL | `SALESFORCE_REDIRECT_URI` in Helm values | No |
| Package install URL | Proposed `SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL` in Helm values | No |

Do not move Salesforce OAuth client secret into Helm values. The client ID and
secret should stay in Key Vault. The install URL is safe in values files because
it only identifies an installable Salesforce package version.

Recommended values:

```yaml
# charts/application/values-dev.yaml
SALESFORCE_REDIRECT_URI: "https://devapi.copilotgtm.com/users/integrations/copilot-oauth/callback"
SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL: "https://test.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U44bQAC"

# charts/application/values-prod.yaml
SALESFORCE_REDIRECT_URI: "https://api.copilotgtm.com/users/integrations/copilot-oauth/callback"
SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL: "https://login.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U4B3QAK"
```

`SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL` should be surfaced through the
generic `admin_prerequisite` response described in
`provider-admin-installation-prerequisites.md`. The frontend should show this as
an admin install step before starting normal Salesforce OAuth.

The install URL is not the same as the OAuth base URL the user enters for a
custom Salesforce org:

| Field | Example | Purpose |
| --- | --- | --- |
| `SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL` | `https://test.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U44bQAC` | Admin installs CopilotGTM's External Client App package into the target Salesforce org |
| `oauth_base_url` request field | `https://ameyo--gtmcopilot.sandbox.my.salesforce.com` | OAuth authorization/token exchange host for that target Salesforce org |

## CLI Commands Used

The package project was generated in `.tmp-salesforce/copilot-eca` and is
intentionally ignored by Git. Recreate it from Salesforce metadata when needed.

```powershell
sf org login web --set-default-dev-hub --alias copilot-devhub
sf org login web --alias copilotgtm-namespace

sf project generate --name copilot-eca --output-dir .tmp-salesforce
cd .tmp-salesforce/copilot-eca

sf project retrieve start --target-org copilot-devhub --metadata ExternalClientApplication
sf project retrieve start --target-org copilot-devhub --metadata ExtlClntAppOauthSettings

# sfdx-project.json must include:
# "namespace": "copilotgtm"

sf package create --name CopilotGTMDevECA --package-type Managed --path force-app-dev --target-dev-hub copilot-devhub
sf package version create --package CopilotGTMDevECA --installation-key-bypass --wait 20 --target-dev-hub copilot-devhub

sf package create --name CopilotGTMProdECA --package-type Managed --path force-app-prod --target-dev-hub copilot-devhub
sf package version create --package CopilotGTMProdECA --path force-app-prod --installation-key-bypass --wait 20 --target-dev-hub copilot-devhub
```

## What Belongs In Git

Keep this repo limited to stable documentation and backend runtime code:

- Commit this doc and package IDs/install URLs.
- Do not commit `.tmp-salesforce/`; it is generated local packaging state.
- Do not commit retrieved `ExtlClntAppGlobalOauthSettings` files, because they
  include OAuth client identifiers and Salesforce rejects them for 2GP packaging.
- Do not commit Salesforce client secrets or CLI auth outputs.

Slack and Teams are different: their environment manifests in
`infra/manifests/slack-app/` and `infra/manifests/teams-app/` are already
committed because they are deployable app manifests and do not include client
secrets. Salesforce package metadata should stay generated unless we create a
dedicated Salesforce packaging repo or package-source folder with only
packageable, non-secret metadata.
