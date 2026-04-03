"""
Messaging sync service for Teams/Slack integration.

This service writes to:
1. messaging_channels - Channel/conversation records
2. messaging_channel_members - Channel participant memberships
3. messaging_messages - Individual message records
4. messaging_deal_links - Channel-to-deal mappings
5. integration_participants - Participant/user records
6. integration_sync_history - Sync tracking
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import (
    SyncStatus,
)
from src.domain.models.organisation_tables.sync.messaging_models import (
    ChannelType,
    MessageType,
    MessagingChannel,
    MessagingChannelMember,
    MessagingMessage,
)
from src.domain.models.organisation_tables.sync.participant_models import (
    IntegrationParticipant,
    IntegrationParticipantIdentity,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncActionCounts,
    extract_sync_action,
)

logger = get_logger(__name__)


class MessagingSyncService:
    """
    Service to sync messaging channels and messages from Teams/Slack.

    Scope: This service modifies messaging-specific tables.
    - messaging_channels
    - messaging_channel_members (channel participants)
    - messaging_messages
    - messaging_deal_links (for channel-to-deal mappings)
    - integration_participants (resolved participant records)
    - integration_sync_history
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id
        self._sync_history = SyncHistoryManager(session)
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
        sync_history = await self._sync_history.start(integration.integration_id)

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
            await self._sync_history.complete(
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

            await self._sync_history.complete(
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

    async def sync_channels_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        channels: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """
        Sync messaging channels using incremental optimization.

        This method:
        - Skips unchanged channels based on change detection
        - Uses database state to determine create vs update
        - Tracks action counts for observability

        Tables Modified:
        - messaging_channels (primary)
        - integration_sync_history (tracking)
        - knowledge_integrations (last_synced_at update only)

        Args:
            integration: The integration record
            channels: List of channel dictionaries from provider (with _sync_metadata)
            provider: Provider name (teams, slack, etc.)

        Returns:
            Dictionary with sync statistics including action counts
        """
        sync_history = await self._sync_history.start(integration.integration_id)
        action_counts = SyncActionCounts()

        try:
            channel_list = list(channels)
            logger.info(
                "sync_channels_incremental started: integration=%s provider=%s channel_count=%d",
                integration.integration_id,
                provider,
                len(channel_list),
            )

            if not channel_list:
                await self._sync_history.complete(
                    sync_history,
                    status=SyncStatus.SUCCESS,
                    records_processed=0,
                    records_failed=0,
                    metadata={"provider": provider, "skipped": 0},
                )
                return {
                    "status": "success",
                    "records_processed": 0,
                    "records_failed": 0,
                    "created": 0,
                    "updated": 0,
                    "skipped": 0,
                }

            # Batch load existing channels for comparison
            external_ids = [
                self._extract_channel_id(ch, provider)
                for ch in channel_list
                if self._extract_channel_id(ch, provider)
            ]
            existing_map = await self._load_existing_channels_by_external_ids(
                integration.integration_id, external_ids
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0

            for idx, channel_data in enumerate(channel_list):
                try:
                    external_id = self._extract_channel_id(channel_data, provider)
                    if not external_id:
                        logger.debug("Channel %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Extract sync action for observability
                    sync_action = extract_sync_action(channel_data)
                    action_counts.increment(sync_action)

                    # Database state is source of truth for create vs update
                    existing_channel = existing_map.get(external_id)
                    is_new = existing_channel is None

                    if is_new:
                        # Create new channel
                        channel = await self._create_channel(
                            integration, channel_data, provider, external_id
                        )
                        created_count += 1
                        logger.debug(
                            "Created channel: external_id=%s name=%s",
                            external_id,
                            channel_data.get("displayName"),
                        )

                        # Sync members for new channel
                        members = channel_data.get("members", [])
                        if members:
                            await self._sync_channel_members(
                                channel, channel_data, integration, provider
                            )
                    else:
                        # Check if meaningful changes exist
                        if self._has_meaningful_channel_changes(
                            existing_channel, channel_data, provider
                        ):
                            await self._update_channel(existing_channel, channel_data, provider)
                            updated_count += 1
                            logger.debug(
                                "Updated channel: external_id=%s name=%s",
                                external_id,
                                channel_data.get("displayName"),
                            )

                            # Sync members for updated channel
                            members = channel_data.get("members", [])
                            if members:
                                await self._sync_channel_members(
                                    existing_channel, channel_data, integration, provider
                                )
                        else:
                            skipped_count += 1
                            logger.debug(
                                "Skipped unchanged channel: external_id=%s",
                                external_id,
                            )

                            # Still sync members for skipped channels if we have member data
                            members = channel_data.get("members", [])
                            if members:
                                await self._sync_channel_members(
                                    existing_channel, channel_data, integration, provider
                                )

                    records_processed += 1
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
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "provider": provider,
                    "action_counts": action_counts.to_dict(),
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Channel incremental sync completed: integration=%s "
                "created=%d updated=%d skipped=%d failed=%d actions=%s",
                integration.integration_id,
                created_count,
                updated_count,
                skipped_count,
                records_failed,
                action_counts.to_dict(),
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
            }

        except Exception as e:
            logger.error(
                "Channel incremental sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(channel_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

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
        sync_history = await self._sync_history.start(integration.integration_id)

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
            await self._sync_history.complete(
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

            await self._sync_history.complete(
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
            col(MessagingChannel.integration_id) == integration_id,
            col(MessagingChannel.is_monitored) == "true",
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _get_channel_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> MessagingChannel | None:
        """Get existing messaging channel by external_id."""
        stmt = select(MessagingChannel).where(
            col(MessagingChannel.integration_id) == integration_id,
            col(MessagingChannel.external_channel_id) == external_id,
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _load_existing_channels_by_external_ids(
        self, integration_id: UUID, external_ids: list[str]
    ) -> dict[str, MessagingChannel]:
        """Batch load existing channels by external IDs for efficient comparison."""
        if not external_ids:
            return {}

        stmt = select(MessagingChannel).where(
            col(MessagingChannel.integration_id) == integration_id,
            col(MessagingChannel.external_channel_id).in_(external_ids),
        )
        result = await self.db.execute(stmt)
        channels = result.scalars().all()

        return {ch.external_channel_id: ch for ch in channels}

    def _has_meaningful_channel_changes(
        self,
        existing: MessagingChannel,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming channel data has meaningful changes.

        Args:
            existing: Existing MessagingChannel record from database
            incoming: Incoming channel data from provider
            provider: Provider name (teams, slack, etc.)

        Returns:
            True if meaningful changes detected, False otherwise
        """
        fields = self._extract_channel_fields(incoming, provider)

        # Compare key fields
        checks = [
            (existing.channel_name, fields.get("channel_name")),
            (existing.channel_type, fields.get("channel_type")),
            (existing.channel_description, fields.get("channel_description")),
            (existing.is_archived, fields.get("is_archived")),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        # Compare participant_count
        incoming_count = fields.get("participant_count", 0)
        if existing.participant_count != incoming_count:
            return True

        # Check last_updated_date_time in metadata (primary indicator for Teams)
        existing_metadata = existing.channel_metadata or {}
        incoming_metadata = fields.get("channel_metadata") or {}
        existing_updated = existing_metadata.get("last_updated_date_time")
        incoming_updated = incoming_metadata.get("last_updated_date_time")

        return bool(incoming_updated and existing_updated and incoming_updated != existing_updated)

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

    async def _sync_channel_members(
        self,
        channel: MessagingChannel,
        channel_data: dict[str, Any],
        integration: KnowledgeIntegration,
        provider: str,
    ) -> int:
        """
        Sync channel members/participants from provider data.

        This method:
        1. Extracts members from channel_data
        2. Resolves or creates IntegrationParticipant for each member
        3. Upserts MessagingChannelMember records (safe for multiple integrations)

        Args:
            channel: The MessagingChannel record
            channel_data: Raw channel data from provider (with 'members' array)
            integration: The integration record
            provider: Provider name (teams, slack, etc.)

        Returns:
            Number of members synced
        """
        members = channel_data.get("members", [])
        if not members:
            logger.debug(
                "No members to sync for channel: channel_id=%s",
                channel.channel_id,
            )
            return 0

        logger.debug(
            "Syncing %d members for channel: channel_id=%s external_id=%s",
            len(members),
            channel.channel_id,
            channel.external_channel_id,
        )

        synced_count = 0
        current_member_participant_ids: set[UUID] = set()

        for member in members:
            try:
                # Extract member data based on provider
                if provider == "teams":
                    external_user_id = member.get("userId") or member.get("id")
                    display_name = member.get("displayName")
                    email = member.get("email")
                    roles = member.get("roles", [])
                    member_role = roles[0] if roles else "member"
                    joined_at_str = member.get("visibleHistoryStartDateTime")
                    tenant_id = member.get("tenantId")
                else:
                    # Slack or other providers
                    external_user_id = member.get("id") or member.get("userId")
                    display_name = member.get("displayName") or member.get("name")
                    email = member.get("email")
                    member_role = member.get("role", "member")
                    joined_at_str = member.get("joinedAt")
                    roles = []
                    tenant_id = None

                if not external_user_id:
                    logger.debug(
                        "Skipping member without user ID in channel %s", channel.channel_id
                    )
                    continue

                # Resolve or create IntegrationParticipant
                participant = await self._resolve_or_create_participant(
                    integration=integration,
                    external_user_id=external_user_id,
                    display_name=display_name,
                    email=email,
                    provider=provider,
                )

                if not participant:
                    logger.warning(
                        "Could not resolve participant for member: external_user_id=%s channel_id=%s",
                        external_user_id,
                        channel.channel_id,
                    )
                    continue

                current_member_participant_ids.add(participant.participant_id)

                # Parse joined_at timestamp - default to now if not provided
                joined_at: datetime = datetime.now(UTC)
                if joined_at_str and joined_at_str.strip():
                    try:
                        joined_at = datetime.fromisoformat(joined_at_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError) as e:
                        logger.debug(
                            "Could not parse joined_at '%s': %s, using current time",
                            joined_at_str,
                            e,
                        )
                        joined_at = datetime.now(UTC)

                # Build metadata
                member_metadata = {
                    "provider": provider,
                    "external_user_id": external_user_id,
                    "email": email,
                    "roles": roles,
                    "tenant_id": tenant_id,
                    "last_synced_at": datetime.now(UTC).isoformat(),
                }

                # Upsert membership - find any existing (active or not)
                existing_membership = await self._get_channel_membership(
                    channel.channel_id, participant.participant_id
                )

                if existing_membership:
                    # Update existing membership
                    existing_membership.member_role = member_role
                    existing_membership.member_metadata = member_metadata
                    existing_membership.left_at = None  # Mark as active
                    existing_membership.is_deleted = False
                    # Fix invalid joined_at (e.g., -infinity from previous bugs)
                    if (
                        existing_membership.joined_at is None
                        or existing_membership.joined_at.year < 1900
                    ):
                        existing_membership.joined_at = joined_at
                    self.db.add(existing_membership)
                    logger.debug(
                        "Updated membership: channel=%s participant=%s",
                        channel.channel_id,
                        participant.participant_id,
                    )
                else:
                    # Create new membership
                    new_membership = MessagingChannelMember(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        channel_id=channel.channel_id,
                        participant_id=participant.participant_id,
                        joined_at=joined_at,
                        member_role=member_role,
                        member_metadata=member_metadata,
                    )
                    self.db.add(new_membership)
                    logger.debug(
                        "Created membership: channel=%s participant=%s",
                        channel.channel_id,
                        participant.participant_id,
                    )

                synced_count += 1

            except Exception as e:
                logger.error(
                    "Failed to sync channel member: channel_id=%s member=%s error=%s",
                    channel.channel_id,
                    member.get("userId") or member.get("id"),
                    str(e),
                    exc_info=True,
                )
                continue

        # Flush all member changes
        await self.db.flush()

        # Mark members no longer in the channel as left (soft delete)
        await self._mark_removed_members(
            channel.channel_id,
            current_member_participant_ids,
            integration.integration_id,
        )

        # Update participant count on channel
        channel.participant_count = len(members)
        self.db.add(channel)
        await self.db.flush()

        logger.debug(
            "Synced %d/%d members for channel: channel_id=%s",
            synced_count,
            len(members),
            channel.channel_id,
        )

        return synced_count

    async def _get_channel_membership(
        self, channel_id: UUID, participant_id: UUID
    ) -> MessagingChannelMember | None:
        """
        Get existing channel membership by channel and participant.

        Finds the most recent membership record (whether active or not).
        """
        stmt = (
            select(MessagingChannelMember)
            .where(
                col(MessagingChannelMember.channel_id) == channel_id,
                col(MessagingChannelMember.participant_id) == participant_id,
            )
            .order_by(col(MessagingChannelMember.joined_at).desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _mark_removed_members(
        self,
        channel_id: UUID,
        current_participant_ids: set[UUID],
        integration_id: UUID,
    ) -> None:
        """
        Mark members who are no longer in the channel as left.

        Only marks members from the same integration to avoid conflicts
        when multiple integrations sync the same channel.
        """
        if not current_participant_ids:
            return

        # Find active members from this integration who are no longer in the member list
        stmt = select(MessagingChannelMember).where(
            col(MessagingChannelMember.channel_id) == channel_id,
            col(MessagingChannelMember.integration_id) == integration_id,
            col(MessagingChannelMember.left_at).is_(None),
            col(MessagingChannelMember.participant_id).notin_(current_participant_ids),
        )
        result = await self.db.execute(stmt)
        removed_members = result.scalars().all()

        for member in removed_members:
            member.left_at = datetime.now(UTC)
            self.db.add(member)
            logger.debug(
                "Marked member as left: channel=%s participant=%s",
                channel_id,
                member.participant_id,
            )

    async def _resolve_or_create_participant(
        self,
        integration: KnowledgeIntegration,
        external_user_id: str,
        display_name: str | None,
        email: str | None,
        provider: str,
    ) -> IntegrationParticipant | None:
        """
        Resolve existing or create new IntegrationParticipant.

        Uses the two-table structure:
        - IntegrationParticipant: Canonical participant record
        - IntegrationParticipantIdentity: Provider-specific identity mapping

        First tries to find by provider_participant_id in identities, then by email.
        Creates new participant and identity if not found.
        """
        email_normalized = email.lower().strip() if email else None

        # Try to find by provider participant ID in identities
        stmt = (
            select(IntegrationParticipant)
            .join(IntegrationParticipantIdentity)
            .where(
                col(IntegrationParticipantIdentity.integration_id) == integration.integration_id,
                col(IntegrationParticipantIdentity.provider_participant_id) == external_user_id,
            )
        )
        result = await self.db.execute(stmt)
        participant = result.scalars().first()

        if participant:
            # Update display name if changed
            if display_name and participant.display_name != display_name:
                participant.display_name = display_name
                self.db.add(participant)
            return participant

        # Try to find by email if we have one
        if email_normalized:
            stmt = select(IntegrationParticipant).where(
                col(IntegrationParticipant.organisation_id) == self.organisation_id,
                col(IntegrationParticipant.canonical_email_normalized) == email_normalized,
            )
            result = await self.db.execute(stmt)
            participant = result.scalars().first()

            if participant:
                # Create identity link for this provider
                await self._create_participant_identity(
                    integration=integration,
                    participant=participant,
                    external_user_id=external_user_id,
                    email=email,
                    display_name=display_name,
                )
                # Update display name if changed
                if display_name and participant.display_name != display_name:
                    participant.display_name = display_name
                    self.db.add(participant)
                return participant

        # Create new participant
        new_participant = IntegrationParticipant(
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            canonical_email=email,
            canonical_email_normalized=email_normalized,
            display_name=display_name or "Unknown",
            participant_metadata={
                "provider": provider,
                "source": "channel_member_sync",
            },
        )
        self.db.add(new_participant)
        await self.db.flush()
        await self.db.refresh(new_participant)

        # Create identity link
        await self._create_participant_identity(
            integration=integration,
            participant=new_participant,
            external_user_id=external_user_id,
            email=email,
            display_name=display_name,
        )

        logger.debug(
            "Created new participant: external_id=%s display_name=%s email=%s",
            external_user_id,
            display_name,
            email,
        )

        return new_participant

    async def _create_participant_identity(
        self,
        integration: KnowledgeIntegration,
        participant: IntegrationParticipant,
        external_user_id: str,
        email: str | None,
        display_name: str | None,
    ) -> IntegrationParticipantIdentity:
        """Create a provider-specific identity for a participant."""
        email_normalized = email.lower().strip() if email else None

        # Check if identity already exists
        stmt = select(IntegrationParticipantIdentity).where(
            col(IntegrationParticipantIdentity.integration_id) == integration.integration_id,
            col(IntegrationParticipantIdentity.provider_participant_id) == external_user_id,
        )
        result = await self.db.execute(stmt)
        existing = result.scalars().first()

        if existing:
            # Update existing identity
            existing.last_seen_at = datetime.now(UTC)
            if display_name:
                existing.provider_display_name = display_name
            self.db.add(existing)
            return existing

        # Create new identity
        identity = IntegrationParticipantIdentity(
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            participant_id=participant.participant_id,
            provider_participant_id=external_user_id,
            provider_email=email,
            provider_email_normalized=email_normalized,
            provider_display_name=display_name,
        )
        self.db.add(identity)
        await self.db.flush()

        logger.debug(
            "Created participant identity: participant=%s provider_id=%s",
            participant.participant_id,
            external_user_id,
        )

        return identity

    async def _get_message_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> MessagingMessage | None:
        """Get existing messaging message by external_id."""
        stmt = select(MessagingMessage).where(
            col(MessagingMessage.integration_id) == integration_id,
            col(MessagingMessage.external_message_id) == external_id,
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
            # Teams messages can have either channelId (for team channels) or chatId (for chats)
            return message_data.get("channelId") or message_data.get("chatId")
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
        """
        Extract channel fields from Microsoft Teams data.

        Handles both Team channels and chats (1:1, group, meeting).
        """
        # Determine if this is a Team channel or a chat
        chat_type = channel_data.get("chatType")  # group, meeting (oneOnOne excluded)
        membership_type = channel_data.get("membershipType", "standard")  # standard, private

        # Map to provider-agnostic ChannelType
        # Note: oneOnOne chats are filtered out in fetch_chats()
        if chat_type == "group":
            channel_type = ChannelType.GROUP_MESSAGE
        elif chat_type == "meeting":
            channel_type = ChannelType.MEETING
        elif membership_type == "private":
            channel_type = ChannelType.PRIVATE
        else:
            channel_type = ChannelType.PUBLIC

        # Get display name (different field for chats vs channels)
        display_name = channel_data.get("displayName") or channel_data.get("topic") or "Unnamed"

        return {
            "channel_name": display_name,
            "channel_type": channel_type,
            "channel_description": channel_data.get("description"),
            "is_monitored": "false",  # Default; user configures later (stored as string in DB)
            "participant_count": 0,  # Would need separate API call to get member count
            "is_archived": "false",  # Teams doesn't expose this in basic channel info
            "channel_metadata": {
                "provider": "teams",  # Identify the source platform
                # Team channel fields
                "team_id": channel_data.get("teamId"),
                "team_name": channel_data.get("teamName"),
                "membership_type": membership_type,
                "email": channel_data.get("email"),
                "web_url": channel_data.get("webUrl"),
                # Chat fields
                "chat_type": chat_type,
                "topic": channel_data.get("topic"),
                "tenant_id": channel_data.get("tenantId"),
                "online_meeting_info": channel_data.get("onlineMeetingInfo"),
                # Common fields
                "created_date_time": channel_data.get("createdDateTime"),
                "last_updated_date_time": channel_data.get("lastUpdatedDateTime"),
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
            "channel_metadata": {
                "provider": "unknown",  # Should be set by specific provider
            },
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
                "provider": "teams",
                "importance": message_data.get("importance"),
                "message_type": msg_type_str,
                "subject": message_data.get("subject"),
                "locale": message_data.get("locale"),
                # Team channel fields
                "team_id": message_data.get("teamId"),
                "channel_id": message_data.get("channelId"),
                # Chat fields
                "chat_id": message_data.get("chatId"),
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
                col(MessagingDealLink.channel_id) == channel_id,
                col(MessagingDealLink.deal_id) == deal_id,
                col(MessagingDealLink.is_deleted) == False,  # noqa: E712
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
            stmt = select(MessagingChannel).where(col(MessagingChannel.channel_id) == channel_id)
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
                col(MessagingChannel.channel_id) == col(MessagingDealLink.channel_id),
            )
            .where(
                col(MessagingDealLink.deal_id) == deal_id,
                col(MessagingDealLink.integration_id) == integration_id,
                col(MessagingDealLink.is_deleted) == False,  # noqa: E712
                col(MessagingChannel.is_deleted) == False,  # noqa: E712
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
            col(MessagingChannel.integration_id) == integration_id,
            col(MessagingChannel.is_deleted) == False,  # noqa: E712
        )

        if not include_archived:
            stmt = stmt.where(col(MessagingChannel.is_archived) == "false")

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
