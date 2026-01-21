"""
Deal channel messages sync service.

This service handles syncing messages from Teams channels that users have linked to deals.
It follows the existing service patterns in the codebase.

Key features:
- Query deal-linked channels with deduplication by external_channel_id
- Random integration selection for rate limit distribution
- Fetch messages via Teams Graph API (Nango proxy)
- Store messages in messaging_messages table

Architecture:
- Service layer handles business logic and database operations
- Provider layer (teams.py) handles external API calls
- Task layer (deal_channel_messages_tasks.py) handles Celery orchestration

Session Management:
    This service follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query deal-linked channels, extract data, release session
    2. For each channel:
       a. Fetch messages from API (NO session held)
       b. Session N: Write messages to DB, update channel sync state, release session
"""

# mypy: disable-error-code="arg-type,call-overload,union-attr"

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
    MessagingChannel,
)
from src.domain.models.organisation_tables.sync.messaging_models import (
    ChannelType,
    MessageType,
    MessagingDealLink,
    MessagingMessage,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers.messaging.teams import (
    FetchMessagesResult,
    fetch_chat_messages,
    fetch_messages,
)

logger = get_logger(__name__)


@dataclass
class DealLinkedChannelInfo:
    """Information about a deal-linked channel for sync."""

    external_channel_id: str
    channel_id: UUID
    integration_id: UUID
    channel_metadata: dict[str, Any]
    channel_name: str | None
    channel_type: str
    last_synced_at: datetime | None
    available_integrations: int
    last_sync_cursor: str | None = None  # Stores deltaLink for incremental sync


@dataclass
class ChannelSyncResult:
    """Result of syncing a single channel."""

    channel_id: UUID
    channel_name: str | None
    integration_id: UUID
    messages_synced: int
    status: str  # "success", "failed", "skipped"
    error: str | None = None


@dataclass
class OrgSyncResult:
    """Result of syncing all channels for an organization."""

    status: str  # "completed", "partial", "failed"
    channels_synced: int
    channels_failed: int
    channels_skipped: int
    messages_synced: int
    channel_results: list[ChannelSyncResult]
    error: str | None = None


class DealChannelMessagesService:
    """
    Service for syncing messages from deal-linked Teams channels.

    This service:
    1. Queries MessagingDealLink for user-selected channels
    2. Groups by external_channel_id to avoid duplicate fetches
    3. Randomly selects integration for load distribution
    4. Fetches messages via Teams Graph API (Nango proxy)
    5. Stores in messaging_messages table

    Session Management:
    - Uses fresh session pattern to avoid holding connections during API calls
    - Sessions are acquired only when DB operations are needed
    - Sessions are released before external API calls
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        """Initialize with session for backward compatibility.

        Note: The session is stored but fresh sessions are acquired for each
        operation to follow the fresh session pattern.
        """
        self.db = session
        self.organisation_id = organisation_id
        self.nango = NangoClient()

    async def sync_all_deal_channels_fresh(
        self,
        integration_id: UUID | None = None,
    ) -> OrgSyncResult:
        """
        Sync messages for all deal-linked channels using full fresh session pattern.

        This method does NOT require an external session - it manages all database
        sessions internally:
        1. Acquires fresh session to query deal-linked channels, releases
        2. For each channel:
           a. Fetch messages from API (NO session held)
           b. Fresh session: Write messages to DB, update channel sync state

        Args:
            integration_id: Optional filter to sync only channels for this integration

        Returns:
            OrgSyncResult with summary of sync operation
        """
        logger.info(
            "Starting deal channel messages sync (fresh sessions) for org: %s",
            self.organisation_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query deal-linked channels with fresh session
        # ─────────────────────────────────────────────────────────────────────
        channels: list[DealLinkedChannelInfo] = []
        async for session in acquire_org_async_session(self.organisation_id):
            channels = await self._get_unique_deal_linked_channels_with_session(
                session, integration_id
            )
        # Session is now released

        if not channels:
            logger.info("No deal-linked channels found for org: %s", self.organisation_id)
            return OrgSyncResult(
                status="completed",
                channels_synced=0,
                channels_failed=0,
                channels_skipped=0,
                messages_synced=0,
                channel_results=[],
            )

        logger.info(
            "Found %d unique deal-linked channel(s) to sync for org: %s",
            len(channels),
            self.organisation_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: For each channel, fetch messages then persist with fresh sessions
        # ─────────────────────────────────────────────────────────────────────
        channel_results: list[ChannelSyncResult] = []
        channels_synced = 0
        channels_failed = 0
        channels_skipped = 0
        total_messages = 0

        for channel_info in channels:
            result = await self._sync_channel_messages_fully_fresh(channel_info)
            channel_results.append(result)

            if result.status == "success":
                channels_synced += 1
                total_messages += result.messages_synced
            elif result.status == "failed":
                channels_failed += 1
            else:
                channels_skipped += 1

        # Determine overall status
        if channels_failed == 0:
            status = "completed"
        elif channels_synced > 0:
            status = "partial"
        else:
            status = "failed"

        logger.info(
            "Deal channel messages sync completed for org %s: "
            "synced=%d failed=%d skipped=%d messages=%d",
            self.organisation_id,
            channels_synced,
            channels_failed,
            channels_skipped,
            total_messages,
        )

        return OrgSyncResult(
            status=status,
            channels_synced=channels_synced,
            channels_failed=channels_failed,
            channels_skipped=channels_skipped,
            messages_synced=total_messages,
            channel_results=channel_results,
        )

    async def sync_all_deal_channels(
        self,
        integration_id: UUID | None = None,
    ) -> OrgSyncResult:
        """
        Sync messages for all deal-linked channels in the organization.

        Uses fresh session pattern:
        1. Session 1: Query deal-linked channels, extract data, release session
        2. For each channel:
           a. Fetch messages from API (NO session held)
           b. Session N: Write messages to DB, update channel sync state

        Args:
            integration_id: Optional filter to sync only channels for this integration

        Returns:
            OrgSyncResult with summary of sync operation
        """
        logger.info(
            "Starting deal channel messages sync for org: %s",
            self.organisation_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query deal-linked channels (using passed session or fresh)
        # For backward compat, we use self.db here since it's a quick query
        # ─────────────────────────────────────────────────────────────────────
        channels = await self._get_unique_deal_linked_channels_internal(integration_id)

        if not channels:
            logger.info("No deal-linked channels found for org: %s", self.organisation_id)
            return OrgSyncResult(
                status="completed",
                channels_synced=0,
                channels_failed=0,
                channels_skipped=0,
                messages_synced=0,
                channel_results=[],
            )

        logger.info(
            "Found %d unique deal-linked channel(s) to sync for org: %s",
            len(channels),
            self.organisation_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: For each channel, fetch messages then persist
        # API calls happen WITHOUT holding a database session
        # ─────────────────────────────────────────────────────────────────────
        channel_results: list[ChannelSyncResult] = []
        channels_synced = 0
        channels_failed = 0
        channels_skipped = 0
        total_messages = 0

        for channel_info in channels:
            result = await self._sync_channel_messages_fresh_session(channel_info)
            channel_results.append(result)

            if result.status == "success":
                channels_synced += 1
                total_messages += result.messages_synced
            elif result.status == "failed":
                channels_failed += 1
            else:
                channels_skipped += 1

        # Determine overall status
        if channels_failed == 0:
            status = "completed"
        elif channels_synced > 0:
            status = "partial"
        else:
            status = "failed"

        logger.info(
            "Deal channel messages sync completed for org %s: "
            "synced=%d failed=%d skipped=%d messages=%d",
            self.organisation_id,
            channels_synced,
            channels_failed,
            channels_skipped,
            total_messages,
        )

        return OrgSyncResult(
            status=status,
            channels_synced=channels_synced,
            channels_failed=channels_failed,
            channels_skipped=channels_skipped,
            messages_synced=total_messages,
            channel_results=channel_results,
        )

    async def _get_unique_deal_linked_channels_internal(
        self,
        integration_id: UUID | None = None,
    ) -> list[DealLinkedChannelInfo]:
        """
        Get channels linked to deals that need message syncing.

        Uses self.db for backward compatibility (quick query, no long hold).

        Args:
            integration_id: Optional filter to get channels for specific integration

        Returns:
            List of DealLinkedChannelInfo for channels to sync
        """
        # Build base query
        stmt = (
            select(
                col(MessagingChannel.channel_id),
                col(MessagingChannel.external_channel_id),
                col(MessagingChannel.integration_id),
                col(MessagingChannel.channel_metadata),
                col(MessagingChannel.channel_name),
                col(MessagingChannel.channel_type),
                col(MessagingChannel.last_synced_at),
                col(MessagingChannel.last_sync_cursor),
            )
            .select_from(MessagingDealLink)
            .join(
                MessagingChannel,
                col(MessagingDealLink.channel_id) == col(MessagingChannel.channel_id),
            )
            .join(
                KnowledgeIntegration,
                col(MessagingChannel.integration_id) == col(KnowledgeIntegration.integration_id),
            )
            .where(
                col(MessagingDealLink.organisation_id) == self.organisation_id,
                col(MessagingDealLink.channel_id).isnot(None),  # Channel-level links only
                col(MessagingDealLink.is_deleted).is_(False),
                col(MessagingChannel.is_deleted).is_(False),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
                col(KnowledgeIntegration.integration_type) == IntegrationType.TEAMS,
            )
            .distinct(MessagingChannel.external_channel_id)
            .order_by(
                MessagingChannel.external_channel_id,
                col(MessagingChannel.last_synced_at).desc().nulls_last(),
            )
        )

        # Add integration filter if specified
        if integration_id:
            stmt = stmt.where(col(MessagingChannel.integration_id) == integration_id)

        result = await self.db.execute(stmt)
        rows = result.all()

        channels_to_sync: list[DealLinkedChannelInfo] = []
        for row in rows:
            channels_to_sync.append(
                DealLinkedChannelInfo(
                    external_channel_id=row.external_channel_id,
                    channel_id=row.channel_id,
                    integration_id=row.integration_id,
                    channel_metadata=row.channel_metadata or {},
                    channel_name=row.channel_name,
                    channel_type=row.channel_type,
                    last_synced_at=row.last_synced_at,
                    available_integrations=1,  # Simplified - one per external_channel_id
                    last_sync_cursor=row.last_sync_cursor,  # Delta link for incremental sync
                )
            )

        return channels_to_sync

    async def get_unique_deal_linked_channels(self) -> list[DealLinkedChannelInfo]:
        """
        Get channels linked to deals that need message syncing.

        Queries MessagingDealLink (source of truth for deal-channel relationships)
        and joins to MessagingChannel/KnowledgeIntegration for channel details.

        Uses DISTINCT ON external_channel_id to avoid duplicate fetches when
        the same Teams channel exists across multiple integrations.

        Returns:
            List of DealLinkedChannelInfo for channels to sync
        """
        return await self._get_unique_deal_linked_channels_internal()

    async def _get_unique_deal_linked_channels_with_session(
        self,
        session: AsyncSession,
        integration_id: UUID | None = None,
    ) -> list[DealLinkedChannelInfo]:
        """
        Get channels linked to deals using the provided session.

        Args:
            session: Database session to use
            integration_id: Optional filter for specific integration

        Returns:
            List of DealLinkedChannelInfo for channels to sync
        """
        # Build base query
        stmt = (
            select(
                col(MessagingChannel.channel_id),
                col(MessagingChannel.external_channel_id),
                col(MessagingChannel.integration_id),
                col(MessagingChannel.channel_metadata),
                col(MessagingChannel.channel_name),
                col(MessagingChannel.channel_type),
                col(MessagingChannel.last_synced_at),
                col(MessagingChannel.last_sync_cursor),
            )
            .select_from(MessagingDealLink)
            .join(
                MessagingChannel,
                col(MessagingDealLink.channel_id) == col(MessagingChannel.channel_id),
            )
            .join(
                KnowledgeIntegration,
                col(MessagingChannel.integration_id) == col(KnowledgeIntegration.integration_id),
            )
            .where(
                col(MessagingDealLink.organisation_id) == self.organisation_id,
                col(MessagingDealLink.channel_id).isnot(None),  # Channel-level links only
                col(MessagingDealLink.is_deleted).is_(False),
                col(MessagingChannel.is_deleted).is_(False),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
                col(KnowledgeIntegration.integration_type) == IntegrationType.TEAMS,
            )
            .distinct(MessagingChannel.external_channel_id)
            .order_by(
                MessagingChannel.external_channel_id,
                col(MessagingChannel.last_synced_at).desc().nulls_last(),
            )
        )

        # Add integration filter if specified
        if integration_id:
            stmt = stmt.where(col(MessagingChannel.integration_id) == integration_id)

        result = await session.execute(stmt)
        rows = result.all()

        channels_to_sync: list[DealLinkedChannelInfo] = []
        for row in rows:
            channels_to_sync.append(
                DealLinkedChannelInfo(
                    external_channel_id=row.external_channel_id,
                    channel_id=row.channel_id,
                    integration_id=row.integration_id,
                    channel_metadata=row.channel_metadata or {},
                    channel_name=row.channel_name,
                    channel_type=row.channel_type,
                    last_synced_at=row.last_synced_at,
                    available_integrations=1,  # Simplified - one per external_channel_id
                    last_sync_cursor=row.last_sync_cursor,  # Delta link for incremental sync
                )
            )

        return channels_to_sync

    async def _sync_channel_messages_fully_fresh(
        self,
        channel_info: DealLinkedChannelInfo,
    ) -> ChannelSyncResult:
        """
        Sync messages for a channel using fully fresh session pattern.

        This method does NOT use self.db at all - it manages all sessions internally.

        Flow:
        1. Get integration connection_id (fresh session, quick read)
        2. Fetch messages from API (NO session held)
        3. Write messages to DB (fresh session)
        4. Update channel sync state (fresh session)

        Args:
            channel_info: Channel information from get_unique_deal_linked_channels

        Returns:
            ChannelSyncResult with sync outcome
        """
        try:
            # ─────────────────────────────────────────────────────────────────
            # Phase 1: Get integration connection_id (fresh session, quick read)
            # ─────────────────────────────────────────────────────────────────
            connection_id: str | None = None
            async for session in acquire_org_async_session(self.organisation_id):
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == channel_info.integration_id
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()
                if integration:
                    connection_id = integration.connection_id
            # Session is now released

            if not connection_id:
                logger.warning(
                    "Integration %s not found or has no connection for channel %s",
                    channel_info.integration_id,
                    channel_info.channel_name,
                )
                return ChannelSyncResult(
                    channel_id=channel_info.channel_id,
                    channel_name=channel_info.channel_name,
                    integration_id=channel_info.integration_id,
                    messages_synced=0,
                    status="failed",
                    error="Integration not found or has no connection",
                )

            # ─────────────────────────────────────────────────────────────────
            # Phase 2: Fetch messages from API (NO session held)
            # ─────────────────────────────────────────────────────────────────
            is_initial = channel_info.last_sync_cursor is None
            fetch_result = await self._fetch_messages_with_connection_id(
                channel_info=channel_info,
                connection_id=connection_id,
                delta_link=channel_info.last_sync_cursor,
            )

            messages = fetch_result.messages
            new_delta_link = fetch_result.delta_link

            if not messages:
                logger.debug(
                    "No new messages for channel %s (initial=%s)",
                    channel_info.channel_name,
                    is_initial,
                )
                # Still update deltaLink even if no messages (for subsequent syncs)
                if new_delta_link:
                    await self._update_channel_sync_state_fresh_session(
                        channel_info.channel_id,
                        delta_link=new_delta_link,
                    )
                return ChannelSyncResult(
                    channel_id=channel_info.channel_id,
                    channel_name=channel_info.channel_name,
                    integration_id=channel_info.integration_id,
                    messages_synced=0,
                    status="success",
                )

            # ─────────────────────────────────────────────────────────────────
            # Phase 3: Store messages (fresh session)
            # ─────────────────────────────────────────────────────────────────
            messages_stored = await self._store_messages_fresh_session(
                channel_id=channel_info.channel_id,
                integration_id=channel_info.integration_id,
                messages=messages,
            )

            # ─────────────────────────────────────────────────────────────────
            # Phase 4: Update channel's last_synced_at and delta_link (fresh session)
            # ─────────────────────────────────────────────────────────────────
            await self._update_channel_sync_state_fresh_session(
                channel_info.channel_id,
                delta_link=new_delta_link,
            )

            logger.info(
                "Synced %d messages from channel %s (integration: %s, initial=%s)",
                messages_stored,
                channel_info.channel_name,
                channel_info.integration_id,
                is_initial,
            )

            return ChannelSyncResult(
                channel_id=channel_info.channel_id,
                channel_name=channel_info.channel_name,
                integration_id=channel_info.integration_id,
                messages_synced=messages_stored,
                status="success",
            )

        except Exception as e:
            logger.error(
                "Failed to sync channel %s: %s",
                channel_info.channel_name,
                str(e),
                exc_info=True,
            )
            return ChannelSyncResult(
                channel_id=channel_info.channel_id,
                channel_name=channel_info.channel_name,
                integration_id=channel_info.integration_id,
                messages_synced=0,
                status="failed",
                error=str(e),
            )

    async def _sync_channel_messages_fresh_session(
        self,
        channel_info: DealLinkedChannelInfo,
    ) -> ChannelSyncResult:
        """
        Sync messages for a single deal-linked channel using fresh session pattern.

        Flow:
        1. Get integration connection_id (quick DB read using existing session)
        2. Fetch messages from API (NO session held)
        3. Write messages to DB with fresh session
        4. Update channel sync state with fresh session

        Args:
            channel_info: Channel information from get_unique_deal_linked_channels

        Returns:
            ChannelSyncResult with sync outcome
        """
        try:
            # ─────────────────────────────────────────────────────────────────
            # Phase 1: Get integration connection_id (quick read, use existing session)
            # ─────────────────────────────────────────────────────────────────
            integration = await self._get_integration(channel_info.integration_id)
            if not integration or not integration.connection_id:
                logger.warning(
                    "Integration %s not found or has no connection for channel %s",
                    channel_info.integration_id,
                    channel_info.channel_name,
                )
                return ChannelSyncResult(
                    channel_id=channel_info.channel_id,
                    channel_name=channel_info.channel_name,
                    integration_id=channel_info.integration_id,
                    messages_synced=0,
                    status="failed",
                    error="Integration not found or has no connection",
                )

            # Extract connection_id for API call (session can be released after this)
            connection_id = integration.connection_id

            # ─────────────────────────────────────────────────────────────────
            # Phase 2: Fetch messages from API (NO session held)
            # ─────────────────────────────────────────────────────────────────
            is_initial = channel_info.last_sync_cursor is None
            fetch_result = await self._fetch_messages_with_connection_id(
                channel_info=channel_info,
                connection_id=connection_id,
                delta_link=channel_info.last_sync_cursor,
            )

            messages = fetch_result.messages
            new_delta_link = fetch_result.delta_link

            if not messages:
                logger.debug(
                    "No new messages for channel %s (initial=%s)",
                    channel_info.channel_name,
                    is_initial,
                )
                # Still update deltaLink even if no messages (for subsequent syncs)
                if new_delta_link:
                    await self._update_channel_sync_state_fresh_session(
                        channel_info.channel_id,
                        delta_link=new_delta_link,
                    )
                return ChannelSyncResult(
                    channel_id=channel_info.channel_id,
                    channel_name=channel_info.channel_name,
                    integration_id=channel_info.integration_id,
                    messages_synced=0,
                    status="success",
                )

            # ─────────────────────────────────────────────────────────────────
            # Phase 3: Store messages (fresh session)
            # ─────────────────────────────────────────────────────────────────
            messages_stored = await self._store_messages_fresh_session(
                channel_id=channel_info.channel_id,
                integration_id=channel_info.integration_id,
                messages=messages,
            )

            # ─────────────────────────────────────────────────────────────────
            # Phase 4: Update channel's last_synced_at and delta_link (fresh session)
            # ─────────────────────────────────────────────────────────────────
            await self._update_channel_sync_state_fresh_session(
                channel_info.channel_id,
                delta_link=new_delta_link,
            )

            logger.info(
                "Synced %d messages from channel %s (integration: %s, initial=%s)",
                messages_stored,
                channel_info.channel_name,
                channel_info.integration_id,
                is_initial,
            )

            return ChannelSyncResult(
                channel_id=channel_info.channel_id,
                channel_name=channel_info.channel_name,
                integration_id=channel_info.integration_id,
                messages_synced=messages_stored,
                status="success",
            )

        except Exception as e:
            logger.error(
                "Failed to sync channel %s: %s",
                channel_info.channel_name,
                str(e),
                exc_info=True,
            )
            return ChannelSyncResult(
                channel_id=channel_info.channel_id,
                channel_name=channel_info.channel_name,
                integration_id=channel_info.integration_id,
                messages_synced=0,
                status="failed",
                error=str(e),
            )

    async def sync_channel_messages(
        self,
        channel_info: DealLinkedChannelInfo,
    ) -> ChannelSyncResult:
        """
        Sync messages for a single deal-linked channel using Delta Query.

        Uses Microsoft Graph Delta Query for efficient incremental sync:
        - Initial sync: Fetches all messages, receives deltaLink
        - Incremental sync: Uses stored deltaLink, returns ONLY changes

        Args:
            channel_info: Channel information from get_unique_deal_linked_channels

        Returns:
            ChannelSyncResult with sync outcome
        """
        # Delegate to fresh session implementation
        return await self._sync_channel_messages_fresh_session(channel_info)

    async def _get_integration(self, integration_id: UUID) -> KnowledgeIntegration | None:
        """Get integration by ID."""
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_id) == integration_id
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _fetch_messages_with_connection_id(
        self,
        channel_info: DealLinkedChannelInfo,
        connection_id: str,
        delta_link: str | None = None,
    ) -> FetchMessagesResult:
        """
        Fetch messages from Teams API using connection_id directly.

        This method accepts connection_id as a string, allowing it to be called
        without holding a reference to the ORM integration object.

        Args:
            channel_info: Channel information
            connection_id: Nango connection ID string
            delta_link: Optional stored deltaLink from previous sync

        Returns:
            FetchMessagesResult with messages and new deltaLink for next sync
        """
        channel_type = channel_info.channel_type

        if channel_type in (ChannelType.PUBLIC, ChannelType.PRIVATE):
            # Team channel - needs team_id from metadata
            is_initial = delta_link is None
            team_id = channel_info.channel_metadata.get("team_id")
            if not team_id:
                raise ValueError(
                    f"Missing team_id in channel_metadata for channel {channel_info.channel_id}"
                )

            return await fetch_messages(
                nango=self.nango,
                connection_id=connection_id,
                team_id=team_id,
                channel_id=channel_info.external_channel_id,
                is_initial=is_initial,
                last_synced_at=channel_info.last_synced_at,
                delta_link=delta_link,
            )

        elif channel_type in (ChannelType.GROUP_MESSAGE, ChannelType.MEETING):
            # Chat - use chat_id directly
            is_initial = channel_info.last_synced_at is None
            return await fetch_chat_messages(
                nango=self.nango,
                connection_id=connection_id,
                chat_id=channel_info.external_channel_id,
                is_initial=is_initial,
                last_synced_at=channel_info.last_synced_at,
                delta_link=delta_link,
            )

        elif channel_type == ChannelType.DIRECT_MESSAGE:
            logger.debug("Skipping 1:1 DM channel %s", channel_info.channel_id)
            return FetchMessagesResult(messages=[], delta_link=None)

        else:
            logger.warning(
                "Unknown channel type %s for channel %s",
                channel_type,
                channel_info.channel_id,
            )
            return FetchMessagesResult(messages=[], delta_link=None)

    async def _fetch_messages(
        self,
        channel_info: DealLinkedChannelInfo,
        integration: KnowledgeIntegration,
        delta_link: str | None = None,
    ) -> FetchMessagesResult:
        """
        Fetch messages from Teams API via provider layer using Delta Query.

        Uses Microsoft Graph Delta Query for efficient incremental sync:
        - Initial sync: /messages/delta (fetches all, returns deltaLink)
        - Incremental sync: Use stored deltaLink (returns ONLY changes)

        Args:
            channel_info: Channel information
            integration: Integration with Nango connection
            delta_link: Optional stored deltaLink from previous sync

        Returns:
            FetchMessagesResult with messages and new deltaLink for next sync
        """
        return await self._fetch_messages_with_connection_id(
            channel_info=channel_info,
            connection_id=integration.connection_id,  # type: ignore
            delta_link=delta_link,
        )

    async def _store_messages_fresh_session(
        self,
        channel_id: UUID,
        integration_id: UUID,
        messages: list[dict[str, Any]],
    ) -> int:
        """
        Store fetched messages in the database using a fresh session.

        Acquires its own database session to avoid holding connections
        during long-running API calls.

        Args:
            channel_id: Channel UUID (our internal ID)
            integration_id: Integration UUID
            messages: List of message dicts from Teams API

        Returns:
            Number of new messages stored
        """
        async for session in acquire_org_async_session(self.organisation_id):
            return await self._store_messages_internal(
                session, channel_id, integration_id, messages
            )
        return 0

    async def _store_messages(
        self,
        channel_id: UUID,
        integration_id: UUID,
        messages: list[dict[str, Any]],
    ) -> int:
        """
        Store fetched messages in the database (backward compatible).

        Uses self.db session. For fresh session pattern, use _store_messages_fresh_session.

        Args:
            channel_id: Channel UUID (our internal ID)
            integration_id: Integration UUID
            messages: List of message dicts from Teams API

        Returns:
            Number of new messages stored
        """
        return await self._store_messages_internal(self.db, channel_id, integration_id, messages)

    async def _store_messages_internal(
        self,
        session: AsyncSession,
        channel_id: UUID,
        integration_id: UUID,
        messages: list[dict[str, Any]],
    ) -> int:
        """
        Internal: Store fetched messages in the database.

        Uses upsert logic to avoid duplicates (based on external_message_id).

        Args:
            session: Database session to use
            channel_id: Channel UUID (our internal ID)
            integration_id: Integration UUID
            messages: List of message dicts from Teams API

        Returns:
            Number of new messages stored
        """
        stored_count = 0

        # Use no_autoflush to prevent SQLAlchemy from auto-flushing pending INSERTs
        # when running SELECT queries in the loop. This avoids duplicate key errors
        # when the same message appears multiple times in the incoming batch.
        with session.no_autoflush:
            for msg in messages:
                external_message_id = msg.get("id")
                if not external_message_id:
                    continue

                # Handle deleted messages from Delta sync
                # Delta Query returns @removed annotation for deleted items
                sync_metadata = msg.get("_sync_metadata", {})
                is_deleted_from_delta = sync_metadata.get("sync_action") == "DELETED"

                if is_deleted_from_delta:
                    # Mark existing message as deleted
                    existing_stmt = select(MessagingMessage).where(
                        col(MessagingMessage.integration_id) == integration_id,
                        col(MessagingMessage.external_message_id) == external_message_id,
                    )
                    existing_result = await session.execute(existing_stmt)
                    existing = existing_result.scalar_one_or_none()
                    if existing and not existing.is_deleted:
                        existing.is_deleted = True
                        existing.updated_at = datetime.now(UTC)
                        logger.debug(
                            "Marked message as deleted: %s",
                            external_message_id,
                        )
                    continue  # Skip to next message

                # Detect system event messages (user joined, call started, members added, etc.)
                # These are stored for timeline/analytics but filtered out for chat display
                # Detection methods:
                # 1. messageType == "systemEventMessage"
                # 2. eventDetail property exists (indicates system event)
                # 3. body.content contains "<systemEventMessage/>" marker
                msg_type_raw = msg.get("messageType", "message")
                has_event_detail = msg.get("eventDetail") is not None
                body = msg.get("body", {})
                raw_content = body.get("content", "") if isinstance(body, dict) else ""

                is_system_event = (
                    msg_type_raw == "systemEventMessage"
                    or has_event_detail
                    or raw_content.strip() == "<systemEventMessage/>"
                )

                # Check if message already exists
                existing_stmt = select(MessagingMessage).where(
                    col(MessagingMessage.integration_id) == integration_id,
                    col(MessagingMessage.external_message_id) == external_message_id,
                )
                existing_result = await session.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()

                # Extract content type
                content_type = body.get("contentType", "text") if isinstance(body, dict) else "text"

                # Process content based on type:
                # Teams API returns body.content which can be HTML or plain text
                # - content: Always plain text (for search, preview, AI analysis)
                # - formatted_content: Original HTML (for rich display in UI)
                if content_type == "html" and raw_content:
                    # Extract plain text from HTML
                    soup = BeautifulSoup(raw_content, "html.parser")
                    # Replace <br> and </p> with newlines for better formatting
                    for br in soup.find_all("br"):
                        br.replace_with("\n")
                    for p in soup.find_all("p"):
                        p.append("\n")
                    plain_text = soup.get_text(separator=" ").strip()
                    # Clean up multiple spaces/newlines
                    plain_text = re.sub(r"\s+", " ", plain_text).strip()
                    content = plain_text
                    formatted_content = raw_content  # Keep original HTML
                else:
                    # Plain text message
                    content = raw_content
                    formatted_content = None  # No HTML version

                # Extract sender info
                from_info = msg.get("from", {})
                sender_user = from_info.get("user", {}) if isinstance(from_info, dict) else {}
                sender_id = sender_user.get("id") if isinstance(sender_user, dict) else None
                sender_name = (
                    sender_user.get("displayName") if isinstance(sender_user, dict) else None
                )

                # Parse timestamps
                sent_at_str = msg.get("createdDateTime")
                sent_at = (
                    datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
                    if sent_at_str
                    else datetime.now(UTC)
                )

                if existing:
                    # Update existing message
                    existing.content = content
                    existing.formatted_content = formatted_content
                    existing.message_type = (
                        MessageType.SYSTEM_EVENT if is_system_event else MessageType.TEXT
                    )
                    existing.updated_at = datetime.now(UTC)
                else:
                    # Determine message type:
                    # - SYSTEM_EVENT: Call started, members added, etc. (for timeline/analytics)
                    # - TEXT: Regular user messages (for chat display and AI analysis)
                    message_type = MessageType.SYSTEM_EVENT if is_system_event else MessageType.TEXT

                    # For system events, store event details in metadata for future analytics
                    metadata = {
                        "importance": msg.get("importance"),
                        "webUrl": msg.get("webUrl"),
                        "sender_display_name": sender_name,
                    }
                    if is_system_event and msg.get("eventDetail"):
                        # Store event type (e.g., "callEndedEventMessageDetail")
                        event_detail = msg.get("eventDetail", {})
                        metadata["event_type"] = event_detail.get("@odata.type", "").replace(
                            "#microsoft.graph.", ""
                        )
                        metadata["event_detail"] = event_detail

                    new_message = MessagingMessage(
                        organisation_id=self.organisation_id,
                        integration_id=integration_id,
                        channel_id=channel_id,
                        external_message_id=external_message_id,
                        content=content,
                        formatted_content=formatted_content,
                        message_type=message_type,
                        sent_at=sent_at,
                        external_user_id=sender_id,
                        attachments=msg.get("attachments", []),
                        mentions=[m.get("id") for m in msg.get("mentions", []) if m.get("id")],
                        reactions={"items": msg.get("reactions", [])},
                        is_deleted=False,
                        is_bot_message=False,
                        is_important=msg.get("importance") == "high",
                        message_metadata=metadata,
                    )
                    session.add(new_message)
                    stored_count += 1

        await session.commit()
        return stored_count

    async def _update_channel_sync_state_fresh_session(
        self,
        channel_id: UUID,
        delta_link: str | None = None,
    ) -> None:
        """
        Update the channel sync state using a fresh session.

        Acquires its own database session to avoid holding connections
        during long-running API calls.

        Args:
            channel_id: Channel UUID
            delta_link: New deltaLink from Microsoft Graph for next incremental sync
        """
        async for session in acquire_org_async_session(self.organisation_id):
            await self._update_channel_sync_state_internal(session, channel_id, delta_link)
            return

    async def _update_channel_sync_state(
        self,
        channel_id: UUID,
        delta_link: str | None = None,
    ) -> None:
        """
        Update the channel sync state (backward compatible).

        Uses self.db session. For fresh session pattern, use _update_channel_sync_state_fresh_session.

        Args:
            channel_id: Channel UUID
            delta_link: New deltaLink from Microsoft Graph for next incremental sync
        """
        await self._update_channel_sync_state_internal(self.db, channel_id, delta_link)

    async def _update_channel_sync_state_internal(
        self,
        session: AsyncSession,
        channel_id: UUID,
        delta_link: str | None = None,
    ) -> None:
        """
        Internal: Update the channel sync state (timestamp and deltaLink).

        Args:
            session: Database session to use
            channel_id: Channel UUID
            delta_link: New deltaLink from Microsoft Graph for next incremental sync
        """
        channel_stmt = select(MessagingChannel).where(
            col(MessagingChannel.channel_id) == channel_id
        )
        channel_result = await session.execute(channel_stmt)
        channel = channel_result.scalar_one_or_none()
        if channel:
            # Use naive datetime for TIMESTAMP WITHOUT TIME ZONE column
            channel.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
            if delta_link:
                channel.last_sync_cursor = delta_link
            await session.commit()
