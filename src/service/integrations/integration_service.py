"""
Core integration service for managing third-party OAuth integrations.

This service provides CRUD operations for integrations and coordinates
between the database and Nango client.
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import UserIDs
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.nango_provider_mapping import to_nango_provider_key

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
        user_ids: UserIDs,
        nango_client: NangoClient | None = None,
    ):
        """
        Initialize the integration service.

        Args:
            db_session: Async SQLAlchemy database session
            user_ids: UserIDs instance for scoping queries
            nango_client: Nango client instance (creates new one if not provided)
        """
        self.db = db_session
        self.user_ids = user_ids
        self.nango = nango_client or NangoClient()

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
            KnowledgeIntegration.organisation_id == self.user_ids.organisation_id,
        )

        result = await self.db.execute(statement)
        return result.scalars().first()

    async def list_all_active_integrations(self) -> list[KnowledgeIntegration]:
        """
        List all active integrations for the organization (excluding admin-only integrations).

        Returns only ORGANIZATION-scoped and USER-scoped integrations that are currently active.
        This is useful for showing all available connections without user-specific filtering.

        Returns:
            List of active KnowledgeIntegration instances, ordered by created_at DESC
        """
        statement = (
            select(KnowledgeIntegration)
            .where(
                KnowledgeIntegration.organisation_id == self.user_ids.organisation_id,
                KnowledgeIntegration.is_active,
            )
            .order_by(KnowledgeIntegration.created_at.desc())
        )

        result = await self.db.execute(statement)
        return list(result.scalars().all())

    @staticmethod
    def _provider_from_type(integration_type: IntegrationType | str) -> str:
        """
        Convert IntegrationType enum or string to Nango provider key.

        Args:
            integration_type: The integration type enum or string

        Returns:
            Nango provider key string (e.g., 'hubspot', 'salesforce')
        """
        # Convert string to enum if needed
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)
        return to_nango_provider_key(integration_type)

    async def delete_integration_from_nango(
        self,
        connection_id: str,
        integration_type: IntegrationType,
    ) -> bool:
        """
        Delete an integration connection from Nango.

        Args:
            connection_id: The Nango connection ID string (e.g., UUID string)
            integration_type: IntegrationType enum for the integration
        Returns:
            True if deletion was successful or connection doesn't exist
        """

        try:
            provider = self._provider_from_type(integration_type)
            logger.info(
                f"Revoking Nango connection for (provider={provider}, connection_id={connection_id})"
            )
            await self.nango.delete_connection(
                provider=provider,
                connection_id=str(connection_id),  # Ensure it's a string
            )
            logger.info(f"Successfully revoked Nango connection for connection_id {connection_id}")
        except NangoConnectionError as e:
            logger.warning(
                f"Failed to delete Nango connection: {str(e)}. Proceeding with soft deletion."
            )
        return True
