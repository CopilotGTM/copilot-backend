"""
Fixtures and fake services for integrations API tests.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.routers.integrations.copilot_oauth_routes import (
    get_copilot_callback_service,
    get_copilot_oauth_service,
)
from src.api.routers.integrations.deal_integration_routes import (
    get_integration_service as get_deal_integration_service,
)
from src.api.routers.integrations.integration_routes import (
    get_integration_service as get_list_integration_service,
)
from src.domain.models.organisation_tables.sync import IntegrationScope, IntegrationType
from src.infra.auth.user_auth import user_auth
from src.infra.config.integrations_metadata import get_all_integration_metadata


def _user_owned_integration_types() -> set[str]:
    return {
        integration_type.value
        for integration_type, metadata in get_all_integration_metadata().items()
        if metadata.default_scope == IntegrationScope.USER
    }


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
        integration_email: str | None = "test@example.com",
        integration_scope: IntegrationScope = IntegrationScope.ORGANIZATION,
        sync_frequency: str = "daily",
        auto_sync_enabled: bool = True,
        last_synced_at: datetime | None = None,
        last_sync_status: str | None = None,
        last_sync_error: str | None = None,
        access_token_expires_at: datetime | None = None,
        scopes: list[str] | None = None,
        requires_reauth: bool = False,
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
        self.integration_email = integration_email
        self.integration_scope = integration_scope
        self.sync_frequency = sync_frequency
        self.auto_sync_enabled = auto_sync_enabled
        self.last_synced_at = last_synced_at
        self.last_sync_status = last_sync_status
        self.last_sync_error = last_sync_error
        self.access_token_expires_at = access_token_expires_at
        self.scopes = scopes
        self.requires_reauth = requires_reauth
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
        integrations = [i for i in self.integrations.values() if i.is_active]
        if user_id is None:
            return integrations
        user_owned_types = _user_owned_integration_types()
        return [
            integration
            for integration in integrations
            if (
                integration.integration_type not in user_owned_types
                and integration.integration_scope == IntegrationScope.ORGANIZATION
            )
            or integration.created_by_user_id == user_id
        ]

    async def list_available_integrations_for_user(
        self, user_id: UUID | None = None
    ) -> list[FakeKnowledgeIntegration]:
        """List active integrations plus inactive ones that explicitly require re-auth."""
        integrations = [i for i in self.integrations.values() if i.is_active or i.requires_reauth]
        if user_id is None:
            return integrations
        user_owned_types = _user_owned_integration_types()
        return [
            integration
            for integration in integrations
            if (
                integration.integration_type not in user_owned_types
                and integration.integration_scope == IntegrationScope.ORGANIZATION
            )
            or integration.created_by_user_id == user_id
        ]

    async def delete_integration_from_nango(
        self, connection_id: str, integration_type: str
    ) -> None:
        """Delete integration from Nango."""
        pass

    async def revoke_custom_oauth_token(self, integration: Any) -> bool:
        """Revoke custom OAuth token (fake)."""
        return True

    async def get_deal_linked_channels(self, deal_id: UUID) -> list[dict[str, Any]]:
        """Get channels linked to a deal."""
        return self.deal_channels.get(str(deal_id), [])

    async def link_channel_to_deal(self, deal_id: UUID, channel_data: Any) -> dict[str, Any]:
        """Link a channel to a deal."""
        channel_id = uuid4()
        integration = None
        if channel_data.integration_id is not None:
            integration = self.integrations.get(str(channel_data.integration_id))
        elif self.integrations:
            integration = next(iter(self.integrations.values()))
        integration_type = integration.integration_type if integration else "teams"
        integration_name = integration.name if integration else "Test Integration"
        resolved_integration_id = integration.integration_id if integration else uuid4()
        result = {
            "channel_id": channel_id,
            "deal_id": deal_id,
            "external_channel_id": channel_data.id,
            "name": channel_data.name,
            "type": channel_data.type,
            "integration_id": resolved_integration_id,
        }

        deal_key = str(deal_id)
        if deal_key not in self.deal_channels:
            self.deal_channels[deal_key] = []
        self.deal_channels[deal_key].append(
            {
                **result,
                "integration_type": integration_type,
                "integration_name": integration_name,
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

    async def list_user_channels_by_type(
        self, integration_type: IntegrationType, deal_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        """List channels visible through the user's active integrations of a given type."""
        matching_integrations = [
            integration
            for integration in self.integrations.values()
            if integration.is_active and integration.integration_type == integration_type.value
        ]
        if not matching_integrations:
            raise ValueError(
                f"No active {integration_type.value} integration found. "
                "Please connect your account first."
            )

        linked_ids: set[str] = set()
        if deal_id:
            linked_ids = {
                ch.get("external_channel_id") for ch in self.deal_channels.get(str(deal_id), [])
            }

        channels: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for integration in matching_integrations:
            for channel in self.channels.get(str(integration.integration_id), []):
                channel_id = channel.get("id")
                if not channel_id or channel_id in linked_ids or channel_id in seen_ids:
                    continue
                channel_type = str(channel.get("type") or "public")
                channels.append(
                    {
                        "id": channel_id,
                        "name": channel.get("name"),
                        "integration_id": integration.integration_id,
                        "type": channel_type,
                        "type_label": channel.get("type_label")
                        or channel_type.replace("_", " ").title(),
                        "parent_id": channel.get("parent_id"),
                        "parent_name": channel.get("parent_name"),
                        "description": channel.get("description"),
                        "member_count": channel.get("member_count", 0),
                        "members": channel.get("members", []),
                        "last_activity_at": channel.get("last_activity_at"),
                    }
                )
                seen_ids.add(channel_id)

        return channels

    # Helper methods for test setup
    def add_integration(self, integration: FakeKnowledgeIntegration):
        """Add an integration for testing."""
        self.integrations[str(integration.integration_id)] = integration

    def add_channels(self, integration_id: UUID, channels: list[dict]):
        """Add channels for an integration."""
        self.channels[str(integration_id)] = channels


