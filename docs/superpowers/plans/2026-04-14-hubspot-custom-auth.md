# HubSpot Custom Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HubSpot as a first-party custom OAuth integration that can be connected, stored, refreshed, and disconnected through the existing production integration architecture, while leaving CRM sync disabled and explicitly marked as pending.

**Architecture:** Extend the current provider-based custom OAuth stack rather than reviving deprecated CRM auth code. Add a dedicated `HubSpotProvider`, a `HubSpotIntegrationConfig`, wire HubSpot into `ProviderFactory`, update metadata to expose the integration, and persist a post-connect placeholder in `connection_metadata` so the record is truthful about sync not being implemented yet.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy async, Pydantic v2, httpx, Azure Key Vault-backed config, pytest

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `src/service/integrations/providers/hubspot_provider.py` | HubSpot OAuth provider implementation |
| `tests/service/integrations/providers/test_hubspot_provider.py` | Provider-level tests for HubSpot auth URL and payload normalization |
| `tests/service/integrations/test_hubspot_oauth_flow.py` | Service-level tests for callback, reactivation, and placeholder metadata |

### Modified Files

| File | Change |
|------|--------|
| `src/infra/config/configs.py` | Add `HubSpotIntegrationConfig` and singleton export |
| `src/service/integrations/providers/provider_factory.py` | Register `IntegrationType.HUBSPOT` |
| `src/infra/config/integrations_metadata.py` | Move HubSpot from deprecated to beta and keep org-scoped |
| `src/service/integrations/initial_sync_service.py` | Add explicit HubSpot no-op placeholder branch |
| `src/service/integrations/providers/__init__.py` | Export `HubSpotProvider` if this package re-exports providers |
| `src/service/integrations/copilot_callback_service.py` | Persist initial-sync placeholder metadata for HubSpot |
| `docs/Technical Documentation/integrations/architecture-overview.md` | Document HubSpot custom auth support |
| `docs/Technical Documentation/integrations/oauth-and-token-management.md` | Document HubSpot auth, refresh, revoke, and placeholder behavior |
| `docs/Technical Documentation/integrations/sync-by-provider.md` | Clarify HubSpot auth supported but CRM sync deferred |

---

## Task 1: Add HubSpot Config

**Files:**
- Modify: `src/infra/config/configs.py`

- [ ] **Step 1: Add `HubSpotIntegrationConfig` next to Google and Microsoft configs**

```python
class HubSpotIntegrationConfig:
    """Configuration for custom HubSpot OAuth flows."""

    SECRET_NAME_CLIENT_ID: str = "hubspot-integration-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "hubspot-integration-client-secret"

    REDIRECT_URI: str = os.getenv("HUBSPOT_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))
    HUBSPOT_HTTP_TIMEOUT_SEC: float = float(os.getenv("HUBSPOT_HTTP_TIMEOUT_SEC", "10"))

    _client_id: str | None = os.getenv("HUBSPOT_CLIENT_ID")
    _client_secret: str | None = os.getenv("HUBSPOT_CLIENT_SECRET")
    _credentials_from_env: bool = bool(
        os.getenv("HUBSPOT_CLIENT_ID") and os.getenv("HUBSPOT_CLIENT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("HUBSPOT_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None
```

- [ ] **Step 2: Add the expiry, vault-load, and validation helpers**

```python
    async def get_credentials(self) -> tuple[str, str]:
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""
        if not self._is_expired():
            return self._client_id or "", self._client_secret or ""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            if not self._is_expired():
                return self._client_id or "", self._client_secret or ""
            await self._load_from_vault()
        return self._client_id or "", self._client_secret or ""

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time
        return ((time.time() - self._credentials_loaded_at) / 3600) >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time
        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading HubSpot OAuth credentials from Key Vault (TTL refresh)")
        self._client_id = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self._client_secret = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()
```

- [ ] **Step 3: Export the singleton**

```python
hubspot_integration_config = HubSpotIntegrationConfig()
```

- [ ] **Step 4: Run a focused import check**

Run: `uv run python -c "from src.infra.config.configs import hubspot_integration_config; print(bool(hubspot_integration_config))"`

Expected: `True`

---

## Task 2: Implement `HubSpotProvider`

**Files:**
- Create: `src/service/integrations/providers/hubspot_provider.py`
- Modify: `src/service/integrations/providers/__init__.py`

- [ ] **Step 1: Create the provider skeleton**

```python
from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import Any

import httpx

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import hubspot_integration_config
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


class HubSpotProvider(BaseIntegrationProvider):
    AUTHORIZATION_BASE_URL = "https://app.hubspot.com/oauth/authorize"
    TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
    REVOKE_URL = "https://api.hubapi.com/oauth/v1/token/revoke"
    ACCESS_TOKEN_INFO_URL = "https://api.hubapi.com/oauth/v1/access-tokens/{token}"
```

