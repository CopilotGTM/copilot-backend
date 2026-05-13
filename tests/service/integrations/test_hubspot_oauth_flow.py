from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.auth.user_context import UserIDs
from src.service.integrations.copilot_callback_service import CopilotCallbackService
from src.service.integrations.custom_token_refresh_service import CustomTokenRefreshService
from src.service.integrations.integration_service import IntegrationService

_HUBSPOT_GRANTED_SCOPE = (
    "oauth crm.objects.companies.read crm.objects.contacts.read "
    "crm.objects.deals.read crm.objects.owners.read crm.schemas.deals.read"
)


class FakeResult:
    def __init__(self, *, scalar: object | None = None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def scalars(self) -> FakeResult:
        return self

    def first(self) -> object | None:
        return self._scalar


def _session_generator(sessions: list[AsyncMock]):
    async def _gen(_organisation_id):
        yield sessions.pop(0)

    return _gen


def _async_session_generator(sessions: list[AsyncMock]):
    async def _gen():
        yield sessions.pop(0)

    return _gen()


@pytest.fixture(autouse=True)
def _mock_callback_connection_guard() -> None:
    with (
        patch(
            "src.service.integrations.copilot_callback_service.acquire_master_async_session",
            side_effect=lambda: _async_session_generator([_make_session(FakeResult(scalar=None))]),
        ),
        patch.object(CopilotCallbackService, "_acquire_callback_processing_lock", AsyncMock()),
    ):
        yield


def _make_session(*results: FakeResult) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


def _make_user(*, user_id: UUID, organisation_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        organisation_id=organisation_id,
        email="seller@acme.com",
        name="Seller",
    )


def _make_integration(**overrides: object) -> KnowledgeIntegration:
    now = datetime.utcnow()
    defaults: dict[str, object] = {
        "organisation_id": uuid4(),
        "created_by_user_id": uuid4(),
        "integration_scope": IntegrationScope.ORGANIZATION,
        "integration_type": IntegrationType.HUBSPOT,
        "name": "HubSpot",
        "credentials": {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        },
        "connection_id": str(uuid4()),
        "is_active": True,
        "integration_email": "sales@acme.com",
        "connection_metadata": {},
        "scopes": ["old.scope"],
        "access_token_expires_at": now + timedelta(minutes=5),
        "refresh_token_expires_at": None,
    }
    defaults.update(overrides)
    return KnowledgeIntegration(**defaults)


@pytest.mark.asyncio
async def test_callback_creates_hubspot_integration_with_placeholder_metadata() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()
    duplicate_session = _make_session(FakeResult(scalar=None))
    reactivate_session = _make_session(FakeResult(scalar=None))
    create_session = _make_session()

    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.exchange_code.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
        "scope": _HUBSPOT_GRANTED_SCOPE,
    }
    provider.get_user_info.return_value = {
        "email": "sales@acme.com",
        "hubspot_portal_id": "123",
        "hubspot_portal_name": "Acme HubSpot",
        "hubspot_user_id": "987",
    }

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
                "jti": str(uuid4()),
            },
        ),
        patch.object(
            CopilotCallbackService,
            "_fetch_user_from_master",
            AsyncMock(return_value=_make_user(user_id=user_id, organisation_id=organisation_id)),
        ),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
        patch(
            "src.service.integrations.copilot_callback_service.acquire_org_async_session",
            side_effect=_session_generator([duplicate_session, reactivate_session, create_session]),
        ),
        patch.object(service, "_save_connection_mapping", AsyncMock()),
        patch.object(service, "_trigger_initial_sync"),
    ):
        result = await service.handle_oauth_callback(code="abc", state="signed-state")

    assert result["integration_type"] == "hubspot"
    created = create_session.add.call_args[0][0]
    assert created.integration_scope == IntegrationScope.ORGANIZATION
    assert created.connection_metadata["provider_key"] == "hubspot"
    assert created.connection_metadata["initial_sync_status"] == "queued"
    assert created.connection_metadata["hubspot_portal_id"] == "123"
    assert created.connection_metadata["hubspot_portal_name"] == "Acme HubSpot"
    assert created.connection_metadata["hubspot_user_id"] == "987"


