"""
Deal channel messages sync service.

This service handles syncing messages from Slack/Teams channels that users have linked to deals.
It follows the existing service patterns in the codebase.

Key features:
- Query deal-linked channels with deduplication by external_channel_id
- Random integration selection for rate limit distribution
- Fetch messages via provider APIs (Teams Graph / Slack Web API)
- Store messages in messaging_messages table

Architecture:
- Service layer handles business logic and database operations
- Provider layer handles external API calls
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
    MessagingChannelIntegrationState,
    MessagingDealLink,
    MessagingMessage,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.messaging.slack import (
    fetch_chat_messages as fetch_slack_chat_messages,
)
from src.workers.integrations_worker.providers.messaging.slack import (
    fetch_messages as fetch_slack_messages,
)
from src.workers.integrations_worker.providers.messaging.slack import (
    split_slack_channel_external_id,
)
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
    Service for syncing messages from deal-linked Slack/Teams channels.

    This service:
    1. Queries MessagingDealLink for user-selected channels
    2. Groups by external_channel_id to avoid duplicate fetches
    3. Randomly selects integration for load distribution
    4. Fetches messages via provider APIs
    5. Stores in messaging_messages table

    Session Management:
    - Uses fresh session pattern to avoid holding connections during API calls
    - Sessions are acquired only when DB operations are needed
    - Sessions are released before external API calls
    """

    def __init__(self, session: AsyncSession | None, organisation_id: UUID) -> None:
        """Initialize with session for backward compatibility.

        Note: The session is stored but fresh sessions are acquired for each
        operation to follow the fresh session pattern.
        """
        self.db = session
        self.organisation_id = organisation_id

    def _require_session(self) -> AsyncSession:
        """Return the configured session or fail fast for legacy code paths."""
        if self.db is None:
            raise RuntimeError("Database session is required for this operation")
        return self.db

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
        # Build base query — join through integration state (org-scoped channels)
        stmt = (
            select(
                col(MessagingChannel.channel_id),
                col(MessagingChannel.external_channel_id),
                col(MessagingChannelIntegrationState.integration_id),
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
                MessagingChannelIntegrationState,
                col(MessagingChannelIntegrationState.channel_id)
                == col(MessagingChannel.channel_id),
            )
            .join(
                KnowledgeIntegration,
                col(MessagingChannelIntegrationState.integration_id)
                == col(KnowledgeIntegration.integration_id),
            )
            .where(
                col(MessagingDealLink.organisation_id) == self.organisation_id,
                col(MessagingDealLink.channel_id).isnot(None),  # Channel-level links only
                col(MessagingDealLink.is_deleted).is_(False),
                col(MessagingChannel.is_deleted).is_(False),
                col(MessagingChannelIntegrationState.is_deleted).is_(False),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
                col(KnowledgeIntegration.integration_type).in_(
                    [IntegrationType.TEAMS, IntegrationType.SLACK]
                ),
            )
            .distinct(col(MessagingChannel.external_channel_id))
            .order_by(
                col(MessagingChannel.external_channel_id),
                col(MessagingChannel.last_synced_at).desc().nulls_last(),
            )
        )

        # Add integration filter if specified
        if integration_id:
            stmt = stmt.where(
                col(MessagingChannelIntegrationState.integration_id) == integration_id
            )

        session = self._require_session()
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
        # Build base query — join through integration state (org-scoped channels)
        stmt = (
            select(
                col(MessagingChannel.channel_id),
                col(MessagingChannel.external_channel_id),
                col(MessagingChannelIntegrationState.integration_id),
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
                MessagingChannelIntegrationState,
                col(MessagingChannelIntegrationState.channel_id)
                == col(MessagingChannel.channel_id),
            )
            .join(
                KnowledgeIntegration,
                col(MessagingChannelIntegrationState.integration_id)
                == col(KnowledgeIntegration.integration_id),
            )
            .where(
                col(MessagingDealLink.organisation_id) == self.organisation_id,
                col(MessagingDealLink.channel_id).isnot(None),  # Channel-level links only
                col(MessagingDealLink.is_deleted).is_(False),
                col(MessagingChannel.is_deleted).is_(False),
                col(MessagingChannelIntegrationState.is_deleted).is_(False),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
                col(KnowledgeIntegration.integration_type).in_(
                    [IntegrationType.TEAMS, IntegrationType.SLACK]
                ),
            )
            .distinct(col(MessagingChannel.external_channel_id))
            .order_by(
                col(MessagingChannel.external_channel_id),
                col(MessagingChannel.last_synced_at).desc().nulls_last(),
            )
        )

        # Add integration filter if specified
        if integration_id:
            stmt = stmt.where(
                col(MessagingChannelIntegrationState.integration_id) == integration_id
            )

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
            token_info: IntegrationTokenInfo | None = None
            async for session in acquire_org_async_session(self.organisation_id):
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == channel_info.integration_id
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()
                if integration:
                    connection_id = integration.connection_id
                    token_info = IntegrationTokenInfo.from_integration(
                        integration, self.organisation_id
                    )
            # Session is now released

            if not connection_id or not token_info:
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
                token_info=token_info,
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
            provider = str(channel_info.channel_metadata.get("provider") or "teams").lower()
            messages_stored = await self._store_messages_fresh_session(
                channel_id=channel_info.channel_id,
                messages=messages,
                provider=provider,
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
        1. Fresh session: Get integration connection_id, extract token info, release
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
            # Phase 1: Get integration connection_id (fresh session, release before API)
            # ─────────────────────────────────────────────────────────────────
            connection_id: str | None = None
            token_info: IntegrationTokenInfo | None = None

            async for session in acquire_org_async_session(self.organisation_id):
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == channel_info.integration_id
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()
                if integration and integration.connection_id:
                    connection_id = integration.connection_id
                    token_info = IntegrationTokenInfo.from_integration(
                        integration, self.organisation_id
                    )
            # Session released

            if not connection_id or not token_info:
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
                token_info=token_info,
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
            provider = str(channel_info.channel_metadata.get("provider") or "teams").lower()
            messages_stored = await self._store_messages_fresh_session(
                channel_id=channel_info.channel_id,
                messages=messages,
                provider=provider,
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
        session = self._require_session()
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _fetch_messages_with_connection_id(
        self,
        channel_info: DealLinkedChannelInfo,
        connection_id: str,
        token_info: IntegrationTokenInfo,
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
        provider = str(channel_info.channel_metadata.get("provider") or "teams").lower()
        channel_type = channel_info.channel_type

        if provider == "slack":
            _, raw_channel_id = split_slack_channel_external_id(channel_info.external_channel_id)
            is_initial = channel_info.last_sync_cursor is None

            if channel_type in (ChannelType.PUBLIC, ChannelType.PRIVATE):
                return await fetch_slack_messages(
                    token_info=token_info,
                    connection_id=connection_id,
                    channel_id=raw_channel_id,
                    is_initial=is_initial,
                    last_synced_at=channel_info.last_synced_at,
                    delta_link=delta_link,
                )

            if channel_type == ChannelType.GROUP_MESSAGE:
                return await fetch_slack_chat_messages(
                    token_info=token_info,
                    connection_id=connection_id,
                    chat_id=raw_channel_id,
                    is_initial=is_initial,
                    last_synced_at=channel_info.last_synced_at,
                    delta_link=delta_link,
                )

            if channel_type == ChannelType.DIRECT_MESSAGE:
                logger.debug("Skipping Slack 1:1 DM channel %s", channel_info.channel_id)
                return FetchMessagesResult(messages=[], delta_link=None)

            logger.warning(
                "Unknown Slack channel type %s for channel %s",
                channel_type,
                channel_info.channel_id,
            )
            return FetchMessagesResult(messages=[], delta_link=None)

        if channel_type in (ChannelType.PUBLIC, ChannelType.PRIVATE):
            # Team channel - needs team_id from metadata
            is_initial = delta_link is None
            team_id = channel_info.channel_metadata.get("team_id")
            if not team_id:
                raise ValueError(
                    f"Missing team_id in channel_metadata for channel {channel_info.channel_id}"
                )

            return await fetch_messages(
                token_info=token_info,
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
                token_info=token_info,
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
        Fetch messages from the provider API via the provider layer.

        Args:
            channel_info: Channel information
            integration: Integration with credentials
            delta_link: Optional stored deltaLink from previous sync

        Returns:
            FetchMessagesResult with messages and new deltaLink for next sync
        """
        token_info = IntegrationTokenInfo.from_integration(integration, self.organisation_id)
        return await self._fetch_messages_with_connection_id(
            channel_info=channel_info,
            connection_id=integration.connection_id,  # type: ignore
            token_info=token_info,
            delta_link=delta_link,
        )

    async def _store_messages_fresh_session(
        self,
        channel_id: UUID,
        messages: list[dict[str, Any]],
        provider: str,
    ) -> int:
        """
        Store fetched messages in the database using a fresh session.

        Acquires its own database session to avoid holding connections
        during long-running API calls.

        Args:
            channel_id: Channel UUID (our internal ID)
            messages: List of message dicts from the provider API
            provider: Source provider (teams or slack)

        Returns:
            Number of new messages stored
        """
        async for session in acquire_org_async_session(self.organisation_id):
            return await self._store_messages_internal(session, channel_id, messages, provider)
        return 0

    async def _store_messages(
        self,
        channel_id: UUID,
        messages: list[dict[str, Any]],
        provider: str,
    ) -> int:
        """
        Store fetched messages in the database (backward compatible).

        Uses self.db session. For fresh session pattern, use _store_messages_fresh_session.

        Args:
            channel_id: Channel UUID (our internal ID)
            messages: List of message dicts from the provider API
            provider: Source provider (teams or slack)

        Returns:
            Number of new messages stored
        """
        session = self._require_session()
        return await self._store_messages_internal(session, channel_id, messages, provider)

    async def _store_messages_internal(
        self,
        session: AsyncSession,
        channel_id: UUID,
        messages: list[dict[str, Any]],
        provider: str,
    ) -> int:
        """
        Internal: Store fetched messages in the database.

        Uses upsert logic to avoid duplicates (based on channel_id + external_message_id).

        Args:
            session: Database session to use
            channel_id: Channel UUID (our internal ID)
            messages: List of message dicts from the provider API
            provider: Source provider (teams or slack)

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
                        col(MessagingMessage.channel_id) == channel_id,
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

                # Check if message already exists (unique by channel + external ID)
                existing_stmt = select(MessagingMessage).where(
                    col(MessagingMessage.channel_id) == channel_id,
                    col(MessagingMessage.external_message_id) == external_message_id,
                )
                existing_result = await session.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()
                extracted = self._extract_message_fields(msg, provider)

                if existing:
                    existing.external_user_id = extracted["external_user_id"]
                    existing.author_display_name = extracted["author_display_name"]
                    existing.author_email = extracted["author_email"]
                    existing.author_email_normalized = extracted["author_email_normalized"]
                    existing.thread_id = extracted["thread_id"]
                    existing.content = extracted["content"]
                    existing.formatted_content = extracted["formatted_content"]
                    existing.message_type = extracted["message_type"]
                    existing.sent_at = extracted["sent_at"]
                    existing.edited_at = extracted["edited_at"]
                    existing.attachments = extracted["attachments"]
                    existing.mentions = extracted["mentions"]
                    existing.reactions = extracted["reactions"]
                    existing.is_deleted = False
                    existing.is_bot_message = extracted["is_bot_message"]
                    existing.is_important = extracted["is_important"]
                    existing.message_metadata = extracted["message_metadata"]
                    existing.synced_at = datetime.now(UTC)
                    existing.last_seen_at = datetime.now(UTC)
                    existing.updated_at = datetime.now(UTC)
                else:
                    new_message = MessagingMessage(
                        organisation_id=self.organisation_id,
                        channel_id=channel_id,
                        external_message_id=external_message_id,
                        external_user_id=extracted["external_user_id"],
                        author_display_name=extracted["author_display_name"],
                        author_email=extracted["author_email"],
                        author_email_normalized=extracted["author_email_normalized"],
                        thread_id=extracted["thread_id"],
                        content=extracted["content"],
                        formatted_content=extracted["formatted_content"],
                        message_type=extracted["message_type"],
                        sent_at=extracted["sent_at"],
                        edited_at=extracted["edited_at"],
                        attachments=extracted["attachments"],
                        mentions=extracted["mentions"],
                        reactions=extracted["reactions"],
                        is_deleted=False,
                        is_bot_message=extracted["is_bot_message"],
                        is_important=extracted["is_important"],
                        message_metadata=extracted["message_metadata"],
                    )
                    session.add(new_message)
                    stored_count += 1

        await session.commit()
        return stored_count

    def _extract_message_fields(
        self,
        msg: dict[str, Any],
        provider: str,
    ) -> dict[str, Any]:
        """Normalize provider payloads into MessagingMessage fields."""
        if provider == "slack":
            return self._extract_slack_message_fields(msg)
        return self._extract_teams_message_fields(msg)

    def _extract_teams_message_fields(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Extract normalized message fields from a Teams message payload."""
        body = msg.get("body", {})
        if not isinstance(body, dict):
            body = {}

        raw_content = body.get("content", "")
        if not isinstance(raw_content, str):
            raw_content = str(raw_content or "")

        content_type = body.get("contentType", "text")
        if content_type == "html" and raw_content:
            soup = BeautifulSoup(raw_content, "html.parser")
            for br in soup.find_all("br"):
                br.replace_with("\n")
            for p in soup.find_all("p"):
                p.append("\n")
            plain_text = soup.get_text(separator=" ").strip()
            content = re.sub(r"\s+", " ", plain_text).strip()
            formatted_content = raw_content
        else:
            content = raw_content
            formatted_content = None

        from_info = msg.get("from", {})
        sender_user = from_info.get("user", {}) if isinstance(from_info, dict) else {}
        sender_id = sender_user.get("id") if isinstance(sender_user, dict) else None
        sender_name = sender_user.get("displayName") if isinstance(sender_user, dict) else None
        sender_email = sender_user.get("email") if isinstance(sender_user, dict) else None

        sent_at = self._parse_iso_datetime(msg.get("createdDateTime")) or datetime.now(UTC)
        edited_at = self._parse_iso_datetime(msg.get("lastEditedDateTime"))

        msg_type_raw = msg.get("messageType", "message")
        has_event_detail = msg.get("eventDetail") is not None
        is_system_event = (
            msg_type_raw == "systemEventMessage"
            or has_event_detail
            or raw_content.strip() == "<systemEventMessage/>"
        )

        metadata: dict[str, Any] = {
            "provider": "teams",
            "importance": msg.get("importance"),
            "webUrl": msg.get("webUrl"),
            "sender_display_name": sender_name,
            "message_type": msg_type_raw,
            "subject": msg.get("subject"),
            "locale": msg.get("locale"),
            "team_id": msg.get("teamId"),
            "channel_id": msg.get("channelId"),
            "chat_id": msg.get("chatId"),
        }
        if is_system_event and msg.get("eventDetail"):
            event_detail = msg.get("eventDetail", {})
            metadata["event_type"] = event_detail.get("@odata.type", "").replace(
                "#microsoft.graph.", ""
            )
            metadata["event_detail"] = event_detail

        mentions = []
        for mention in msg.get("mentions", []):
            if isinstance(mention, dict) and mention.get("id"):
                mentions.append(mention["id"])

        return {
            "external_user_id": sender_id,
            "author_display_name": sender_name,
            "author_email": sender_email,
            "author_email_normalized": sender_email.lower().strip() if sender_email else None,
            "thread_id": msg.get("replyToId"),
            "content": content,
            "formatted_content": formatted_content,
            "message_type": MessageType.SYSTEM_EVENT if is_system_event else MessageType.TEXT,
            "sent_at": sent_at,
            "edited_at": edited_at,
            "attachments": msg.get("attachments", []),
            "mentions": mentions,
            "reactions": {"items": msg.get("reactions", [])},
            "is_bot_message": False,
            "is_important": msg.get("importance") == "high",
            "message_metadata": metadata,
        }

    def _extract_slack_message_fields(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Extract normalized message fields from a Slack message payload."""
        user_profile = msg.get("user_profile", {})
        if not isinstance(user_profile, dict):
            user_profile = {}

        text = msg.get("text")
        content = text if isinstance(text, str) else None

        subtype = str(msg.get("subtype") or "")
        files = msg.get("files", [])
        if not isinstance(files, list):
            files = []

        reactions = msg.get("reactions", [])
        if not isinstance(reactions, list):
            reactions = []

        edited = msg.get("edited", {})
        if not isinstance(edited, dict):
            edited = {}

        system_subtypes = {
            "channel_join",
            "channel_leave",
            "channel_topic",
            "channel_purpose",
            "channel_name",
            "channel_archive",
            "channel_unarchive",
            "group_join",
            "group_leave",
            "group_topic",
            "group_purpose",
            "group_name",
            "group_archive",
            "group_unarchive",
            "thread_broadcast",
        }

        message_type = MessageType.TEXT
        if files:
            message_type = MessageType.FILE
        elif subtype in system_subtypes:
            message_type = MessageType.SYSTEM_EVENT

        sender_email = user_profile.get("email")
        sender_name = user_profile.get("displayName") or user_profile.get("name")
        thread_ts = msg.get("thread_ts")

        return {
            "external_user_id": msg.get("user"),
            "author_display_name": sender_name,
            "author_email": sender_email,
            "author_email_normalized": sender_email.lower().strip() if sender_email else None,
            "thread_id": thread_ts if thread_ts else None,
            "content": content,
            "formatted_content": None,
            "message_type": message_type,
            "sent_at": self._parse_slack_ts(msg.get("ts")) or datetime.now(UTC),
            "edited_at": self._parse_slack_ts(edited.get("ts")),
            "attachments": files,
            "mentions": self._extract_slack_mentions(content),
            "reactions": {"items": reactions},
            "is_bot_message": bool(msg.get("bot_id") or subtype == "bot_message"),
            "is_important": bool(msg.get("pinned_to")),
            "message_metadata": {
                "provider": "slack",
                "subtype": subtype or None,
                "channel_id": msg.get("channel"),
                "slack_channel_id": msg.get("slack_channel_id"),
                "reply_count": msg.get("reply_count"),
                "reply_users_count": msg.get("reply_users_count"),
                "author_display_name": sender_name,
                "author_email": sender_email,
            },
        }

    @staticmethod
    def _parse_iso_datetime(value: Any) -> datetime | None:
        """Parse an ISO timestamp from Teams payloads."""
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _parse_slack_ts(value: Any) -> datetime | None:
        """Parse a Slack timestamp into a UTC datetime."""
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(float(str(value)), tz=UTC)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _extract_slack_mentions(text: str | None) -> list[str]:
        """Extract Slack user mentions from mrkdwn text."""
        if not text:
            return []
        return re.findall(r"<@([A-Z0-9]+)>", text)

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
        session = self._require_session()
        await self._update_channel_sync_state_internal(session, channel_id, delta_link)

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