- [ ] **Step 2: Add lazy credential loading, `provider_key`, and scopes**

```python
    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.redirect_uri = hubspot_integration_config.REDIRECT_URI
        self.timeout = hubspot_integration_config.HUBSPOT_HTTP_TIMEOUT_SEC
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def _ensure_credentials(self) -> None:
        if self._client_id and self._client_secret:
            return
        client_id, client_secret = await hubspot_integration_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError("HubSpot OAuth credentials not available")
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "hubspot"

    def _get_scopes(self) -> list[str]:
        return [
            "oauth",
            "crm.objects.deals.read",
            "crm.objects.contacts.read",
            "crm.objects.companies.read",
        ]
```

- [ ] **Step 3: Add the shared HTTP retry helper**

```python
    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(_MAX_RETRIES + 1):
                response = (
                    await client.post(url, data=data, headers=headers)
                    if method == "POST"
                    else await client.get(url, headers=headers)
                )
                if response.status_code != 429 or attempt >= _MAX_RETRIES:
                    return response
                retry_after = response.headers.get("Retry-After")
                wait = min(float(retry_after), _MAX_BACKOFF_SEC) if retry_after else min(
                    _BASE_BACKOFF_SEC * (2**attempt), _MAX_BACKOFF_SEC
                )
                await asyncio.sleep(wait + random.uniform(0, wait * 0.5))  # noqa: S311  # nosec B311
        raise RuntimeError("Exhausted retries without response")
```

- [ ] **Step 4: Implement authorization URL and code exchange**

```python
    async def get_authorization_url(self, state: str) -> str:
        await self._ensure_credentials()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self._get_scopes()),
            "state": state,
        }
        return f"{self.AUTHORIZATION_BASE_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri or self.redirect_uri,
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise ValueError("HubSpot OAuth exchange returned an invalid payload")
```

- [ ] **Step 5: Implement refresh, revoke, and user info normalization**

```python
    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise ValueError("HubSpot token refresh returned an invalid payload")

    async def revoke_token(self, token: str) -> None:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.REVOKE_URL,
            data={
                "token": token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        response = await self._request_with_retry(
            "GET",
            self.ACCESS_TOKEN_INFO_URL.format(token=access_token),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HubSpot access token info returned an invalid payload")
        email = str(payload.get("user") or payload.get("user_email") or "").strip().lower()
        if not email:
            raise ValueError("HubSpot OAuth response did not include a connected account email")
        return {
            "email": email,
            "hubspot_user_id": payload.get("user_id"),
            "hubspot_portal_id": payload.get("hub_id") or payload.get("hub_domain"),
            "hubspot_portal_name": payload.get("hub_domain"),
        }
```

- [ ] **Step 6: Export the provider in the package if needed**

```python
from .hubspot_provider import HubSpotProvider
```

- [ ] **Step 7: Run a focused import check**

Run: `uv run python -c "from src.service.integrations.providers.hubspot_provider import HubSpotProvider; print(HubSpotProvider.__name__)"`

Expected: `HubSpotProvider`

---

## Task 3: Wire HubSpot Into the Existing OAuth Stack

**Files:**
- Modify: `src/service/integrations/providers/provider_factory.py`
- Modify: `src/infra/config/integrations_metadata.py`

- [ ] **Step 1: Register HubSpot in `ProviderFactory`**

```python
from src.service.integrations.providers.hubspot_provider import HubSpotProvider


class ProviderFactory:
    _providers: dict[IntegrationType, type[BaseIntegrationProvider]] = {
        IntegrationType.GMAIL: GoogleProvider,
        IntegrationType.GOOGLE_CALENDAR: GoogleProvider,
        IntegrationType.OUTLOOK_EMAIL: MicrosoftProvider,
        IntegrationType.OUTLOOK_CALENDAR: MicrosoftProvider,
        IntegrationType.TEAMS: MicrosoftProvider,
        IntegrationType.SLACK: SlackProvider,
        IntegrationType.HUBSPOT: HubSpotProvider,
    }
```

- [ ] **Step 2: Update HubSpot integration metadata**

```python
    IntegrationType.HUBSPOT: IntegrationMetadata(
        name="HubSpot",
        description="Connect HubSpot using Copilot's custom OAuth flow. CRM sync setup follows separately.",
        logo_url="https://api.nango.dev/images/template-logos/hubspot.svg",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.BETA,
        oauth_scopes=[
            "oauth",
            "crm.objects.deals.read",
            "crm.objects.contacts.read",
            "crm.objects.companies.read",
        ],
        documentation_url="https://developers.hubspot.com/docs/api/overview",
        default_sync_frequency="manual",
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
```

- [ ] **Step 3: Verify the metadata surface**

