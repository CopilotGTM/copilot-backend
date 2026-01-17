"""
Core integration service for managing third-party OAuth integrations.

This service provides CRUD operations for integrations and coordinates
between the database and Nango client.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
    MessagingChannel,
    MessagingDealLink,
)
from src.infra.auth.user_context import UserIDs
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

    async def close(self) -> None:
        """Close the database session."""
        await self.db.close()

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
            KnowledgeIntegration.integration_id == integration_id,  # type: ignore
            KnowledgeIntegration.organisation_id == self.user_ids.organisation_id,  # type: ignore
        )

        result = await self.db.execute(statement)
        return result.scalars().first()

    async def list_all_active_integrations(
        self, user_id: UUID | None = None
    ) -> list[KnowledgeIntegration]:
        """
        List all active integrations for the organization (excluding admin-only integrations).

        Returns only ORGANIZATION-scoped and USER-scoped integrations that are currently active.
        This is useful for showing all available connections without user-specific filtering.

        Args:
            user_id: Optional user ID to filter USER-scoped integrations.
                     If provided, returns ORG integrations + USER integrations owned by this user.

        Returns:
            List of active KnowledgeIntegration instances, ordered by created_at DESC
        """
        statement = select(KnowledgeIntegration).where(
            KnowledgeIntegration.organisation_id == self.user_ids.organisation_id,  # type: ignore
            cast(KnowledgeIntegration.is_active, Boolean).is_(True),
        )

        if user_id:
            statement = statement.where(
                or_(
                    KnowledgeIntegration.integration_scope == IntegrationScope.ORGANIZATION,  # type: ignore
                    and_(
                        KnowledgeIntegration.integration_scope == IntegrationScope.USER,  # type: ignore
                        KnowledgeIntegration.created_by_user_id == user_id,  # type: ignore
                    ),
                )
            )

        statement = statement.order_by(KnowledgeIntegration.created_at.desc())  # type: ignore[attr-defined]

        result = await self.db.execute(statement)
        return list(result.scalars().all())

    @staticmethod
    def _provider_from_type(integration_type: IntegrationType | str) -> str:
        """
        Convert IntegrationType enum or string to Nango provider key for connection operations.

        For delete operations, we use the Connect integration key (not the provider key)
        because that's what was used during connection creation.

        Args:
            integration_type: The integration type enum or string

        Returns:
            Nango provider config key string (e.g., 'google-calendar-getting-started', 'hubspot')
        """
        # Convert string to enum if needed
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)
        # Use Connect integration key for connection operations (create/delete)
        return to_nango_connect_integration_key(integration_type)

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

    async def list_integration_channels(
        self, integration_id: UUID, deal_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        """
        List available channels for a specific integration from the database.

        Args:
            integration_id: UUID of the integration
            deal_id: Optional UUID of the deal to filter out already linked channels

        Returns:
            List of channel dictionaries
        """
        # Verify integration exists
        integration = await self.get_integration(integration_id)
        if not integration:
            raise ValueError("Integration not found")

        # Query MessagingChannel table
        stmt = select(MessagingChannel).where(
            MessagingChannel.integration_id == integration_id,  # type: ignore
            MessagingChannel.is_archived == "false",  # Only active channels # type: ignore
        )

        if deal_id:
            # Exclude channels already linked to this deal
            subquery = select(MessagingDealLink.channel_id).where(  # type: ignore
                MessagingDealLink.deal_id == deal_id
            )
            stmt = stmt.where(MessagingChannel.channel_id.not_in(subquery))  # type: ignore

        result = await self.db.execute(stmt)
        db_channels = result.scalars().all()

        channels = []
        for ch in db_channels:
            # Extract parent info from metadata
            parent_id = None
            parent_name = None

            if ch.channel_metadata:
                parent_id = ch.channel_metadata.get("parent_id") or ch.channel_metadata.get(
                    "team_id"
                )
                parent_name = ch.channel_metadata.get("parent_name") or ch.channel_metadata.get(
                    "team_name"
                )

            channels.append(
                {
                    "id": ch.external_channel_id,
                    "name": ch.channel_name,
                    "type": ch.channel_type.value
                    if hasattr(ch.channel_type, "value")
                    else ch.channel_type,
                    "parent_id": parent_id,
                    "parent_name": parent_name,
                }
            )

        return channels

    async def get_deal_linked_channels(self, deal_id: UUID) -> list[dict]:
        """
        Get all channels linked to a specific deal.
        """
        stmt = (
            select(
                MessagingChannel.channel_id,  # type: ignore
                MessagingChannel.external_channel_id,  # type: ignore
                MessagingChannel.channel_name,  # type: ignore
                MessagingChannel.channel_type,  # type: ignore
                MessagingChannel.integration_id,  # type: ignore
                KnowledgeIntegration.name.label("integration_name"),  # type: ignore
                KnowledgeIntegration.integration_type,  # type: ignore
                MessagingChannel.channel_metadata,  # type: ignore
            )  # type: ignore
            .join(MessagingDealLink, MessagingDealLink.channel_id == MessagingChannel.channel_id)
            .join(
                KnowledgeIntegration,
                KnowledgeIntegration.integration_id == MessagingChannel.integration_id,
            )
            .where(MessagingDealLink.deal_id == deal_id)
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        channels = []
        for row in rows:
            # Extract parent name from metadata if available (for Teams)
            parent_name = None
            if row.channel_metadata:
                parent_name = row.channel_metadata.get("team_name")

            channels.append(
                {
                    "channel_id": row.channel_id,
                    "external_channel_id": row.external_channel_id,
                    "name": row.channel_name,
                    "type": row.channel_type,
                    "integration_id": row.integration_id,
                    "integration_name": row.integration_name,
                    "integration_type": row.integration_type,  # Helper for grouping
                    "parent_name": parent_name,
                }
            )

        return channels

    async def remove_channel_from_deal(self, deal_id: UUID, channel_id: UUID) -> bool:
        """
        Remove a channel link from a deal.

        If the channel is no longer linked to any deals after removal,
        sets is_monitored=false to stop message syncing.
        """
        stmt = select(MessagingDealLink).where(
            MessagingDealLink.deal_id == deal_id,  # type: ignore
            MessagingDealLink.channel_id == channel_id,  # type: ignore
        )
        result = await self.db.execute(stmt)
        link = result.scalars().first()

        if not link:
            return False

        await self.db.delete(link)

        # Check if channel is still linked to other deals
        other_links_stmt = select(MessagingDealLink).where(
            MessagingDealLink.channel_id == channel_id,  # type: ignore
            MessagingDealLink.deal_id != deal_id,  # type: ignore
        )
        other_links_result = await self.db.execute(other_links_stmt)
        has_other_links = other_links_result.scalars().first() is not None

        # If no other deals linked, stop monitoring this channel
        if not has_other_links:
            channel_stmt = select(MessagingChannel).where(MessagingChannel.channel_id == channel_id)  # type: ignore
            channel_result = await self.db.execute(channel_stmt)
            channel = channel_result.scalars().first()
            if channel and channel.is_monitored == "true":
                channel.is_monitored = "false"

        await self.db.commit()
        return True

    async def link_channel_to_deal(
        self,
        deal_id: UUID,
        channel_data: Any,  # ChannelLinkRequest
    ) -> dict:
        """
        Link a messaging channel to a deal.

        Args:
            deal_id: UUID of the deal
            channel_data: ChannelLinkRequest object containing channel details

        Returns:
            Dictionary with linked channel details
        """
        integration_id = channel_data.integration_id
        external_channel_id = channel_data.id

        # 1. Verify integration exists
        integration = await self.get_integration(integration_id)
        if not integration:
            raise ValueError(f"Integration {integration_id} not found")

        # 2. Find MessagingChannel (must already exist from Teams sync)
        stmt = select(MessagingChannel).where(
            MessagingChannel.integration_id == integration_id,
            MessagingChannel.external_channel_id == external_channel_id,
        )
        result = await self.db.execute(stmt)
        channel = result.scalars().first()

        if not channel:
            raise ValueError(
                f"Channel {external_channel_id} not found. "
                "Channels must be synced from Teams before linking to deals."
            )

        # 3. Mark channel as monitored (will be synced for messages)
        if channel.is_monitored != "true":
            channel.is_monitored = "true"

        # 4. Create Link if not exists
        link_stmt = select(MessagingDealLink).where(
            MessagingDealLink.deal_id == deal_id,  # type: ignore
            MessagingDealLink.channel_id == channel.channel_id,  # type: ignore
        )
        link_result = await self.db.execute(link_stmt)
        link = link_result.scalars().first()

        if not link:
            link = MessagingDealLink(
                organisation_id=self.user_ids.organisation_id,
                integration_id=integration_id,
                deal_id=deal_id,
                channel_id=channel.channel_id,
                detection_method="manual",
                confidence_score=1.0,  # Manual links have full confidence
                linked_by=self.user_ids.user_id,
                is_verified="true",  # Manual links are pre-verified
                verified_by=self.user_ids.user_id,
                link_metadata={
                    "source": "deal_channel_link_api",
                    "channel_name": channel.channel_name,
                    "channel_type": channel.channel_type.value
                    if hasattr(channel.channel_type, "value")
                    else channel.channel_type,
                    "integration_type": integration.integration_type.value
                    if hasattr(integration.integration_type, "value")
                    else integration.integration_type,
                },
            )
            self.db.add(link)

        # Commit both channel update (is_monitored) and link creation
        await self.db.commit()

        return {
            "deal_id": str(deal_id),
            "channel_id": str(channel.channel_id),
            "external_channel_id": channel.external_channel_id,
            "name": channel.channel_name,
            "type": channel.channel_type.value
            if hasattr(channel.channel_type, "value")
            else channel.channel_type,
            "integration_id": str(integration_id),
        }