class FakeCopilotOAuthService:
    """Fake CopilotOAuthService for testing."""

    def __init__(self):
        self.should_raise_unavailable = False
        self.should_raise_value_error = False

    async def initiate_oauth_flow(
        self,
        user_ids: UserIDs,
        integration_type: IntegrationType,
    ) -> str:
        """Return a fake auth URL or raise configured errors."""
        if self.should_raise_unavailable:
            from src.service.integrations.copilot_oauth_service import (
                CopilotOAuthIntegrationUnavailableError,
            )

            raise CopilotOAuthIntegrationUnavailableError(status="coming_soon")

        if self.should_raise_value_error:
            raise ValueError("No custom integration provider found for type: unsupported")

        return f"https://accounts.example.com/oauth/{integration_type.value}"


class FakeCopilotCallbackService:
    """Fake CopilotCallbackService for testing."""

    def __init__(self):
        self.should_raise_value_error = False
        self.value_error_message = "Invalid or expired state parameter"

    async def handle_oauth_callback(self, code: str, state: str) -> dict[str, str]:
        """Return a fake callback payload."""
        if self.should_raise_value_error:
            raise ValueError(self.value_error_message)
        return {
            "integration_id": "test-integration",
            "connection_id": "test-connection",
            "integration_type": "gmail",
        }


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
def fake_copilot_oauth_service():
    """Create a FakeCopilotOAuthService instance."""
    return FakeCopilotOAuthService()


@pytest.fixture(scope="function")
def fake_copilot_callback_service():
    """Create a FakeCopilotCallbackService instance."""
    return FakeCopilotCallbackService()


@pytest.fixture(scope="function")
def integrations_dependency_overrides(
    client,
    integration_test_ids,
    fake_integration_service,
    fake_copilot_oauth_service,
    fake_copilot_callback_service,
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

    async def _override_copilot_oauth_service():
        yield fake_copilot_oauth_service

    async def _override_copilot_callback_service():
        yield fake_copilot_callback_service

    from src.infra.utils.app_state import fastapp as app

    # Override all the dependencies
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[user_auth] = _override_user_auth
    app.dependency_overrides[get_list_integration_service] = _override_integration_service
    app.dependency_overrides[get_deal_integration_service] = _override_integration_service
    app.dependency_overrides[get_copilot_oauth_service] = _override_copilot_oauth_service
    app.dependency_overrides[get_copilot_callback_service] = _override_copilot_callback_service

    yield {
        "user_ids": user_ids,
        "integration_service": fake_integration_service,
        "copilot_oauth_service": fake_copilot_oauth_service,
        "copilot_callback_service": fake_copilot_callback_service,
    }

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides = {}
