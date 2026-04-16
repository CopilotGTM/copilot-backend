# Salesforce Custom Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Salesforce as a first-party custom OAuth integration that can be connected, stored, refreshed, and disconnected through the existing production integration architecture, while leaving CRM sync disabled and explicitly marked as pending.

**Architecture:** Extend the current provider-based custom OAuth stack rather than reviving deprecated CRM auth code. Add a dedicated `SalesforceProvider`, a `SalesforceIntegrationConfig`, wire Salesforce into `ProviderFactory`, update metadata to expose the integration, and persist a post-connect placeholder in `connection_metadata` so the record is truthful about sync not being implemented yet.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy async, Pydantic v2, httpx, Azure Key Vault-backed config, pytest

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `src/service/integrations/providers/salesforce_provider.py` | Salesforce OAuth provider implementation |
| `tests/service/integrations/providers/test_salesforce_provider.py` | Provider-level tests for Salesforce auth URL and payload normalization |
| `tests/service/integrations/test_salesforce_oauth_flow.py` | Service-level tests for callback, reactivation, placeholder metadata, and auth guard rails |

### Modified Files

| File | Change |
|------|--------|
| `src/infra/config/configs.py` | Add `SalesforceIntegrationConfig` and singleton export |
| `src/service/integrations/providers/provider_factory.py` | Register `IntegrationType.SALESFORCE` |
| `src/service/integrations/providers/__init__.py` | Export `SalesforceProvider` |
| `src/infra/config/integrations_metadata.py` | Move Salesforce from deprecated to beta and keep org-scoped |
| `src/service/integrations/initial_sync_service.py` | Add explicit Salesforce post-connect placeholder and no-op initial-sync branch |
| `docs/Technical Documentation/integrations/architecture-overview.md` | Document Salesforce custom auth support |
| `docs/Technical Documentation/integrations/oauth-and-token-management.md` | Document Salesforce auth, refresh, revoke, and placeholder behavior |
| `docs/Technical Documentation/integrations/sync-by-provider.md` | Clarify Salesforce auth supported but CRM sync deferred |

---

## Task 1: Add Salesforce Config

**Files:**
- Modify: `src/infra/config/configs.py`
- Test: `tests/infra/config/test_salesforce_integration_config.py`

- [ ] **Step 1: Write the failing config test**

```python
import pytest

from src.infra.config.configs import SalesforceIntegrationConfig


@pytest.mark.asyncio
async def test_salesforce_config_uses_env_credentials(monkeypatch):
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "sf-client-id")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "sf-client-secret")

    config = SalesforceIntegrationConfig()

    client_id, client_secret = await config.get_credentials()

    assert client_id == "sf-client-id"
    assert client_secret == "sf-client-secret"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/infra/config/test_salesforce_integration_config.py -q`

Expected: FAIL with `ImportError` or `AttributeError` because `SalesforceIntegrationConfig` does not exist yet

- [ ] **Step 3: Write the minimal config implementation**

```python
class SalesforceIntegrationConfig:
    """Configuration for custom Salesforce OAuth flows."""

    SECRET_NAME_CLIENT_ID: str = "salesforce-integration-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "salesforce-integration-client-secret"

    REDIRECT_URI: str = os.getenv("SALESFORCE_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))
    SALESFORCE_HTTP_TIMEOUT_SEC: float = float(os.getenv("SALESFORCE_HTTP_TIMEOUT_SEC", "10"))

    _client_id: str | None = os.getenv("SALESFORCE_CLIENT_ID")
    _client_secret: str | None = os.getenv("SALESFORCE_CLIENT_SECRET")
    _credentials_from_env: bool = bool(
        os.getenv("SALESFORCE_CLIENT_ID") and os.getenv("SALESFORCE_CLIENT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("SALESFORCE_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

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
        return ((time.time() - self._credentials_loaded_at) / 3600) >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        logger.info("Loading Salesforce OAuth credentials from Key Vault (TTL refresh)")
        self._client_id = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self._client_secret = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()


salesforce_integration_config = SalesforceIntegrationConfig()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/infra/config/test_salesforce_integration_config.py -q`

Expected: PASS