@pytest.mark.asyncio
async def test_callback_returns_existing_connection_before_redeeming_duplicate_code() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()
    connection_id = uuid4()
    integration_id = uuid4()
    mapping = SimpleNamespace(
        connection_id=connection_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider_config_key="custom-hubspot",
    )
    integration = _make_integration(
        organisation_id=organisation_id,
        created_by_user_id=user_id,
        integration_id=integration_id,
        connection_id=str(connection_id),
    )
    master_session = _make_session(FakeResult(scalar=mapping))
    org_session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "hubspot"

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
                "jti": str(connection_id),
            },
        ),
        patch.object(
            CopilotCallbackService,
            "_fetch_user_from_master",
            AsyncMock(return_value=_make_user(user_id=user_id, organisation_id=organisation_id)),
        ),
        patch.object(CopilotCallbackService, "_acquire_callback_processing_lock", AsyncMock()),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
            return_value=provider,
        ) as get_provider,
        patch(
            "src.service.integrations.copilot_callback_service.acquire_master_async_session",
            side_effect=lambda: _async_session_generator([master_session]),
        ),
        patch(
            "src.service.integrations.copilot_callback_service.acquire_org_async_session",
            side_effect=_session_generator([org_session]),
        ),
        patch.object(service, "_trigger_initial_sync"),
    ):
        result = await service.handle_oauth_callback(code="duplicate-code", state="signed-state")

    assert result["integration_id"] == str(integration_id)
    assert result["connection_id"] == str(connection_id)
    get_provider.assert_not_called()
    provider.exchange_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_validates_hubspot_scopes_from_access_token_metadata() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()
    duplicate_session = _make_session(FakeResult(scalar=None))
    reactivate_session = _make_session(FakeResult(scalar=None))
    create_session = _make_session()

    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.exchange_code.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
    }
    provider.get_user_info.return_value = {
        "email": "sales@acme.com",
        "hubspot_portal_id": "123",
        "scopes": _HUBSPOT_GRANTED_SCOPE.split(),
    }

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
                "jti": str(uuid4()),
            },
        ),
        patch.object(
            CopilotCallbackService,
            "_fetch_user_from_master",
            AsyncMock(return_value=_make_user(user_id=user_id, organisation_id=organisation_id)),
        ),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
        patch(
            "src.service.integrations.copilot_callback_service.acquire_org_async_session",
            side_effect=_session_generator([duplicate_session, reactivate_session, create_session]),
        ),
        patch.object(service, "_save_connection_mapping", AsyncMock()),
        patch.object(service, "_trigger_initial_sync"),
    ):
        result = await service.handle_oauth_callback(code="abc", state="signed-state")

    assert result["integration_type"] == "hubspot"
    created = create_session.add.call_args[0][0]
    assert created.scopes == _HUBSPOT_GRANTED_SCOPE.split()


@pytest.mark.asyncio
async def test_callback_reactivates_existing_hubspot_integration() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()
    existing = _make_integration(
        organisation_id=organisation_id,
        created_by_user_id=user_id,
        is_active=False,
        integration_email="sales@acme.com",
        connection_metadata={},
    )
    duplicate_session = _make_session(FakeResult(scalar=None))
    reactivate_session = _make_session(FakeResult(scalar=existing))

    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.exchange_code.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
        "scope": _HUBSPOT_GRANTED_SCOPE,
    }
    provider.get_user_info.return_value = {
        "email": "sales@acme.com",
        "hubspot_portal_id": "123",
        "hubspot_user_id": "987",
    }

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
                "jti": str(uuid4()),
            },
        ),
        patch.object(
            CopilotCallbackService,
            "_fetch_user_from_master",
            AsyncMock(return_value=_make_user(user_id=user_id, organisation_id=organisation_id)),
        ),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
        patch(
            "src.service.integrations.copilot_callback_service.acquire_org_async_session",
            side_effect=_session_generator([duplicate_session, reactivate_session]),
        ),
        patch.object(service, "_save_connection_mapping", AsyncMock()),
        patch.object(service, "_trigger_initial_sync"),
    ):
        result = await service.handle_oauth_callback(code="abc", state="signed-state")

    assert result["reactivated"] is True
    assert existing.is_active is True
    assert existing.connection_metadata["initial_sync_status"] == "queued"
    assert existing.connection_metadata["hubspot_portal_id"] == "123"
    assert existing.connection_metadata["hubspot_user_id"] == "987"


@pytest.mark.asyncio
async def test_callback_allows_personal_email_accounts_temporarily() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()
    duplicate_session = _make_session(FakeResult(scalar=None))
    reactivate_session = _make_session(FakeResult(scalar=None))
    create_session = _make_session()

    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.exchange_code.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
        "scope": _HUBSPOT_GRANTED_SCOPE,
    }
    provider.get_user_info.return_value = {"email": "seller@gmail.com"}

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
                "jti": str(uuid4()),
            },
        ),
        patch.object(
            CopilotCallbackService,
            "_fetch_user_from_master",
            AsyncMock(return_value=_make_user(user_id=user_id, organisation_id=organisation_id)),
        ),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
        patch(
            "src.service.integrations.copilot_callback_service.acquire_org_async_session",
            side_effect=_session_generator([duplicate_session, reactivate_session, create_session]),
        ),
        patch.object(service, "_save_connection_mapping", AsyncMock()),
        patch.object(service, "_trigger_initial_sync"),
    ):
        result = await service.handle_oauth_callback(code="abc", state="signed-state")

    assert result["integration_email"] == "seller@gmail.com"
    created = create_session.add.call_args[0][0]
    assert created.integration_email == "seller@gmail.com"