Run: `uv run python -c "from src.infra.config.integrations_metadata import INTEGRATION_METADATA; from src.domain.models.organisation_tables.sync import IntegrationType; print(INTEGRATION_METADATA[IntegrationType.HUBSPOT].availability_status)"`

Expected: `beta`

---

## Task 4: Add Explicit Post-Connect Placeholder Behavior

**Files:**
- Modify: `src/service/integrations/copilot_callback_service.py`
- Modify: `src/service/integrations/initial_sync_service.py`

- [ ] **Step 1: Add a helper in `CopilotCallbackService` to mark HubSpot sync as pending**

```python
    @staticmethod
    def _apply_initial_sync_placeholder(
        integration: KnowledgeIntegration,
        *,
        integration_type: IntegrationType,
    ) -> None:
        if integration_type != IntegrationType.HUBSPOT:
            return

        metadata = dict(integration.connection_metadata or {})
        metadata["initial_sync_status"] = "not_implemented"
        metadata["initial_sync_placeholder"] = (
            "HubSpot custom auth connected successfully; CRM sync dispatch will be added later."
        )
        integration.connection_metadata = metadata
```

- [ ] **Step 2: Call the helper immediately after custom token metadata is applied**

```python
        self._apply_custom_token_payload(
            integration,
            integration_type=integration_type,
            provider_key=provider_key,
            token_payload=tokens,
        )
        self._apply_initial_sync_placeholder(
            integration,
            integration_type=integration_type,
        )
```

- [ ] **Step 3: Make `InitialSyncService` explicitly no-op for HubSpot**

```python
    CRM_PLACEHOLDER_INTEGRATION_TYPES = {IntegrationType.HUBSPOT}

    def trigger_initial_sync(
        self,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> None:
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)
        org_id_str = str(organisation_id)
        int_id_str = str(integration.integration_id)
        if integration_type in self.CRM_PLACEHOLDER_INTEGRATION_TYPES:
            logger.info(
                "Initial sync placeholder only for integration type %s (integration=%s)",
                integration_type.value,
                int_id_str,
            )
            return
```

- [ ] **Step 4: Run a focused import check**

Run: `uv run python -c "from src.service.integrations.copilot_callback_service import CopilotCallbackService; from src.service.integrations.initial_sync_service import InitialSyncService; print(CopilotCallbackService.__name__, InitialSyncService.__name__)"`

Expected: `CopilotCallbackService InitialSyncService`

---

## Task 5: Add Tests for Connect, Reactivate, Refresh, and Disconnect

**Files:**
- Create: `tests/service/integrations/providers/test_hubspot_provider.py`
- Create: `tests/service/integrations/test_hubspot_oauth_flow.py`

- [ ] **Step 1: Add a provider test for authorization URL generation**

```python
from urllib.parse import parse_qs, urlparse

import pytest

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.hubspot_provider import HubSpotProvider


@pytest.mark.asyncio
async def test_hubspot_provider_builds_authorization_url():
    provider = HubSpotProvider(IntegrationType.HUBSPOT)
    provider._client_id = "hubspot-client-id"
    provider._client_secret = "hubspot-client-secret"
    provider.redirect_uri = "https://app.example.com/api/v1/users/integrations/copilot-oauth/callback"

    url = await provider.get_authorization_url("signed-state")

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "app.hubspot.com"
    assert params["client_id"] == ["hubspot-client-id"]
    assert params["state"] == ["signed-state"]
```

- [ ] **Step 2: Add a provider test for user-info normalization**

```python
@pytest.mark.asyncio
async def test_hubspot_provider_normalizes_user_info(monkeypatch):
    provider = HubSpotProvider(IntegrationType.HUBSPOT)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "user": "sales@acme.com",
                "user_id": 42,
                "hub_id": 777,
                "hub_domain": "acme",
            }

    async def fake_request(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(provider, "_request_with_retry", fake_request)
    payload = await provider.get_user_info("token")

    assert payload["email"] == "sales@acme.com"
    assert payload["hubspot_user_id"] == 42
```

- [ ] **Step 3: Add callback tests for create and reactivate**

```python
from sqlalchemy import select

from src.domain.models.organisation_tables.sync import KnowledgeIntegration


async def test_callback_creates_hubspot_integration_with_placeholder_metadata(
    hubspot_callback_service,
    hubspot_state_token,
    org_session,
):
    result = await hubspot_callback_service.handle_oauth_callback(
        code="abc",
        state=hubspot_state_token,
    )
    assert result["integration_type"] == "hubspot"
    integration = (await org_session.execute(select(KnowledgeIntegration))).scalar_one()
    assert integration.connection_metadata["provider_key"] == "hubspot"
    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"


async def test_callback_reactivates_existing_hubspot_integration(
    hubspot_callback_service,
    existing_hubspot_integration,
    hubspot_state_token,
):
    result = await hubspot_callback_service.handle_oauth_callback(
        code="abc",
        state=hubspot_state_token,
    )
    assert result["reactivated"] is True
```