---

## Task 2: Implement `SalesforceProvider`

**Files:**
- Create: `src/service/integrations/providers/salesforce_provider.py`
- Modify: `src/service/integrations/providers/__init__.py`
- Test: `tests/service/integrations/providers/test_salesforce_provider.py`

- [ ] **Step 1: Write the failing provider tests**

```python
from urllib.parse import parse_qs, urlparse

import pytest

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.salesforce_provider import SalesforceProvider


@pytest.mark.asyncio
async def test_salesforce_provider_builds_authorization_url():
    provider = SalesforceProvider(IntegrationType.SALESFORCE)
    provider._client_id = "salesforce-client-id"
    provider._client_secret = "salesforce-client-secret"
    provider.redirect_uri = "https://app.example.com/api/v1/users/integrations/copilot-oauth/callback"

    url = await provider.get_authorization_url("signed-state")

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "login.salesforce.com"
    assert params["client_id"] == ["salesforce-client-id"]
    assert params["state"] == ["signed-state"]


@pytest.mark.asyncio
async def test_salesforce_provider_normalizes_user_info(monkeypatch):
    provider = SalesforceProvider(IntegrationType.SALESFORCE)
    provider._client_id = "salesforce-client-id"
    provider._client_secret = "salesforce-client-secret"

    async def fake_identity_request(*args, **kwargs):
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "user_id": "005ABC",
                    "organization_id": "00DABC",
                    "username": "sales@acme.com",
                    "email": "sales@acme.com",
                }

        return FakeResponse()

    monkeypatch.setattr(provider, "_request_with_retry", fake_identity_request)

    payload = await provider.get_user_info("access-token")

    assert payload["email"] == "sales@acme.com"
    assert payload["salesforce_user_id"] == "005ABC"
    assert payload["salesforce_organization_id"] == "00DABC"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/service/integrations/providers/test_salesforce_provider.py -q`

Expected: FAIL because `salesforce_provider.py` does not exist yet

- [ ] **Step 3: Write the minimal provider implementation**

```python
from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import Any

import httpx

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import salesforce_integration_config
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


class SalesforceProvider(BaseIntegrationProvider):
    AUTHORIZATION_BASE_URL = "https://login.salesforce.com/services/oauth2/authorize"
    TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"
    REVOKE_URL = "https://login.salesforce.com/services/oauth2/revoke"
    USERINFO_URL = "https://login.salesforce.com/services/oauth2/userinfo"

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.redirect_uri = salesforce_integration_config.REDIRECT_URI
        self.timeout = salesforce_integration_config.SALESFORCE_HTTP_TIMEOUT_SEC
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def _ensure_credentials(self) -> None:
        if self._client_id and self._client_secret:
            return
        client_id, client_secret = await salesforce_integration_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError("Salesforce OAuth credentials not available")
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "salesforce"

    def _get_scopes(self) -> list[str]:
        return ["refresh_token", "api"]

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
                if method == "POST":
                    response = await client.post(url, data=data, headers=headers)
                else:
                    response = await client.get(url, headers=headers)
                if response.status_code != 429 or attempt >= _MAX_RETRIES:
                    return response
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait = min(float(retry_after), _MAX_BACKOFF_SEC)
                    except (ValueError, TypeError):
                        wait = min(_BASE_BACKOFF_SEC * (2**attempt), _MAX_BACKOFF_SEC)
                else:
                    wait = min(_BASE_BACKOFF_SEC * (2**attempt), _MAX_BACKOFF_SEC)
                await asyncio.sleep(wait + random.uniform(0, wait * 0.5))  # noqa: S311  # nosec B311
        raise RuntimeError("Exhausted retries without response")

    async def get_authorization_url(self, state: str) -> str:
        await self._ensure_credentials()
        params = {
            "response_type": "code",
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
        raise ValueError("Salesforce OAuth exchange returned an invalid payload")

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
        raise ValueError("Salesforce token refresh returned an invalid payload")

    async def revoke_token(self, token: str) -> None:
        response = await self._request_with_retry(
            "POST",
            self.REVOKE_URL,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        response = await self._request_with_retry(
            "GET",
            self.USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce userinfo returned an invalid payload")
        email = str(payload.get("email") or payload.get("username") or "").strip().lower()
        if not email:
            raise ValueError("Salesforce OAuth response did not include a connected account email")
        return {
            "email": email,
            "salesforce_user_id": payload.get("user_id"),
            "salesforce_organization_id": payload.get("organization_id"),
            "salesforce_instance_url": payload.get("urls", {}).get("custom_domain")
            if isinstance(payload.get("urls"), dict)
            else payload.get("custom_domain"),
            "salesforce_username": payload.get("username"),
        }
```

