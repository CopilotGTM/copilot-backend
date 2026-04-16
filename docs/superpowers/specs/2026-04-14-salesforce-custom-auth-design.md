# Salesforce Custom OAuth Design

**Date:** 2026-04-14
**Status:** Approved for planning
**Scope:** Backend-only custom OAuth for Salesforce using the current first-party integration architecture

---

## Overview

CopilotGTM should support Salesforce connection through the same production OAuth path already used for Google, Microsoft, Slack, and HubSpot. This work restores Salesforce as a first-class custom OAuth provider without restoring any CRM sync pipeline yet.

The outcome of this phase is intentionally narrow:

- A user can initiate Salesforce OAuth from the existing Copilot OAuth routes.
- The callback exchanges tokens and stores or reactivates a `KnowledgeIntegration`.
- The connection remains `ORGANIZATION` scoped.
- Disconnect and scheduled token refresh work through the shared provider infrastructure.
- A metadata placeholder records that initial sync is still pending implementation.

This phase does **not** add Salesforce deal sync, contact sync, account sync, enrichment, worker tasks, or webhook processing.

---

## Goals

1. Add Salesforce to the current custom OAuth provider architecture.
2. Keep all auth logic inside the shared provider abstraction instead of special-casing CRM in the callback.
3. Preserve current organization isolation and duplicate-connection safety.
4. Make the post-connect state explicit by storing metadata that sync dispatch is not implemented yet.
5. Update integration docs so Salesforce auth is documented as active even while CRM sync remains deferred.

## Non-Goals

1. No Salesforce sync tasks, providers, or worker registry changes.
2. No initial CRM import or webhook-triggered sync.
3. No frontend redesign beyond exposing Salesforce through the existing available integrations flow.
4. No migration of deprecated CRM sync code back into `src/`.

---

## Current Architecture Anchors

The current production auth flow already exists in these files:

- `src/service/integrations/providers/base_provider.py`
- `src/service/integrations/providers/provider_factory.py`
- `src/service/integrations/copilot_oauth_service.py`
- `src/service/integrations/copilot_callback_service.py`
- `src/service/integrations/custom_token_refresh_service.py`
- `src/service/integrations/initial_sync_service.py`
- `src/api/routers/integrations/copilot_oauth_routes.py`
- `src/infra/config/integrations_metadata.py`
- `src/infra/config/configs.py`

Salesforce must fit into that path instead of reviving the deprecated CRM sync flow.

---

## Proposed Design

### 1. Provider implementation

Add `src/service/integrations/providers/salesforce_provider.py` implementing `BaseIntegrationProvider`.

Responsibilities:

- Build Salesforce authorization URLs.
- Exchange authorization codes for tokens.
- Refresh tokens.
- Revoke tokens on disconnect.
- Fetch connected account identity and normalize it to the callback contract.

### 2. Salesforce config

Add `SalesforceIntegrationConfig` in `src/infra/config/configs.py` with the same pattern used by Google, Microsoft, and HubSpot:

- env vars for local development
- Azure Key Vault secret fallback for non-local environments
- in-memory TTL cache
- provider-specific redirect URI and HTTP timeout

Proposed config surface:

- `SALESFORCE_CLIENT_ID`
- `SALESFORCE_CLIENT_SECRET`
- `SALESFORCE_REDIRECT_URI`
- `SALESFORCE_HTTP_TIMEOUT_SEC`
- `SALESFORCE_CREDENTIALS_TTL_HOURS`
- Key Vault secret names:
  - `salesforce-integration-client-id`
  - `salesforce-integration-client-secret`

### 3. Provider registration

Register `IntegrationType.SALESFORCE` in `ProviderFactory`.

This automatically enables:

- OAuth initiation through `CopilotOAuthService`
- callback token exchange through `CopilotCallbackService`
- disconnect token revocation through `IntegrationService`
- scheduled token refresh through `CustomTokenRefreshService`

### 4. Integration metadata

Update Salesforce metadata in `src/infra/config/integrations_metadata.py`.

Required changes:

- move Salesforce from `DEPRECATED`
- mark it `BETA` for this phase
- keep category as CRM
- keep `default_scope=IntegrationScope.ORGANIZATION`
- keep `allow_multiple_connections=False`
- set OAuth scopes to the minimum set needed for current auth and near-term CRM sync work

