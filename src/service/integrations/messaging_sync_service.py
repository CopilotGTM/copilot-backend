"""
Messaging sync service for Teams/Slack integration.

This service writes to:
1. messaging_channels - Channel/conversation records
2. messaging_messages - Individual message records
3. messaging_deal_links - Channel-to-deal mappings
4. integration_sync_history - Sync tracking
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.domain.models.organisation_tables.sync.messaging_models import (
    ChannelType,
    MessageType,
    MessagingChannel,
    MessagingMessage,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def datetime_now_naive() -> datetime:
    """Return current UTC timestamp without timezone info for naive columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class MessagingSyncService:
    """
    Service to sync messaging channels and messages from Teams/Slack.

    Scope: This service ONLY modifies messaging-specific tables.
    - messaging_channels
    - messaging_messages
    - messaging_deal_links (for channel-to-deal mappings)
    - integration_sync_history
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id
        # Cache external_channel_id -> channel primary key
        self._channel_cache: dict[str, UUID] = {}

    async def sync_channels(
        self,
        *,
        integration: KnowledgeIntegration,
        channels: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """
        Sync messaging channels from external provider.

        Tables Modified:
        - messaging_channels (primary)
        - integration_sync_history (tracking)
        - knowledge_integrations (last_synced_at update only)

        Args:
            integration: The integration record
            channels: List of channel dictionaries from provider
            provider: Provider name (teams, slack, etc.)

        Returns:
            Dictionary with sync statistics
        """
        sync_history = await self._create_sync_history(integration.integration_id)

        try:
            channel_list = list(channels)
            logger.info(
                "sync_channels started: integration=%s provider=%s channel_count=%d",
                integration.integration_id,
                provider,
                len(channel_list),
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0

            for idx, channel_data in enumerate(channel_list):
                try:
                    external_id = self._extract_channel_id(channel_data, provider)
                    if not external_id:
                        logger.debug("Channel %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Check if channel already exists
                    existing_channel = await self._get_channel_by_external_id(
                        integration.integration_id, external_id
                    )

                    if existing_channel:
                        # Update existing channel
                        await self._update_channel(existing_channel, channel_data, provider)
                        updated_count += 1
                        logger.debug(
                            "Updated channel: external_id=%s name=%s",
                            external_id,
                            channel_data.get("displayName"),
                        )
                    else:
                        # Create new channel
                        await self._create_channel(integration, channel_data, provider, external_id)
                        created_count += 1
                        logger.debug(
                            "Created channel: external_id=%s name=%s",
                            external_id,
                            channel_data.get("displayName"),
                        )

                    records_processed += 1

                    # Commit after each channel
                    await self.db.commit()

                except Exception as e:
                    logger.error(
                        "Failed to sync channel %d (external_id=%s): %s",
                        idx,
                        channel_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
                    if self.db.in_transaction():
                        await self.db.rollback()
                    records_failed += 1

            # Final commit
            await self.db.commit()

            # Update sync history
            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "provider": provider,
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Channel sync completed: integration=%s created=%d updated=%d failed=%d",
                integration.integration_id,
                created_count,
                updated_count,
                records_failed,
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
            }

        except Exception as e:
            logger.error(
                "Channel sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(channel_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            # Update integration sync status
            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def sync_messages(
        self,
        *,
        integration: KnowledgeIntegration,
        messages: Iterable[dict[str, Any]],
        provider: str,
        channel_id: UUID | None = None,
    ) -> dict[str, Any]:
        """
        Sync messaging messages from external provider.

        Only syncs messages for channels where is_monitored=true.

        Tables Modified:
        - messaging_messages (primary)
        - integration_sync_history (tracking)
        - knowledge_integrations (last_synced_at update only)

        Args:
            integration: The integration record
            messages: List of message dictionaries from provider
            provider: Provider name (teams, slack, etc.)
            channel_id: Optional channel UUID for channel-specific sync

        Returns:
            Dictionary with sync statistics
        """
        sync_history = await self._create_sync_history(integration.integration_id)

        try:
            message_list = list(messages)
            logger.info(
                "sync_messages started: integration=%s provider=%s message_count=%d channel_id=%s",
                integration.integration_id,
                provider,
                len(message_list),
                channel_id,
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0

            for idx, message_data in enumerate(message_list):
                try:
                    external_id = self._extract_message_id(message_data, provider)
                    if not external_id:
                        logger.debug("Message %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Check if message already exists
                    existing_message = await self._get_message_by_external_id(
                        integration.integration_id, external_id
                    )

                    if existing_message:
                        # Update existing message
                        await self._update_message(existing_message, message_data, provider)
                        updated_count += 1
                        logger.debug(
                            "Updated message: external_id=%s",
                            external_id,
                        )
                    else:
                        # Create new message
                        await self._create_message(
                            integration,
                            message_data,
                            provider,
                            external_id,
                            channel_id,
                        )
                        created_count += 1
                        logger.debug(
                            "Created message: external_id=%s",
                            external_id,
                        )

                    records_processed += 1

                    # Commit after each message
                    await self.db.commit()

                except Exception as e:
                    logger.error(
                        "Failed to sync message %d (external_id=%s): %s",
                        idx,
                        message_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
                    if self.db.in_transaction():
                        await self.db.rollback()
                    records_failed += 1

            # Final commit
            await self.db.commit()

            # Update sync history
            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "provider": provider,
                    "channel_id": str(channel_id) if channel_id else None,
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Message sync completed: integration=%s created=%d updated=%d failed=%d",
                integration.integration_id,
                created_count,
                updated_count,
                records_failed,
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
            }

        except Exception as e:
            logger.error(
                "Message sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(message_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            # Update integration sync status
            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def get_monitored_channels(self, integration_id: UUID) -> list[MessagingChannel]:
        """Get all channels that should be actively synced (is_monitored=true)."""
        stmt = select(MessagingChannel).where(
            MessagingChannel.integration_id == integration_id,  # type: ignore[arg-type]
            MessagingChannel.is_monitored == "true",  # type: ignore[arg-type]
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _create_sync_history(self, integration_id: UUID) -> IntegrationSyncHistory:
        """Create a new sync history record."""
        sync_history = IntegrationSyncHistory(
            integration_id=integration_id,
            started_at=datetime_now_naive(),
            status=SyncStatus.IN_PROGRESS,
        )
        self.db.add(sync_history)
        await self.db.commit()
        await self.db.refresh(sync_history)

        logger.info("Created sync history: sync_id=%s", sync_history.sync_id)
        return sync_history

    async def _complete_sync_history(
        self,
        sync_history: IntegrationSyncHistory,
        status: SyncStatus,
        records_processed: int,
        records_failed: int,
        metadata: dict[str, Any] | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Update sync history with completion details."""
        sync_history.completed_at = datetime_now_naive()
        sync_history.status = status
        sync_history.records_processed = records_processed
        sync_history.records_failed = records_failed

        if metadata:
            sync_history.sync_metadata = metadata

        if error_details:
            sync_history.error_details = error_details

        self.db.add(sync_history)
        await self.db.commit()

        logger.info(
            "Updated sync history: sync_id=%s status=%s processed=%d failed=%d",
            sync_history.sync_id,
            status.value,
            records_processed,
            records_failed,
        )

    async def _get_channel_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> MessagingChannel | None:
        """Get existing messaging channel by external_id."""
        stmt = select(MessagingChannel).where(
            MessagingChannel.integration_id == integration_id,  # type: ignore[arg-type]
            MessagingChannel.external_channel_id == external_id,  # type: ignore[arg-type]
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _create_channel(
        self,
        integration: KnowledgeIntegration,
        channel_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> MessagingChannel:
        """Create a new messaging channel."""
        channel = MessagingChannel(
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            external_channel_id=external_id,
            synced_at=datetime.now(UTC),
            **self._extract_channel_fields(channel_data, provider),
        )

        self.db.add(channel)
        await self.db.flush()
        await self.db.refresh(channel)

        # Cache for later lookups
        self._channel_cache[external_id] = channel.channel_id

        return channel

    async def _update_channel(
        self, channel: MessagingChannel, channel_data: dict[str, Any], provider: str
    ) -> None:
        """Update existing messaging channel."""
        # Update fields
        fields = self._extract_channel_fields(channel_data, provider)
        for key, value in fields.items():
            setattr(channel, key, value)

        # Update sync timestamp
        channel.synced_at = datetime.now(UTC)

        self.db.add(channel)
        await self.db.flush()

    async def _get_message_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> MessagingMessage | None:
        """Get existing messaging message by external_id."""
        stmt = select(MessagingMessage).where(
            MessagingMessage.integration_id == integration_id,  # type: ignore[arg-type]
            MessagingMessage.external_message_id == external_id,  # type: ignore[arg-type]
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _create_message(
        self,
        integration: KnowledgeIntegration,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        channel_id: UUID | None,
    ) -> MessagingMessage:
        """Create a new messaging message."""
        resolved_channel_id = await self._ensure_channel_id(
            integration.integration_id,
            message_data,
            provider,
            channel_id,
            external_id,
        )

        message = MessagingMessage(
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            external_message_id=external_id,
            channel_id=resolved_channel_id,
            synced_at=datetime.now(UTC),
            **self._extract_message_fields(message_data, provider),
        )

        self.db.add(message)
        await self.db.flush()
        return message

    async def _update_message(
        self, message: MessagingMessage, message_data: dict[str, Any], provider: str
    ) -> None:
        """Update existing messaging message."""
        # Update fields
        fields = self._extract_message_fields(message_data, provider)
        for key, value in fields.items():
            setattr(message, key, value)

        # Update sync timestamp
        message.synced_at = datetime.now(UTC)

        self.db.add(message)
        await self.db.flush()

    async def _ensure_channel_id(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        existing_channel_id: UUID | None,
        external_message_id: str,
    ) -> UUID:
        """Ensure we have a channel UUID for this message, resolving from external ID if needed."""
        if existing_channel_id:
            return existing_channel_id

        external_channel_id = self._extract_external_channel_id_from_message(message_data, provider)
        resolved = await self._resolve_channel_id(integration_id, external_channel_id)
        if resolved:
            return resolved

        raise ValueError(
            "Unable to resolve channel for message "
            f"{external_message_id} (provider={provider}, external_channel_id={external_channel_id})"
        )

    async def _resolve_channel_id(
        self,
        integration_id: UUID,
        external_channel_id: str | None,
    ) -> UUID | None:
        """Resolve channel UUID using provider-specific identifiers."""
        if not external_channel_id:
            return None

        cached_id = self._channel_cache.get(external_channel_id)
        if cached_id:
            return cached_id

        channel = await self._get_channel_by_external_id(integration_id, external_channel_id)
        if channel:
            self._channel_cache[external_channel_id] = channel.channel_id
            return channel.channel_id
        return None

    @staticmethod
    def _extract_channel_id(channel_data: dict[str, Any], _provider: str) -> str | None:
        """Extract external ID from channel data."""
        return channel_data.get("id")

    @staticmethod
    def _extract_message_id(message_data: dict[str, Any], _provider: str) -> str | None:
        """Extract external ID from message data."""
        return message_data.get("id")

    @staticmethod
    def _extract_external_channel_id_from_message(
        message_data: dict[str, Any],
        provider: str,
    ) -> str | None:
        """Extract provider-specific channel identifier from message payload."""
        if provider in ("microsoft", "teams"):
            return message_data.get("channelId")
        if provider == "slack":
            return message_data.get("channel")
        return message_data.get("channelId")

    @classmethod
    def _extract_channel_fields(cls, channel_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract channel fields from provider data."""
        if provider in ("microsoft", "teams"):
            return cls._extract_teams_channel_fields(channel_data)
        return cls._extract_generic_channel_fields(channel_data)

    @classmethod
    def _extract_teams_channel_fields(cls, channel_data: dict[str, Any]) -> dict[str, Any]:
        """Extract channel fields from Microsoft Teams data."""
        membership_type = channel_data.get("membershipType", "standard")

        # Map Teams membership types to our ChannelType
        channel_type = ChannelType.PUBLIC
        if membership_type == "private":
            channel_type = ChannelType.PRIVATE

        return {
            "channel_name": channel_data.get("displayName", "Unnamed Channel"),
            "channel_type": channel_type,
            "channel_description": channel_data.get("description"),
            "is_monitored": "false",  # Default; user configures later (stored as string in DB)
            "participant_count": 0,  # Would need separate API call to get member count
            "is_archived": "false",  # Teams doesn't expose this in basic channel info
            "channel_metadata": {
                "team_id": channel_data.get("teamId"),
                "team_name": channel_data.get("teamName"),
                "email": channel_data.get("email"),
                "web_url": channel_data.get("webUrl"),
                "membership_type": membership_type,
                "created_date_time": channel_data.get("createdDateTime"),
            },
        }

    @staticmethod
    def _extract_generic_channel_fields(channel_data: dict[str, Any]) -> dict[str, Any]:
        """Extract channel fields from generic provider data."""
        return {
            "channel_name": channel_data.get("name") or channel_data.get("displayName", "Unnamed"),
            "channel_type": ChannelType.PUBLIC,
            "channel_description": None,
            "is_monitored": "false",  # Stored as string in DB
            "participant_count": 0,
            "is_archived": "false",
            "channel_metadata": {},
        }

    @classmethod
    def _extract_message_fields(cls, message_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract message fields from provider data."""
        if provider in ("microsoft", "teams"):
            return cls._extract_teams_message_fields(message_data)
        return cls._extract_generic_message_fields(message_data)

    @classmethod
    def _extract_teams_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from Microsoft Teams data."""
        from_user = message_data.get("from", {}).get("user", {})
        body = message_data.get("body", {})

        # Determine message type
        message_type = MessageType.TEXT
        msg_type_str = message_data.get("messageType", "message")
        if msg_type_str == "systemEventMessage":
            message_type = MessageType.SYSTEM_EVENT

        return {
            "external_user_id": from_user.get("id"),
            "thread_id": message_data.get("replyToId"),  # Teams thread identifier
            "message_type": message_type,
            "content": body.get("content"),
            "formatted_content": body.get("content") if body.get("contentType") == "html" else None,
            "sent_at": cls._parse_iso_datetime(message_data.get("createdDateTime")),
            "edited_at": cls._parse_iso_datetime(message_data.get("lastEditedDateTime")),
            "reactions": message_data.get("reactions", {}),
            "attachments": message_data.get("attachments", []),
            "mentions": [
                m.get("mentioned", {}).get("user", {}).get("id")
                for m in message_data.get("mentions", [])
            ],
            "is_deleted": "true" if message_data.get("deletedDateTime") else "false",
            "is_bot_message": "false",  # Would need to check user identity type
            "is_important": "true" if message_data.get("importance") == "urgent" else "false",
            "message_metadata": {
                "importance": message_data.get("importance"),
                "message_type": msg_type_str,
                "subject": message_data.get("subject"),
                "locale": message_data.get("locale"),
                "team_id": message_data.get("teamId"),
                "channel_id": message_data.get("channelId"),
            },
        }

    @staticmethod
    def _extract_generic_message_fields(message_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from generic provider data."""
        return {
            "external_user_id": message_data.get("userId"),
            "thread_id": message_data.get("threadId"),
            "message_type": MessageType.TEXT,
            "content": message_data.get("text"),
            "formatted_content": None,
            "sent_at": datetime.now(UTC),
            "edited_at": None,
            "reactions": {},
            "attachments": [],
            "mentions": [],
            "is_deleted": "false",
            "is_bot_message": "false",
            "is_important": "false",
            "message_metadata": {},
        }

    @staticmethod
    def _parse_iso_datetime(value: str | None) -> datetime | None:
        """Parse ISO datetime string to datetime object."""
        if not value:
            return None
        try:
            # Handle 'Z' suffix and convert to UTC
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except (ValueError, AttributeError):
            return None

    async def link_channel_to_deal(
        self,
        *,
        channel_id: UUID,
        deal_id: UUID,
        integration_id: UUID,
        linked_by: UUID | None = None,
        detection_method: str = "manual",
        confidence_score: float = 1.0,
    ) -> dict[str, Any]:
        """
        Link a channel to a deal and enable monitoring.

        This method:
        1. Creates a MessagingDealLink record
        2. Sets is_monitored="true" on the channel
        3. Returns the link details

        Args:
            channel_id: Internal channel UUID
            deal_id: Internal deal UUID
            integration_id: Integration UUID
            linked_by: User who created the link (optional)
            detection_method: How link was created (manual, auto, etc.)
            confidence_score: Confidence for auto-detected links (0.0-1.0)

        Returns:
            Dictionary with link_id and status
        """
        from src.domain.models.organisation_tables.sync.messaging_models import MessagingDealLink

        try:
            # Check if link already exists
            stmt = select(MessagingDealLink).where(
                MessagingDealLink.channel_id == channel_id,  # type: ignore[arg-type]
                MessagingDealLink.deal_id == deal_id,  # type: ignore[arg-type]
                MessagingDealLink.is_deleted == False,  # noqa: E712
            )
            result = await self.db.execute(stmt)
            existing_link = result.scalars().first()

            if existing_link:
                logger.info(
                    "Channel-to-deal link already exists: channel_id=%s deal_id=%s",
                    channel_id,
                    deal_id,
                )
                return {
                    "status": "exists",
                    "link_id": str(existing_link.link_id),
                    "channel_id": str(channel_id),
                    "deal_id": str(deal_id),
                }

            # Create new link
            new_link = MessagingDealLink(
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                channel_id=channel_id,
                deal_id=deal_id,
                detection_method=detection_method,
                confidence_score=confidence_score,
                linked_by=linked_by,
                linked_at=datetime.now(UTC),
                is_verified="true" if detection_method == "manual" else "false",
                verified_by=linked_by if detection_method == "manual" else None,
                verified_at=datetime.now(UTC) if detection_method == "manual" else None,
                is_deleted=False,
                link_metadata={},
            )

            self.db.add(new_link)

            # Enable monitoring on the channel
            stmt = select(MessagingChannel).where(
                MessagingChannel.channel_id == channel_id  # type: ignore[arg-type]
            )
            result = await self.db.execute(stmt)
            channel = result.scalars().first()

            if channel:
                channel.is_monitored = "true"
                self.db.add(channel)
                logger.info(
                    "Enabled monitoring for channel: channel_id=%s (linked to deal=%s)",
                    channel_id,
                    deal_id,
                )

            await self.db.commit()

            return {
                "status": "created",
                "link_id": str(new_link.link_id),
                "channel_id": str(channel_id),
                "deal_id": str(deal_id),
                "is_monitored": True,
            }

        except Exception as e:
            await self.db.rollback()
            logger.exception(
                "Failed to link channel to deal: channel_id=%s deal_id=%s error=%s",
                channel_id,
                deal_id,
                str(e),
            )
            raise

    async def get_channels_for_deal(
        self,
        *,
        deal_id: UUID,
        integration_id: UUID,
    ) -> list[dict[str, Any]]:
        """
        Get all channels linked to a specific deal.

        Args:
            deal_id: Internal deal UUID
            integration_id: Integration UUID

        Returns:
            List of channel dictionaries with link metadata
        """
        from src.domain.models.organisation_tables.sync.messaging_models import MessagingDealLink

        stmt = (
            select(MessagingChannel, MessagingDealLink)
            .join(
                MessagingDealLink,
                MessagingChannel.channel_id == MessagingDealLink.channel_id,
            )
            .where(
                MessagingDealLink.deal_id == deal_id,  # type: ignore[arg-type]
                MessagingDealLink.integration_id == integration_id,  # type: ignore[arg-type]
                MessagingDealLink.is_deleted == False,  # noqa: E712
                MessagingChannel.is_deleted == False,  # noqa: E712
            )
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        channels = []
        for channel, link in rows:
            channels.append(
                {
                    "channel_id": str(channel.channel_id),
                    "external_channel_id": channel.external_channel_id,
                    "channel_name": channel.channel_name,
                    "channel_type": channel.channel_type,
                    "is_monitored": channel.is_monitored == "true",
                    "participant_count": channel.participant_count,
                    "last_activity_at": channel.last_activity_at,
                    "link_id": str(link.link_id),
                    "detection_method": link.detection_method,
                    "confidence_score": link.confidence_score,
                    "linked_at": link.linked_at,
                    "is_verified": link.is_verified == "true",
                }
            )

        return channels

    async def get_all_channels_for_integration(
        self,
        *,
        integration_id: UUID,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get all channels for an integration (for channel selection UI).

        Args:
            integration_id: Integration UUID
            include_archived: Whether to include archived channels

        Returns:
            List of all channel dictionaries
        """
        stmt = select(MessagingChannel).where(
            MessagingChannel.integration_id == integration_id,  # type: ignore[arg-type]
            MessagingChannel.is_deleted == False,  # noqa: E712
        )

        if not include_archived:
            stmt = stmt.where(
                MessagingChannel.is_archived == "false"  # type: ignore[arg-type]
            )

        result = await self.db.execute(stmt)
        channels = result.scalars().all()

        return [
            {
                "channel_id": str(channel.channel_id),
                "external_channel_id": channel.external_channel_id,
                "channel_name": channel.channel_name,
                "channel_type": channel.channel_type,
                "channel_description": channel.channel_description,
                "is_monitored": channel.is_monitored == "true",
                "participant_count": channel.participant_count,
                "last_activity_at": channel.last_activity_at,
                "is_archived": channel.is_archived == "true",
                "channel_metadata": channel.channel_metadata,
            }
            for channel in channels
        ]


__all__ = ["MessagingSyncService"]