- [ ] **Step 4: Export the provider**

```python
from .salesforce_provider import SalesforceProvider
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/service/integrations/providers/test_salesforce_provider.py -q`

Expected: PASS

---

## Task 3: Wire Salesforce Into the Existing OAuth Stack

**Files:**
- Modify: `src/service/integrations/providers/provider_factory.py`
- Modify: `src/infra/config/integrations_metadata.py`
- Test: `tests/service/integrations/test_salesforce_oauth_registry.py`

- [ ] **Step 1: Write the failing registry test**

```python
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.integrations_metadata import INTEGRATION_METADATA
from src.service.integrations.providers.provider_factory import ProviderFactory
from src.service.integrations.providers.salesforce_provider import SalesforceProvider


def test_salesforce_provider_factory_and_metadata():
    provider = ProviderFactory.get_provider(IntegrationType.SALESFORCE)

    assert isinstance(provider, SalesforceProvider)
    metadata = INTEGRATION_METADATA[IntegrationType.SALESFORCE]
    assert metadata.availability_status.value == "beta"
    assert metadata.default_scope.value == "organization"
    assert metadata.allow_multiple_connections is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/service/integrations/test_salesforce_oauth_registry.py -q`

Expected: FAIL because Salesforce is still deprecated and not in `ProviderFactory`

- [ ] **Step 3: Register Salesforce and update metadata**

```python
from src.service.integrations.providers.salesforce_provider import SalesforceProvider


class ProviderFactory:
    _providers: dict[IntegrationType, type[BaseIntegrationProvider]] = {
        IntegrationType.GMAIL: GoogleProvider,
        IntegrationType.GOOGLE_CALENDAR: GoogleProvider,
        IntegrationType.OUTLOOK_EMAIL: MicrosoftProvider,
        IntegrationType.OUTLOOK_CALENDAR: MicrosoftProvider,
        IntegrationType.TEAMS: MicrosoftProvider,
        IntegrationType.SLACK: SlackProvider,
        IntegrationType.HUBSPOT: HubSpotProvider,
        IntegrationType.SALESFORCE: SalesforceProvider,
    }
```

```python
    IntegrationType.SALESFORCE: IntegrationMetadata(
        name="Salesforce",
        description="Connect Salesforce using Copilot's custom OAuth flow. CRM sync setup follows separately.",
        logo_url="https://api.nango.dev/images/template-logos/salesforce.svg",
        category="crm",
        category_name=IntegrationCategory.CRM_SALES,
        availability_status=AvailabilityStatus.BETA,
        oauth_scopes=["refresh_token", "api"],
        documentation_url="https://developer.salesforce.com/docs/platform/mobile-sdk/guide/oauth-scope-parameter-values.html",
        default_sync_frequency="manual",
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN, UserAppRole.MEMBER],
        allow_multiple_connections=False,
    ),
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/service/integrations/test_salesforce_oauth_registry.py -q`

Expected: PASS

---

## Task 4: Add Explicit Post-Connect Placeholder Behavior

**Files:**
- Modify: `src/service/integrations/initial_sync_service.py`
- Test: `tests/service/integrations/test_salesforce_initial_sync_placeholder.py`

- [ ] **Step 1: Write the failing placeholder test**

```python
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.initial_sync_service import InitialSyncService


def test_salesforce_post_connect_state_marks_sync_not_implemented():
    integration = KnowledgeIntegration(
        integration_type=IntegrationType.SALESFORCE,
        connection_metadata={"provider_key": "salesforce"},
    )

    InitialSyncService().apply_post_connect_state(integration)

    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"
    assert "Salesforce custom auth connected successfully" in integration.connection_metadata[
        "initial_sync_placeholder"
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/service/integrations/test_salesforce_initial_sync_placeholder.py -q`