The practical effect is that Salesforce becomes visible again in `/users/integrations/available` because that route currently hides deprecated integrations.

### 5. Callback and post-connect placeholder

The callback service should continue to own persistence and reactivation logic. No Salesforce-specific alternate path is needed.

After a successful Salesforce connect or reactivate, store a metadata placeholder under `connection_metadata`, for example:

```json
{
  "auth_backend": "custom",
  "provider_key": "salesforce",
  "provider_config_key": "custom-salesforce",
  "initial_sync_status": "not_implemented",
  "initial_sync_placeholder": "Salesforce custom auth connected successfully; CRM sync dispatch will be added later."
}
```

This preserves honest state without pretending that sync has run.

`InitialSyncService` should not dispatch any Salesforce task yet. It should explicitly no-op for Salesforce with a log message so the behavior is intentional rather than ambiguous. This should live in the same post-connect service layer that already owns the HubSpot placeholder, not in the callback service.

### 6. Connected account identity

`CopilotCallbackService` expects `provider.get_user_info()` to return an `email` key.

For Salesforce, the provider should normalize the connected identity into:

```python
{
    "email": "sales@acme.com",
    "salesforce_user_id": "005...",
    "salesforce_organization_id": "00D...",
    "salesforce_instance_url": "https://mydomain.my.salesforce.com",
    "salesforce_username": "sales@acme.com",
}
```

The provider should resolve this from Salesforce identity data after token exchange and fail the callback if no work email can be established.

This keeps the callback service unchanged and preserves the duplicate-account checks already in place.

---

## Data and Security Rules

1. Salesforce remains `ORGANIZATION` scoped.
2. Only one active Salesforce integration can exist per organization.
3. The existing callback duplicate guard remains in force, preventing conflicting connections.
4. The callback continues to validate the signed JWT state and cross-check the user in master DB.
5. Tokens stay in `knowledge_integrations.credentials`.
6. Any provider-specific extra identity data belongs in `connection_metadata`, not new schema columns.

---

## Error Handling

Salesforce should follow the same resilience model as other custom providers:

1. OAuth exchange and refresh use provider-local HTTP retry for transient `429` responses.
2. Refresh should re-raise raw `HTTPStatusError` when possible so shared auth-failure classification can inspect provider responses later.
3. Disconnect should attempt revoke and still allow local deactivation if revoke fails.
4. Callback should fail closed if connected account email cannot be resolved.

Because Salesforce sync is not part of this phase, no worker-side retry or sync-history changes are needed.

---

## Documentation Changes

Update these docs after implementation:

- `docs/Technical Documentation/integrations/architecture-overview.md`
- `docs/Technical Documentation/integrations/oauth-and-token-management.md`
- `docs/Technical Documentation/integrations/sync-by-provider.md`

Required documentation outcome:

- Salesforce is documented as supported for custom OAuth.
- Salesforce sync is still documented as deferred / not yet restored.
- The placeholder post-connect state is documented so support/debugging is straightforward.

---

## Testing Strategy

Focus on auth-path coverage, not CRM sync coverage.

Required test areas:

1. ProviderFactory returns `SalesforceProvider` for `IntegrationType.SALESFORCE`.
2. `CopilotOAuthService.initiate_oauth_flow()` returns a valid Salesforce authorization URL.
3. Callback creates a new Salesforce `KnowledgeIntegration` with correct scope and metadata placeholder.
4. Callback reactivates a disconnected Salesforce integration instead of creating a duplicate.
5. Disconnect calls `revoke_token()` and marks the integration inactive.
6. Scheduled refresh uses `SalesforceProvider.refresh_token()` and persists updated tokens.
7. Callback rejects personal-email connections and duplicate-account conflicts using the same guard rails as other org-scoped providers.

---

## Open Decisions Resolved

1. **Scope:** organization-scoped
2. **Sync in this phase:** no
3. **Post-connect behavior:** store placeholder metadata indicating initial sync dispatch is not implemented yet
4. **Architecture choice:** current production custom OAuth path, not deprecated CRM auth

---

## Implementation Boundary

This design is complete when Salesforce can be connected, stored, refreshed, and disconnected through the custom OAuth stack, and the system makes it explicit that CRM sync has not yet been enabled.
