# OAuth and Token Management

## Token Storage

Tokens are stored in the `knowledge_integrations` table per org schema:

```
KnowledgeIntegration
  credentials: JSONB         # {access_token, refresh_token, expires_in, scope, token_type}
  access_token_expires_at    # Parsed expiry timestamp
  connection_metadata: JSONB # {token_refresh_failures, email_folder_delta_links, ...}
```

OAuth client credentials (client_id, client_secret) are stored in Azure Key Vault per environment and loaded with a 6-hour TTL cache via `GoogleIntegrationConfig` and `MicrosoftIntegrationConfig` in `src/infra/config/configs.py`.

## OAuth Flow

### Connect (Authorization)

```
1. Frontend: GET /users/integrations/copilot-oauth/connect?type=outlook_email
2. Backend: Provider.get_authorization_url(state=encrypted_payload)
   - state contains: org_id, user_id, integration_type, redirect_url
3. Redirect to Microsoft/Google consent screen
4. User grants consent
5. Redirect to: /users/integrations/copilot-oauth/callback?code=...&state=...
6. copilot_callback_service.py:
   a. Decrypt state, validate org/user
   b. Provider.exchange_code(code) -> {access_token, refresh_token, ...}
   c. Upsert KnowledgeIntegration with credentials
   d. Dispatch Tier 3 initial sync task
```

### Token Refresh

Two layers protect against expired tokens:

#### Layer 1: Inline Refresh (during sync)

Located in `src/service/integrations/providers/token_resolution.py`:

**Proactive** (`get_access_token`): Before every API call, checks if the token expires within 5 minutes. If so, refreshes before the call.

**Reactive** (`call_with_token_retry`): Wraps the entire API call. If the call returns 401, refreshes the token and retries once. Used by all Google fetchers and all Microsoft fetchers.

**Distributed lock**: Token refresh uses a Redis lock (`token_refresh:{integration_id}`, TTL=30s) to prevent concurrent refresh calls for the same integration across multiple workers.

```python
# How fetchers use it (e.g., outlook_email.py):
async def _do_fetch(token: str) -> list[dict]:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        return await graph_api_request(client, endpoint)

return await call_with_token_retry(token_info, _do_fetch)
```

#### Layer 2: Scheduled Refresh (maintenance)

Located in `src/service/integrations/custom_token_refresh_service.py`, triggered by Celery Beat every 4 hours:

- Finds integrations with tokens expiring within 2 hours
- Calls `provider.refresh_token(refresh_token)`
- Persists new tokens to DB
- Deactivates integration after 3 consecutive refresh failures

### Permanent Auth Error Detection

When a token refresh fails, `_is_permanent_auth_error()` in `token_resolution.py` inspects the raw HTTP response to distinguish recoverable errors from permanent ones:

**Google** (HTTP 400):
- `invalid_grant` — user revoked access, password change, 6-month inactivity
- `admin_policy_enforced` — Workspace admin restricted app
- `invalid_client` — client secret rotated

**Microsoft** (HTTP 400 with AADSTS codes):
- `AADSTS50173/70008/700082/700084` — token expired/revoked
- `AADSTS65001/70001` — consent revoked or app disabled
- `AADSTS50076/50078/53003` — MFA/Conditional Access requires interactive re-auth

When detected, raises `AuthTokenRevokedError` which sets `requires_reauth=True` and `is_active=False` on the integration. The user must re-authenticate.

**Why providers re-raise raw HTTPStatusError**: `google_provider.py` and `microsoft_provider.py` do NOT wrap `HTTPStatusError` in a generic `Exception`. The raw response body must reach `_is_permanent_auth_error()` for AADSTS/invalid_grant detection to work.

## Per-Environment App Registrations

Each environment has its own Microsoft Azure AD app registration (Terraform-managed via `modules/microsoft_oauth_app`):

| Environment | App Name | Key Vault |
|-------------|----------|-----------|
| Local | CopilotGTM MS-Integration - Local | app-master-vault-local |
| Dev | CopilotGTM MS-Integration - Dev | copilot-master-kv-dev |
| Prod | CopilotGTM MS-Integration - Prod | copilot-master-kv-prod |

This isolates rate limits, consent screens, and tokens across environments. Google OAuth clients are created manually in Google Cloud Console.