Expected: FAIL because Salesforce is not part of the placeholder branch yet

- [ ] **Step 3: Extend the placeholder branch for Salesforce**

```python
    CRM_PLACEHOLDER_INTEGRATION_TYPES = {
        IntegrationType.HUBSPOT,
        IntegrationType.SALESFORCE,
    }

    CRM_PLACEHOLDER_MESSAGES = {
        IntegrationType.HUBSPOT: (
            "HubSpot custom auth connected successfully; CRM sync dispatch will be added later."
        ),
        IntegrationType.SALESFORCE: (
            "Salesforce custom auth connected successfully; CRM sync dispatch will be added later."
        ),
    }

    def apply_post_connect_state(self, integration: KnowledgeIntegration) -> None:
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)
        if integration_type not in self.CRM_PLACEHOLDER_INTEGRATION_TYPES:
            return

        metadata = dict(integration.connection_metadata or {})

        # TODO(crm-sync): Replace these placeholder markers with real CRM initial
        # dispatch once HubSpot and Salesforce sync pipelines are restored.
        metadata[self.INITIAL_SYNC_STATUS_METADATA_KEY] = "not_implemented"
        metadata[self.INITIAL_SYNC_PLACEHOLDER_METADATA_KEY] = self.CRM_PLACEHOLDER_MESSAGES[
            integration_type
        ]
        integration.connection_metadata = metadata
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/service/integrations/test_salesforce_initial_sync_placeholder.py -q`

Expected: PASS

---

## Task 5: Add Service-Level OAuth Flow Tests

**Files:**
- Create: `tests/service/integrations/test_salesforce_oauth_flow.py`

- [ ] **Step 1: Write the failing flow tests**

```python
async def test_callback_creates_salesforce_integration_with_placeholder_metadata(...):
    result = await salesforce_callback_service.handle_oauth_callback(code="abc", state=state_token)
    assert result["integration_type"] == "salesforce"
    assert integration.connection_metadata["provider_key"] == "salesforce"
    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"


async def test_callback_reactivates_existing_salesforce_integration(...):
    result = await salesforce_callback_service.handle_oauth_callback(code="abc", state=state_token)
    assert result["reactivated"] is True


async def test_callback_rejects_personal_email_accounts(...):
    with pytest.raises(ValueError, match="personal email"):
        await salesforce_callback_service.handle_oauth_callback(code="abc", state=state_token)


async def test_callback_rejects_duplicate_account_connected_by_other_user(...):
    with pytest.raises(ValueError, match="already connected"):
        await salesforce_callback_service.handle_oauth_callback(code="abc", state=state_token)


async def test_custom_token_refresh_deactivates_salesforce_after_three_failures(...):
    for _ in range(3):
        await token_refresh_service.refresh_single_integration_token(integration.integration_id)
    assert integration.is_active is False
    assert integration.connection_metadata["token_refresh_failures"] == 3


async def test_disconnect_revokes_salesforce_token_and_marks_inactive(...):
    await integration_service.revoke_custom_oauth_token(salesforce_integration)
    assert fake_salesforce_provider.revoked_token == "refresh-token"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/service/integrations/test_salesforce_oauth_flow.py -q`

Expected: FAIL because the provider and registry are not fully wired yet

- [ ] **Step 3: Add the minimal test harness and fixtures mirroring the HubSpot flow tests**

```python
class FakeSalesforceProvider:
    provider_key = "salesforce"

    def __init__(self) -> None:
        self.revoked_token: str | None = None

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, str]:
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    async def get_user_info(self, access_token: str) -> dict[str, str]:
        return {
            "email": "sales@acme.com",
            "salesforce_user_id": "005ABC",
            "salesforce_organization_id": "00DABC",
            "salesforce_instance_url": "https://acme.my.salesforce.com",
            "salesforce_username": "sales@acme.com",
        }

    async def refresh_token(self, refresh_token: str) -> dict[str, str]:
        return {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    async def revoke_token(self, token: str) -> None:
        self.revoked_token = token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/service/integrations/test_salesforce_oauth_flow.py -q`

