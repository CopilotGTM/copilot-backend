"""
Fixtures and fake services for integrations API tests.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.responses import JSONResponse
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.routers.integrations.deal_integration_routes import (
    get_integration_service as get_deal_integration_service,
)
from src.api.routers.integrations.integration_routes import (
    get_integration_service as get_list_integration_service,
)
from src.api.routers.integrations.oauth_routes import (
    get_integration_service as get_oauth_integration_service,
)
from src.api.routers.integrations.oauth_routes import (
    get_oauth_service,
    get_webhook_service,
)
from src.api.schemas.integrations import OAuthInitiateResponse
from src.domain.models.organisation_tables.sync import IntegrationScope, IntegrationType
from src.infra.auth.user_auth import user_auth


class FakeKnowledgeIntegration:
    """Fake KnowledgeIntegration model for testing."""

    def __init__(
        self,
        integration_id: UUID | None = None,
        organisation_id: UUID | None = None,
        integration_type: str = "teams",
        name: str = "Test Integration",
        connection_id: str | None = "conn_123",
        is_active: bool = True,
        created_by_user_id: UUID | None = None,
        created_by_admin: bool = False,
        integration_email: str | None = "test@example.com",
        integration_scope: IntegrationScope = IntegrationScope.ORGANIZATION,
        sync_frequency: str = "daily",
        auto_sync_enabled: bool = True,
        last_synced_at: datetime | None = None,
        last_sync_status: str | None = None,
        last_sync_error: str | None = None,
        access_token_expires_at: datetime | None = None,
        connection_metadata: dict | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        self.integration_id = integration_id or uuid4()
        self.organisation_id = organisation_id or uuid4()
        self.integration_type = integration_type
        self.name = name
        self.connection_id = connection_id
        self.is_active = is_active
        self.created_by_user_id = created_by_user_id or uuid4()
        self.created_by_admin = created_by_admin
        self.integration_email = integration_email
        self.integration_scope = integration_scope
        self.sync_frequency = sync_frequency
        self.auto_sync_enabled = auto_sync_enabled
        self.last_synced_at = last_synced_at
        self.last_sync_status = last_sync_status
        self.last_sync_error = last_sync_error
        self.access_token_expires_at = access_token_expires_at
        self.connection_metadata = connection_metadata or {}
        self.created_at = created_at or datetime(2024, 1, 1, 0, 0, 0)
        self.updated_at = updated_at or datetime(2024, 1, 1, 0, 0, 0)


class FakeDB:
    """Fake database session for testing."""

    def __init__(self):
        self._committed = False

    async def commit(self):
        self._committed = True

    async def rollback(self):
        pass

    def add(self, obj):
        pass


class FakeIntegrationService:
    """Fake IntegrationService for testing."""

    def __init__(self, user_ids: UserIDs):
        self.user_ids = user_ids
        self.db = FakeDB()
        self.integrations: dict[str, FakeKnowledgeIntegration] = {}
        self.channels: dict[str, list[dict]] = {}  # integration_id -> channels
        self.deal_channels: dict[str, list[dict]] = {}  # deal_id -> linked channels

    async def close(self):
        pass

    async def get_integration(self, integration_id: UUID) -> FakeKnowledgeIntegration | None:
        """Get integration by ID."""
        return self.integrations.get(str(integration_id))

    async def list_all_active_integrations(
        self, user_id: UUID | None = None
    ) -> list[FakeKnowledgeIntegration]:
        """List all active integrations."""
        return [i for i in self.integrations.values() if i.is_active]

    async def delete_integration_from_nango(
        self, connection_id: str, integration_type: str
    ) -> None:
        """Delete integration from Nango."""
        # Simulate successful deletion
        pass

    async def get_deal_linked_channels(self, deal_id: UUID) -> list[dict[str, Any]]:
        """Get channels linked to a deal."""
        return self.deal_channels.get(str(deal_id), [])

    async def link_channel_to_deal(self, deal_id: UUID, channel_data: Any) -> dict[str, Any]:
        """Link a channel to a deal."""
        channel_id = uuid4()
        result = {
            "channel_id": channel_id,
            "deal_id": deal_id,
            "external_channel_id": channel_data.id,
            "name": channel_data.name,
            "type": channel_data.type,
            "integration_id": channel_data.integration_id,
        }

        deal_key = str(deal_id)
        if deal_key not in self.deal_channels:
            self.deal_channels[deal_key] = []
        self.deal_channels[deal_key].append(
            {
                **result,
                "integration_type": "teams",
                "integration_name": "Test Integration",
                "parent_name": channel_data.parent_name,
            }
        )

        return result

    async def remove_channel_from_deal(self, deal_id: UUID, channel_id: UUID) -> bool:
        """Remove a channel from a deal."""
        deal_key = str(deal_id)
        if deal_key not in self.deal_channels:
            return False

        original_len = len(self.deal_channels[deal_key])
        self.deal_channels[deal_key] = [
            ch
            for ch in self.deal_channels[deal_key]
            if str(ch.get("channel_id")) != str(channel_id)
        ]
        return len(self.deal_channels[deal_key]) < original_len

    async def list_integration_channels(
        self, integration_id: UUID, deal_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        """List channels for an integration."""
        int_key = str(integration_id)
        if int_key not in self.integrations:
            raise ValueError("Integration not found")

        channels = self.channels.get(int_key, [])

        # Filter out already linked channels if deal_id provided
        if deal_id:
            linked_ids = {
                ch.get("external_channel_id") for ch in self.deal_channels.get(str(deal_id), [])
            }
            channels = [ch for ch in channels if ch.get("id") not in linked_ids]

        return channels

    # Helper methods for test setup
    def add_integration(self, integration: FakeKnowledgeIntegration):
        """Add an integration for testing."""
        self.integrations[str(integration.integration_id)] = integration

    def add_channels(self, integration_id: UUID, channels: list[dict]):
        """Add channels for an integration."""
        self.channels[str(integration_id)] = channels


class FakeOAuthIntegrationService:
    """Fake OAuthIntegrationService for testing."""

    def __init__(self):
        self.should_raise_unavailable = False
        self.should_raise_nango_error = False
        self.nango_error_status = None
        self.nango_error_payload = None
        self._unavailable_error_class = None
        self._nango_error_class = None

    def _get_exception_classes(self):
        """Lazy load exception classes to ensure proper import."""
        if self._unavailable_error_class is None:
            from src.service.integrations.nango_client import NangoConnectionError
            from src.service.integrations.oauth_service import IntegrationUnavailableError

            self._unavailable_error_class = IntegrationUnavailableError
            self._nango_error_class = NangoConnectionError

    async def create_connect_session(
        self,
        user_ids: UserIDs,
        user: Any,
        integration_type: IntegrationType,
    ) -> OAuthInitiateResponse:
        """Create a Nango connect session."""
        self._get_exception_classes()

        if self.should_raise_unavailable:
            raise self._unavailable_error_class(status="coming_soon")

        if self.should_raise_nango_error:
            raise self._nango_error_class(
                message="Nango error",
                status_code=self.nango_error_status,
                payload=self.nango_error_payload,
            )

        return OAuthInitiateResponse(
            session_token="fake_session_token_123",
            integration_type=integration_type,
            expires_at="2024-12-31T23:59:59Z",
        )

    @staticmethod
    def extract_nango_error_message(payload: dict | None) -> str | None:
        """Extract error message from Nango payload."""
        if payload and isinstance(payload, dict):
            return payload.get("error", {}).get("message")
        return None


class FakeNangoWebhookService:
    """Fake NangoWebhookService for testing."""

    def __init__(self):
        self.valid_signature = True
        self.auth_webhook_response = JSONResponse(
            status_code=200,
            content={"status": "success", "message": "Integration created"},
        )

    def _is_valid_signature(self, signature: str | None, payload: dict) -> bool:
        """Validate webhook signature."""
        return self.valid_signature and signature is not None

    async def _handle_authorization_webhook(self, payload: dict) -> JSONResponse:
        """Handle authorization webhook."""
        return self.auth_webhook_response


class FakeUser:
    """Fake User model for testing."""

    def __init__(self, user_id: UUID, organisation_id: UUID):
        self.user_id = user_id
        self.organisation_id = organisation_id
        self.email = "test@example.com"
        self.name = "Test User"


@pytest.fixture(scope="function")
def integration_test_ids():
    """Provide consistent test IDs."""
    return {
        "user_id": uuid4(),
        "organisation_id": uuid4(),
    }


@pytest.fixture(scope="function")
def fake_integration_service(integration_test_ids):
    """Create a FakeIntegrationService instance."""
    user_ids = UserIDs(
        user_id=integration_test_ids["user_id"],
        organisation_id=integration_test_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    return FakeIntegrationService(user_ids)


@pytest.fixture(scope="function")
def fake_oauth_service():
    """Create a FakeOAuthIntegrationService instance."""
    return FakeOAuthIntegrationService()


@pytest.fixture(scope="function")
def fake_webhook_service():
    """Create a FakeNangoWebhookService instance."""
    return FakeNangoWebhookService()


@pytest.fixture(scope="function")
def integrations_dependency_overrides(
    client,
    integration_test_ids,
    fake_integration_service,
    fake_oauth_service,
    fake_webhook_service,
):
    """
    Fixture to override dependencies for integrations tests.
    """
    user_ids = UserIDs(
        user_id=integration_test_ids["user_id"],
        organisation_id=integration_test_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )

    fake_user = FakeUser(
        user_id=integration_test_ids["user_id"],
        organisation_id=integration_test_ids["organisation_id"],
    )

    def _override_user():
        return user_ids

    def _override_user_auth():
        return fake_user

    async def _override_integration_service():
        try:
            yield fake_integration_service
        finally:
            await fake_integration_service.close()

    async def _override_oauth_service():
        yield fake_oauth_service

    async def _override_webhook_service():
        yield fake_webhook_service

    from src.infra.utils.app_state import fastapp as app

    # Override all the dependencies
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[user_auth] = _override_user_auth
    app.dependency_overrides[get_oauth_integration_service] = _override_integration_service
    app.dependency_overrides[get_list_integration_service] = _override_integration_service
    app.dependency_overrides[get_deal_integration_service] = _override_integration_service
    app.dependency_overrides[get_oauth_service] = _override_oauth_service
    app.dependency_overrides[get_webhook_service] = _override_webhook_service

    yield {
        "user_ids": user_ids,
        "integration_service": fake_integration_service,
        "oauth_service": fake_oauth_service,
        "webhook_service": fake_webhook_service,
    }

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides = {}
