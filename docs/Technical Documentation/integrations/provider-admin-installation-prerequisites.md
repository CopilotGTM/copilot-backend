# Provider Admin Installation Prerequisites

Some OAuth providers allow a user to authorize immediately. Others require a
tenant admin to install, approve, or trust the CopilotGTM provider app before
normal OAuth can succeed.

This document defines the product pattern for those provider-level prerequisites.
Salesforce is the first confirmed case for this pattern.

## Problem Pattern

The backend can have a valid OAuth client ID, client secret, redirect URI, and
authorization URL, but the provider can still reject the OAuth callback because
the customer's tenant has not installed or approved the provider app.

This is not a credential problem in CopilotGTM. It is a provider-side
distribution or tenant-trust prerequisite.

Examples:

| Provider | Admin prerequisite | Result if missing |
| --- | --- | --- |
| Salesforce External Client App | Target Salesforce org must install the CopilotGTM managed package containing the External Client App | OAuth callback can fail with `OAUTH_EC_APP_NOT_FOUND` |
| Microsoft Entra | Tenant admin may need to approve delegated scopes | OAuth can return admin-consent-required errors |

## Product Flow

Use this flow for any provider that has an admin prerequisite:

1. User opens the integration card.
2. If the provider has an admin install or approval prerequisite, show the admin
   action first.
3. Admin completes the provider-owned install or approval flow outside
   CopilotGTM.
4. User returns to CopilotGTM and starts the normal OAuth connect flow.
5. Backend exchanges the OAuth code and creates the integration record.

The admin action does not create a user OAuth token by itself. It only prepares
the provider tenant so the later user OAuth flow can succeed.

## Runtime Contract Proposal

Expose provider prerequisites through the existing
`GET /users/integrations/available` response instead of building provider-specific
frontend logic.

Recommended field names:

```json
{
  "integration_type": "salesforce",
  "name": "Salesforce",
  "availability_status": "beta",
  "admin_prerequisite": {
    "type": "install_url",
    "required_before_connect": true,
    "url": "https://login.salesforce.com/packaging/installPackage.apexp?p0=04tdM000000U44bQAC",
    "label": "Install CopilotGTM in Salesforce"
  }
}
```

Use a nested object instead of a Salesforce-only field so future providers can
reuse the same contract. For example, Microsoft admin consent can use the same
shape with `type: "admin_consent_url"` if we later decide to surface it in
`/available`.

## Configuration Naming

Provider-specific environment variables should stay explicit at the runtime
boundary. The generic API response can normalize them into
`admin_prerequisite`.

Recommended Salesforce variables:

| Variable | Purpose | Secret? |
| --- | --- | --- |
| `SALESFORCE_REDIRECT_URI` | OAuth callback registered in the Salesforce External Client App | No |
| `SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL` | Admin package install URL shown before connect | No |

Do not store OAuth client secrets, access tokens, Salesforce CLI auth output, or
retrieved global OAuth settings in Helm values or Git.

## Helm Values

For Salesforce, the install URL differs by CopilotGTM environment:

| CopilotGTM environment | OAuth app | Install URL host |
| --- | --- | --- |
| Dev | `CopilotGTMDev` | `login.salesforce.com`, because production Salesforce org admins may validate against the dev backend |
| Prod | `CopilotGTMProd` | `login.salesforce.com`, because production customers install into production Salesforce orgs |

The backend should continue loading Salesforce client ID and client secret from
Key Vault. Helm values should only carry non-secret redirect and install URLs.

## Frontend Behavior

When `admin_prerequisite.required_before_connect` is true:

1. Show an admin install or approval action on the provider card.
2. Open the provider-owned URL in a new tab/window.
3. Keep the normal connect action available after the admin confirms install.
4. If OAuth later returns a known prerequisite error, show the same admin action
   again instead of a generic connection failure.

For Salesforce, the user must still provide the target Salesforce login/My
Domain host when connecting a sandbox or custom domain. The install URL and the
OAuth base URL are different:

| Value | Example | Used for |
| --- | --- | --- |
| Install URL | `https://login.salesforce.com/packaging/installPackage.apexp?p0=...` | Admin installs CopilotGTM External Client App package |
| OAuth base URL | `https://ameyo--gtmcopilot.sandbox.my.salesforce.com` | User authorizes CopilotGTM against the target org |

