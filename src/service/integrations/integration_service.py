"""
Core integration service for managing third-party OAuth integrations.

This service provides CRUD operations for integrations and coordinates
between the database and Nango client.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.domain.models.master_tables import OrganisationIntegrationDetails
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.nango_provider_mapping import (
    to_nango_connect_integration_key,
)

from .nango_client import NangoClient, NangoConnectionError

logger = logging.getLogger(__name__)


class IntegrationService:
    """
    Service for managing knowledge base integrations.

    Handles:
    - Creating and configuring integrations
    - Updating integration settings
    - Retrieving integration details
    - Soft-deleting integrations
    - Managing integration lifecycle
    """

    def __init__(
        self,
        db_session: AsyncSession,
        organisation_id: UUID,
        nango_client: NangoClient | None = None,
    ):
        """
        Initialize the integration service.

        Args:
            db_session: Async SQLAlchemy database session
            organisation_id: Organization UUID for scoping queries
            nango_client: Nango client instance (creates new one if not provided)
        """
        self.db = db_session
        self.organisation_id = organisation_id
        self.nango = nango_client or NangoClient()

    @staticmethod
    def _provider_from_type(type_or_str: Any) -> str:
        """Resolve Nango provider_config_key for given integration.

        Uses the Connect integration key where applicable so downstream
        Nango connection APIs address the correct configured integration.
        """
        try:
            if isinstance(type_or_str, IntegrationType):
                return to_nango_connect_integration_key(type_or_str)
            if isinstance(type_or_str, str):
                try:
                    enum_value = IntegrationType(type_or_str)
                    return to_nango_connect_integration_key(enum_value)
                except ValueError:
                    # Already a provider key; return as-is
                    return type_or_str
            # Fallback to string representation
            return str(type_or_str)
        except Exception:
            # Very defensive: never let provider resolution explode
            return str(type_or_str)

    async def create_integration(
        self,
        integration_type: IntegrationType,
        name: str,
        connection_id: str,
        created_by_user_id: UUID,
        created_by_admin: bool,
        integration_scope: IntegrationScope,
        integration_email: str | None = None,
        connection_metadata: dict[str, Any] | None = None,
        scopes: list[str] | None = None,
        sync_frequency: str = "daily",
        auto_sync_enabled: bool = True,
    ) -> KnowledgeIntegration:
        """
        Create a new integration after OAuth is complete.

        Args:
            integration_type: Type of integration (HUBSPOT, SLACK, etc.)
            name: User-defined name for the integration
            connection_id: Nango connection identifier
            created_by_user_id: User who created this integration
            created_by_admin: Whether this integration was created by an admin
            integration_scope: Access scope (ORGANIZATION or USER)
            integration_email: Email/username of the connected account (e.g., 'john@company.com')
            connection_metadata: Additional metadata from Nango
            scopes: OAuth scopes granted
            sync_frequency: How often to sync ('hourly', 'daily', 'weekly', 'manual')
            auto_sync_enabled: Whether automatic syncing is enabled

        Returns:
            Created KnowledgeIntegration instance

        Raises:
            ValueError: If integration creation fails
        """
        try:
            # Get token expiry from Nango connection
            provider = self._provider_from_type(integration_type)
            nango_connection = await self.nango.get_connection(
                provider=provider,
                connection_id=connection_id,
            )

            credentials = nango_connection.get("credentials", {})
            expires_in = credentials.get("expires_in")

            integration = KnowledgeIntegration(
                organisation_id=self.organisation_id,
                integration_type=integration_type,
                name=name,
                connection_id=connection_id,
                created_by_user_id=created_by_user_id,
                created_by_admin=created_by_admin,
                integration_email=integration_email,
                integration_scope=integration_scope,
                credentials={},  # Nango stores credentials, we just reference them
                is_active=True,
                connection_metadata=connection_metadata or {},
                scopes=scopes,
                access_token_expires_at=self.nango.parse_token_expiry(expires_in),
                sync_frequency=sync_frequency,
                auto_sync_enabled=auto_sync_enabled,
                last_sync_status=None,
            )

            self.db.add(integration)
            await self.db.commit()
            await self.db.refresh(integration)

            await self._record_connection_mapping(
                connection_id=connection_id,
                provider_key=provider,
                integration=integration,
            )

            logger.info(
                f"Created integration: {integration.integration_id} "
                f"({provider}) for org {self.organisation_id}, "
                f"scope={integration_scope}, created_by={created_by_user_id}"
            )

            return integration

        except IntegrityError as e:
            await self.db.rollback()
            logger.error(f"Database integrity error creating integration: {str(e)}")
            raise ValueError(f"Failed to create integration: {str(e)}") from e
        except Exception as e:
            await self.db.rollback()
            logger.error(f"Unexpected error creating integration: {str(e)}")
            raise

    async def _record_connection_mapping(
        self,
        *,
        connection_id: str,
        provider_key: str,
        integration: KnowledgeIntegration,
    ) -> None:
        try:
            connection_uuid = UUID(connection_id)
        except ValueError:
            logger.warning("Skipping master mapping for invalid connection_id %s", connection_id)
            return

        async for master_session in acquire_master_async_session():
            stmt = select(OrganisationIntegrationDetails).where(
                OrganisationIntegrationDetails.connection_id == connection_uuid
            )
            result = await master_session.execute(stmt)
            mapping = result.scalar_one_or_none()

            if mapping:
                updated = False
                if mapping.organisation_id != integration.organisation_id:
                    mapping.organisation_id = integration.organisation_id
                    updated = True
                if mapping.integration_id != integration.integration_id:
                    mapping.integration_id = integration.integration_id
                    updated = True
                if mapping.provider_config_key != provider_key:
                    mapping.provider_config_key = provider_key
                    updated = True
                if updated:
                    master_session.add(mapping)
                    await master_session.commit()
                return

            master_session.add(
                OrganisationIntegrationDetails(
                    connection_id=connection_uuid,
                    organisation_id=integration.organisation_id,
                    integration_id=integration.integration_id,
                    provider_config_key=provider_key,
                )
            )
            await master_session.commit()
            return

    async def get_integration(
        self,
        integration_id: UUID,
    ) -> KnowledgeIntegration | None:
        """
        Get an integration by ID (scoped to this service's organization).

        Args:
            integration_id: Integration UUID

        Returns:
            KnowledgeIntegration if found, None otherwise
        """
        statement = select(KnowledgeIntegration).where(
            KnowledgeIntegration.integration_id == integration_id,
            KnowledgeIntegration.organisation_id == self.organisation_id,
        )

        result = await self.db.execute(statement)
        return result.scalars().first()

    async def list_integrations(
        self,
        integration_type: IntegrationType | None = None,
        is_active: bool | None = None,
        user_id: UUID | None = None,
        include_user_scoped: bool = True,
    ) -> list[KnowledgeIntegration]:
        """
        List integrations accessible to the organization or user.

        Args:
            integration_type: Optional filter by integration type
            is_active: Optional filter by active status
            user_id: If provided, includes user-scoped integrations created by this user
            include_user_scoped: Whether to include user-scoped integrations

        Returns:
            List of KnowledgeIntegration instances accessible to the user/org
        """
        from sqlalchemy import or_

        # Base query for organization
        statement = select(KnowledgeIntegration).where(
            KnowledgeIntegration.organisation_id == self.organisation_id
        )

        # Filter by scope: include org-wide + user's personal integrations
        if user_id and include_user_scoped:
            statement = statement.where(
                or_(
                    KnowledgeIntegration.integration_scope == IntegrationScope.ORGANIZATION,
                    (
                        (KnowledgeIntegration.integration_scope == IntegrationScope.USER)
                        & (KnowledgeIntegration.created_by_user_id == user_id)
                    ),
                )
            )
        elif not include_user_scoped:
            # Only org-wide integrations
            statement = statement.where(
                KnowledgeIntegration.integration_scope == IntegrationScope.ORGANIZATION
            )

        if integration_type:
            statement = statement.where(KnowledgeIntegration.integration_type == integration_type)

        if is_active is not None:
            statement = statement.where(KnowledgeIntegration.is_active == is_active)

        result = await self.db.execute(statement)
        return list(result.scalars().all())

    async def update_integration(
        self,
        integration_id: UUID,
        name: str | None = None,
        sync_frequency: str | None = None,
        auto_sync_enabled: bool | None = None,
    ) -> KnowledgeIntegration:
        """
        Update integration settings.

        Args:
            integration_id: Integration UUID
            name: New name for the integration
            sync_frequency: New sync frequency
            auto_sync_enabled: Enable/disable automatic syncing

        Returns:
            Updated KnowledgeIntegration

        Raises:
            ValueError: If integration not found
        """
        integration = await self.get_integration(integration_id)

        if not integration:
            raise ValueError(f"Integration {integration_id} not found")

        if name is not None:
            integration.name = name
        if sync_frequency is not None:
            integration.sync_frequency = sync_frequency
        if auto_sync_enabled is not None:
            integration.auto_sync_enabled = auto_sync_enabled

        self.db.add(integration)
        await self.db.commit()
        await self.db.refresh(integration)

        logger.info(f"Updated integration {integration_id}")
        return integration

    async def deactivate_integration(
        self,
        integration_id: UUID,
        error_message: str | None = None,
    ) -> KnowledgeIntegration:
        """
        Deactivate an integration (e.g., due to token refresh failure).

        Args:
            integration_id: Integration UUID
            error_message: Reason for deactivation

        Returns:
            Updated KnowledgeIntegration
        """
        integration = await self.get_integration(integration_id)

        if not integration:
            raise ValueError(f"Integration {integration_id} not found")

        integration.is_active = False
        if error_message:
            integration.last_sync_error = error_message

        self.db.add(integration)
        await self.db.commit()
        await self.db.refresh(integration)

        logger.warning(f"Deactivated integration {integration_id}: {error_message}")
        return integration

    async def delete_integration(
        self,
        integration_id: UUID,
    ) -> bool:
        """
        Delete an integration and revoke Nango connection.

        Args:
            integration_id: Integration UUID

        Returns:
            True if deletion was successful

        Raises:
            ValueError: If integration not found
        """
        integration = await self.get_integration(integration_id)

        if not integration:
            raise ValueError(f"Integration {integration_id} not found")

        # Delete from Nango
        try:
            provider = self._provider_from_type(integration.integration_type)
            logger.info(
                f"Revoking Nango connection for integration {integration_id} "
                f"(provider={provider}, connection_id={integration.connection_id})"
            )
            await self.nango.delete_connection(
                provider=provider,
                connection_id=integration.connection_id,
            )
            logger.info(f"Successfully revoked Nango connection for integration {integration_id}")
        except NangoConnectionError as e:
            logger.warning(
                f"Failed to delete Nango connection: {str(e)}. Proceeding with database deletion."
            )

        # Delete from database (cascade will handle sync history)
        await self.db.delete(integration)
        await self.db.commit()

        logger.info(f"Deleted integration {integration_id} for org {self.organisation_id}")
        return True

    async def check_and_refresh_token(
        self,
        integration: KnowledgeIntegration,
    ) -> bool:
        """
        Check if token needs refresh and refresh if necessary.

        Args:
            integration: KnowledgeIntegration instance

        Returns:
            True if token is valid/refreshed, False if refresh failed
        """
        # Check if token is expiring soon
        if not self.nango.is_token_expiring_soon(integration.access_token_expires_at):
            return True

        try:
            # Refresh token via Nango
            provider = self._provider_from_type(integration.integration_type)
            logger.debug(
                f"Refreshing token for integration {integration.integration_id} "
                f"(provider={provider}, connection_id={integration.connection_id})"
            )
            connection = await self.nango.refresh_token(
                provider=provider,
                connection_id=integration.connection_id,
            )

            # Update expiry time
            credentials = connection.get("credentials", {})
            expires_in = credentials.get("expires_in")
            integration.access_token_expires_at = self.nango.parse_token_expiry(expires_in)

            integration.last_synced_at = datetime.now(UTC)

            self.db.add(integration)
            await self.db.commit()

            logger.info(f"Refreshed token for integration {integration.integration_id}")
            return True

        except Exception as e:
            logger.error(f"Token refresh failed: {str(e)}")
            await self.deactivate_integration(
                integration.integration_id,
                error_message=f"Token refresh failed: {str(e)}",
            )
            return False