- [ ] **Step 4: Add disconnect and refresh-path coverage**

```python
async def test_disconnect_revokes_hubspot_token_and_marks_inactive(
    integration_service,
    hubspot_integration,
    fake_hubspot_provider,
):
    await integration_service.revoke_custom_oauth_token(hubspot_integration)
    assert fake_hubspot_provider.revoked_token == "refresh-token"


async def test_custom_token_refresh_uses_hubspot_provider(
    token_refresh_service,
    hubspot_integration,
    fake_hubspot_provider,
):
    result = await token_refresh_service.refresh_single_integration_token(
        hubspot_integration.integration_id
    )
    assert result["success"] is True
```

- [ ] **Step 5: Run the targeted tests**

Run: `uv run pytest tests/service/integrations/providers/test_hubspot_provider.py tests/service/integrations/test_hubspot_oauth_flow.py -v`

Expected: all tests pass

---

## Task 6: Update Integration Documentation

**Files:**
- Modify: `docs/Technical Documentation/integrations/architecture-overview.md`
- Modify: `docs/Technical Documentation/integrations/oauth-and-token-management.md`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

- [ ] **Step 1: Update architecture overview**

Replace the current fully-deprecated HubSpot wording with:

```md
HubSpot custom OAuth is supported through the first-party provider architecture. CRM sync for HubSpot remains deferred and is not part of the active sync worker pipeline yet. Salesforce CRM remains deprecated.
```

- [ ] **Step 2: Update OAuth documentation**

```md
### HubSpot

HubSpot uses the same first-party custom OAuth flow as Google, Microsoft, and Slack:

- authorization URL built by `HubSpotProvider`
- callback handled by `copilot_callback_service.py`
- tokens stored in `knowledge_integrations.credentials`
- scheduled refresh handled by `custom_token_refresh_service.py`

Current limitation:

- successful connect stores `initial_sync_status = not_implemented` in `connection_metadata`
- no HubSpot CRM sync tasks are dispatched in this phase
```

- [ ] **Step 3: Update sync-by-provider documentation**

```md
### HubSpot CRM

- **Custom OAuth**: Supported via the current first-party provider architecture
- **Data sync**: Deferred
- **Current behavior after connect**: Integration is stored as active and marked with an initial-sync placeholder in `connection_metadata`
```

- [ ] **Step 4: Verify doc wording**

Run: `rg -n "HubSpot|custom OAuth|initial_sync_status|deferred" "docs/Technical Documentation/integrations"`

Expected: HubSpot auth is documented as supported while sync remains deferred

---

## Task 7: Final Verification

**Files:**
- Verify only

- [ ] **Step 1: Run the targeted auth-path tests again**

Run: `uv run pytest tests/service/integrations/providers/test_hubspot_provider.py tests/service/integrations/test_hubspot_oauth_flow.py -v`

Expected: PASS

- [ ] **Step 2: Run a focused compile sweep**

Run: `uv run python -m compileall src/service/integrations/providers src/service/integrations src/infra/config`

Expected: no syntax errors

- [ ] **Step 3: Run a narrow lint pass on touched files**

Run: `uv run ruff check src/service/integrations/providers/hubspot_provider.py src/service/integrations/providers/provider_factory.py src/service/integrations/copilot_callback_service.py src/service/integrations/initial_sync_service.py src/infra/config/configs.py src/infra/config/integrations_metadata.py tests/service/integrations/providers/test_hubspot_provider.py tests/service/integrations/test_hubspot_oauth_flow.py`

Expected: all checks pass

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-04-14-hubspot-custom-auth-design.md docs/superpowers/plans/2026-04-14-hubspot-custom-auth.md src/infra/config/configs.py src/service/integrations/providers/hubspot_provider.py src/service/integrations/providers/provider_factory.py src/service/integrations/copilot_callback_service.py src/service/integrations/initial_sync_service.py src/infra/config/integrations_metadata.py tests/service/integrations/providers/test_hubspot_provider.py tests/service/integrations/test_hubspot_oauth_flow.py docs/Technical\ Documentation/integrations/architecture-overview.md docs/Technical\ Documentation/integrations/oauth-and-token-management.md docs/Technical\ Documentation/integrations/sync-by-provider.md
git commit -m "feat: add hubspot custom oauth support"
```

- [ ] **Step 5: Record completion criteria**

Success means:

- HubSpot appears in available integrations
- authorize route can build a HubSpot auth URL
- callback stores or reactivates an organization-scoped `KnowledgeIntegration`
- connection metadata includes the initial-sync placeholder
- disconnect and scheduled refresh use `HubSpotProvider`
- docs describe HubSpot auth as supported and sync as deferred