Expected: PASS

---

## Task 6: Update Integration Documentation

**Files:**
- Modify: `docs/Technical Documentation/integrations/architecture-overview.md`
- Modify: `docs/Technical Documentation/integrations/oauth-and-token-management.md`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

- [ ] **Step 1: Write a failing documentation verification**

Run: `rg -n "Salesforce custom OAuth|Salesforce CRM \\(Deprecated\\)|initial_sync_status|deferred" "docs/Technical Documentation/integrations"`

Expected: output still says Salesforce is deprecated and does not document the new placeholder behavior

- [ ] **Step 2: Update architecture overview**

```md
HubSpot custom OAuth is supported through the first-party provider architecture. CRM sync for HubSpot remains deferred and is not part of the active sync worker pipeline yet. Salesforce custom OAuth is also supported through the first-party provider architecture, while Salesforce CRM sync remains deferred.
```

- [ ] **Step 3: Update OAuth documentation**

```md
### Salesforce

Salesforce uses the same first-party custom OAuth flow as Google, Microsoft, Slack, and HubSpot:

- authorization URL built by `SalesforceProvider`
- callback handled by `copilot_callback_service.py`
- tokens stored in `knowledge_integrations.credentials`
- scheduled refresh handled by `custom_token_refresh_service.py`
- disconnect handled through `IntegrationService.revoke_custom_oauth_token()`

Current limitation:

- successful connect stores `initial_sync_status = not_implemented` in `connection_metadata`
- no Salesforce CRM sync tasks are dispatched in this phase
```

- [ ] **Step 4: Update sync-by-provider documentation**

```md
## Salesforce CRM

- **Custom OAuth**: Supported via the current first-party provider architecture
- **Data sync**: Deferred
- **Current behavior after connect**: Integration is stored as active and marked with an initial-sync placeholder in `connection_metadata`
- **Current placeholder metadata**:
  - `initial_sync_status = not_implemented`
  - `initial_sync_placeholder = Salesforce custom auth connected successfully; CRM sync dispatch will be added later.`
```

- [ ] **Step 5: Verify the documentation**

Run: `rg -n "Salesforce custom OAuth|initial_sync_status|deferred" "docs/Technical Documentation/integrations"`

Expected: matches in all three docs

---

## Task 7: Final Verification

**Files:**
- Verify only

- [ ] **Step 1: Run the targeted Salesforce test suite**

Run: `uv run pytest tests/infra/config/test_salesforce_integration_config.py tests/service/integrations/providers/test_salesforce_provider.py tests/service/integrations/test_salesforce_oauth_registry.py tests/service/integrations/test_salesforce_initial_sync_placeholder.py tests/service/integrations/test_salesforce_oauth_flow.py -q`

Expected: PASS

- [ ] **Step 2: Run a focused compile sweep**

Run: `uv run python -m compileall src/service/integrations/providers src/service/integrations src/infra/config`

Expected: no syntax errors

- [ ] **Step 3: Run a narrow lint pass on touched files**

Run: `uv run ruff check src/service/integrations/providers/salesforce_provider.py src/service/integrations/providers/provider_factory.py src/service/integrations/initial_sync_service.py src/infra/config/configs.py src/infra/config/integrations_metadata.py tests/infra/config/test_salesforce_integration_config.py tests/service/integrations/providers/test_salesforce_provider.py tests/service/integrations/test_salesforce_oauth_registry.py tests/service/integrations/test_salesforce_initial_sync_placeholder.py tests/service/integrations/test_salesforce_oauth_flow.py`

Expected: all checks pass

- [ ] **Step 4: Record completion criteria**

Success means:

- Salesforce appears in available integrations
- authorize route can build a Salesforce auth URL
- callback stores or reactivates an organization-scoped `KnowledgeIntegration`
- connection metadata includes the initial-sync placeholder
- disconnect and scheduled refresh use `SalesforceProvider`
- docs describe Salesforce auth as supported and sync as deferred