@pytest.mark.asyncio
async def test_callback_rejects_duplicate_account_connected_by_other_user() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()
    duplicate_owner_id = uuid4()
    duplicate_integration = _make_integration(
        organisation_id=organisation_id,
        created_by_user_id=duplicate_owner_id,
        integration_email="sales@acme.com",
    )
    duplicate_session = _make_session(FakeResult(scalar=duplicate_integration))

    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.exchange_code.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
        "scope": _HUBSPOT_GRANTED_SCOPE,
    }
    provider.get_user_info.return_value = {"email": "sales@acme.com"}

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
                "jti": str(uuid4()),
            },
        ),
        patch.object(
            CopilotCallbackService,
            "_fetch_user_from_master",
            AsyncMock(return_value=_make_user(user_id=user_id, organisation_id=organisation_id)),
        ),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
        patch(
            "src.service.integrations.copilot_callback_service.acquire_org_async_session",
            side_effect=_session_generator([duplicate_session]),
        ),
        pytest.raises(
            ValueError,
            match="already connected by another user",
        ),
    ):
        await service.handle_oauth_callback(code="abc", state="signed-state")


@pytest.mark.asyncio
async def test_callback_rejects_state_without_jti_claim() -> None:
    service = CopilotCallbackService()
    user_id = uuid4()
    organisation_id = uuid4()

    with (
        patch(
            "src.service.integrations.copilot_callback_service.copilot_oauth_service.decode_oauth_state",
            return_value={
                "sub": str(user_id),
                "org": str(organisation_id),
                "type": IntegrationType.HUBSPOT.value,
            },
        ),
        patch(
            "src.service.integrations.copilot_callback_service.ProviderFactory.get_provider",
        ) as provider_factory,
        pytest.raises(ValueError, match="State payload is missing jti claim"),
    ):
        await service.handle_oauth_callback(code="abc", state="signed-state")

    provider_factory.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_revokes_hubspot_refresh_token() -> None:
    integration = _make_integration(
        credentials={
            "access_token": "old-access",
            "refresh_token": "refresh-token",
        }
    )
    service = IntegrationService(
        db_session=AsyncMock(),
        user_ids=UserIDs(
            user_id=uuid4(),
            organisation_id=integration.organisation_id,
            user_name="Test User",
            user_email="test@example.com",
        ),
    )

    provider = AsyncMock()
    provider.revoke_token = AsyncMock()

    with patch(
        "src.service.integrations.providers.provider_factory.ProviderFactory.get_provider",
        return_value=provider,
    ):
        result = await service.revoke_custom_oauth_token(integration)

    assert result is True
    provider.revoke_token.assert_awaited_once_with("refresh-token")


@pytest.mark.asyncio
async def test_custom_token_refresh_uses_hubspot_provider() -> None:
    organisation_id = uuid4()
    integration = _make_integration(organisation_id=organisation_id)
    session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.refresh_token.return_value = {
        "access_token": "new-access",
        "expires_in": 3600,
        "scope": _HUBSPOT_GRANTED_SCOPE,
    }

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result["success"] is True
    assert integration.credentials["access_token"] == "new-access"
    assert integration.credentials["refresh_token"] == "old-refresh"
    provider.refresh_token.assert_awaited_once_with("old-refresh")


@pytest.mark.asyncio
async def test_custom_token_refresh_deactivates_hubspot_after_three_failures() -> None:
    organisation_id = uuid4()
    integration = _make_integration(
        organisation_id=organisation_id,
        connection_metadata={
            "initial_sync_status": "not_implemented",
            "initial_sync_placeholder": (
                "HubSpot custom auth connected successfully; CRM sync dispatch will be added later."
            ),
            "token_refresh_failures": 2,
        },
    )
    session = _make_session(FakeResult(scalar=integration))
    provider = AsyncMock()
    provider.provider_key = "hubspot"
    provider.refresh_token.side_effect = ValueError("refresh failed")

    service = CustomTokenRefreshService(organisation_id=organisation_id)

    with (
        patch(
            "src.service.integrations.custom_token_refresh_service.acquire_org_async_session",
            side_effect=_session_generator([session]),
        ),
        patch(
            "src.service.integrations.custom_token_refresh_service.ProviderFactory.get_provider",
            return_value=provider,
        ),
    ):
        result = await service.refresh_single_token_async(integration.integration_id)

    assert result["success"] is False
    assert result["failure_count"] == 3
    assert result["deactivated"] is True
    assert integration.is_active is False
    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"
    assert integration.connection_metadata["token_refresh_failures"] == 3
    session.commit.assert_awaited_once()
