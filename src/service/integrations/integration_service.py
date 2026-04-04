"""
Core integration service for managing OAuth integrations.

This service provides CRUD operations for integrations.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
    MessagingChannel,
    MessagingDealLink,
)
from src.domain.models.organisation_tables.sync.messaging_models import (
    MessagingChannelIntegrationState,
    MessagingChannelMember,
)
from src.infra.auth.user_context import UserIDs

logger = logging.getLogger(__name__)

# Human-readable labels for channel types
CHANNEL_TYPE_LABELS = {
    "public": "Public Channel",
    "private": "Private Channel",
    "direct_message": "Direct Message",
    "group_message": "Group Chat",
    "thread": "Thread",
    "meeting": "Meeting Chat",
}


def get_channel_type_label(channel_type: str) -> str:
    """Get human-readable label for channel type."""
    return CHANNEL_TYPE_LABELS.get(channel_type, channel_type.replace("_", " ").title())


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
    ):
        """
        Initialize the integration service.

        Args:
            db_session: Async SQLAlchemy database session
            user_ids: UserIDs instance for scoping queries
        """
        self.db = db_session
        self.user_ids = user_ids

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
            col(KnowledgeIntegration.integration_id) == integration_id,
            col(KnowledgeIntegration.organisation_id) == self.user_ids.organisation_id,
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
            col(KnowledgeIntegration.organisation_id) == self.user_ids.organisation_id,
            cast(col(KnowledgeIntegration.is_active), Boolean).is_(True),
        )

        if user_id:
            statement = statement.where(
                or_(
                    col(KnowledgeIntegration.integration_scope) == IntegrationScope.ORGANIZATION,
                    and_(
                        col(KnowledgeIntegration.integration_scope) == IntegrationScope.USER,
                        col(KnowledgeIntegration.created_by_user_id) == user_id,
                    ),
                )
            )

        statement = statement.order_by(col(KnowledgeIntegration.created_at).desc())

        result = await self.db.execute(statement)
        return list(result.scalars().all())

    async def revoke_custom_oauth_token(
        self,
        integration: KnowledgeIntegration,
    ) -> bool:
        """Revoke tokens at the provider for custom OAuth integrations.

        Prefers revoking refresh_token (invalidates both tokens at Google).
        Falls back to access_token if no refresh_token. Logs but does not
        raise on failure — we still want to deactivate locally even if
        revocation fails (e.g., token already expired at provider).
        """
        from src.service.integrations.providers.provider_factory import ProviderFactory

        credentials = integration.credentials or {}
        refresh_token = credentials.get("refresh_token")
        access_token = credentials.get("access_token")
        token_to_revoke = refresh_token or access_token
        if not token_to_revoke:
            logger.warning("No token to revoke for integration %s", integration.integration_id)
            return False

        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        try:
            provider = ProviderFactory.get_provider(integration_type)
            await provider.revoke_token(str(token_to_revoke))
            logger.info(
                "Revoked custom OAuth token for integration %s",
                integration.integration_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to revoke custom OAuth token for %s: %s. Proceeding with deactivation.",
                integration.integration_id,
                str(e),
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

        # Query MessagingChannel table via integration state for visibility
        stmt = (
            select(MessagingChannel)
            .join(
                MessagingChannelIntegrationState,
                col(MessagingChannelIntegrationState.channel_id)
                == col(MessagingChannel.channel_id),
            )
            .where(
                col(MessagingChannelIntegrationState.integration_id) == integration_id,
                col(MessagingChannelIntegrationState.is_deleted).is_(False),
                col(MessagingChannel.is_archived) == "false",
            )
        )

        if deal_id:
            # Exclude channels already linked to this deal
            subquery = select(col(MessagingDealLink.channel_id)).where(
                col(MessagingDealLink.deal_id) == deal_id
            )
            stmt = stmt.where(col(MessagingChannel.channel_id).not_in(subquery))

        result = await self.db.execute(stmt)
        db_channels = result.scalars().all()

        # Collect channel IDs to batch-fetch members
        channel_ids = [ch.channel_id for ch in db_channels]

        # Fetch members for all channels in one query (limit to active members)
        channel_members_map: dict[str, list[dict]] = {}
        if channel_ids:
            members_stmt = (
                select(
                    col(MessagingChannelMember.channel_id),
                    col(MessagingChannelMember.display_name),
                    col(MessagingChannelMember.email),
                    col(MessagingChannelMember.member_role),
                )
                .where(
                    col(MessagingChannelMember.channel_id).in_(channel_ids),
                    col(MessagingChannelMember.left_at).is_(None),
                )
                .order_by(
                    col(MessagingChannelMember.channel_id),
                    col(MessagingChannelMember.member_role),
                )
            )
            members_result = await self.db.execute(members_stmt)
            members_rows = members_result.all()

            # Group members by channel_id (limit to 10 per channel for display)
            for row in members_rows:
                channel_id_str = str(row.channel_id)
                if channel_id_str not in channel_members_map:
                    channel_members_map[channel_id_str] = []
                if len(channel_members_map[channel_id_str]) < 10:
                    channel_members_map[channel_id_str].append(
                        {
                            "name": row.display_name,
                            "email": row.email,
                            "role": row.member_role,
                        }
                    )

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

            # Determine channel type value
            channel_type = (
                ch.channel_type.value if hasattr(ch.channel_type, "value") else ch.channel_type
            )

            # Get members for this channel
            members = channel_members_map.get(str(ch.channel_id), [])

            channels.append(
                {
                    "id": ch.external_channel_id,
                    "name": ch.channel_name,
                    "type": channel_type,
                    "type_label": get_channel_type_label(channel_type),
                    "parent_id": parent_id,
                    "parent_name": parent_name,
                    "description": ch.channel_description,
                    "member_count": ch.participant_count or 0,
                    "members": members,
                    "last_activity_at": ch.last_activity_at,
                }
            )

        return channels

    async def get_deal_linked_channels(self, deal_id: UUID) -> list[dict]:
        """
        Get all channels linked to a specific deal.
        """
        stmt = (
            select(
                col(MessagingChannel.channel_id),
                col(MessagingChannel.external_channel_id),
                col(MessagingChannel.channel_name),
                col(MessagingChannel.channel_type),
                col(MessagingChannel.integration_id),
                col(KnowledgeIntegration.name).label("integration_name"),
                col(KnowledgeIntegration.integration_type),
                col(MessagingChannel.channel_metadata),
            )
            .join(
                MessagingDealLink,
                col(MessagingDealLink.channel_id) == col(MessagingChannel.channel_id),
            )
            .join(
                KnowledgeIntegration,
                col(KnowledgeIntegration.integration_id) == col(MessagingChannel.integration_id),
            )
            .where(col(MessagingDealLink.deal_id) == deal_id)
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
            col(MessagingDealLink.deal_id) == deal_id,
            col(MessagingDealLink.channel_id) == channel_id,
        )
        result = await self.db.execute(stmt)
        link = result.scalars().first()

        if not link:
            return False

        await self.db.delete(link)

        # Check if channel is still linked to other deals
        other_links_stmt = select(MessagingDealLink).where(
            col(MessagingDealLink.channel_id) == channel_id,
            col(MessagingDealLink.deal_id) != deal_id,
        )
        other_links_result = await self.db.execute(other_links_stmt)
        has_other_links = other_links_result.scalars().first() is not None

        # If no other deals linked, stop monitoring this channel
        if not has_other_links:
            channel_stmt = select(MessagingChannel).where(
                col(MessagingChannel.channel_id) == channel_id
            )
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
            col(MessagingChannel.integration_id) == integration_id,
            col(MessagingChannel.external_channel_id) == external_channel_id,
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
            col(MessagingDealLink.deal_id) == deal_id,
            col(MessagingDealLink.channel_id) == channel.channel_id,
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
