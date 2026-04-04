"""
Messaging sync service for Teams/Slack integration.

This service writes to:
1. messaging_channels - Channel/conversation records
2. messaging_channel_members - Channel participant memberships
3. messaging_messages - Individual message records
4. messaging_deal_links - Channel-to-deal mappings
5. integration_sync_history - Sync tracking
"""

from __future__ import annotations

import contextlib
import time
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
    MessagingChannelIntegrationState,
    MessagingChannelMember,
    MessagingMessage,
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

# Batch size for channel processing — balances memory vs commit frequency
_CHANNEL_BATCH_SIZE = 50


class MessagingSyncService:
    """
    Service to sync messaging channels and messages from Teams/Slack.

    Scope: This service modifies messaging-specific tables.
    - messaging_channels
    - messaging_channel_members (channel participants)
    - messaging_messages
    - messaging_deal_links (for channel-to-deal mappings)
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
                "Channel sync started",
                extra={
                    "event": "channel_sync_started",
                    "integration_id": str(integration.integration_id),
                    "provider": provider,
                    "channel_count": len(channel_list),
                },
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
                        "Channel sync failed for single channel",
                        extra={
                            "event": "channel_sync_item_failed",
                            "channel_index": idx,
                            "external_id": channel_data.get("id"),
                            "error": str(e),
                        },
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
                    "created_count": created_count,
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
                "Channel sync completed",
                extra={
                    "event": "channel_sync_completed",
                    "integration_id": str(integration.integration_id),
                    "provider": provider,
                    "created_count": created_count,
                    "updated": updated_count,
                    "failed": records_failed,
                },
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created_count": created_count,
                "updated": updated_count,
            }

        except Exception as e:
            logger.error(
                "Channel sync failed",
                extra={
                    "event": "channel_sync_failed",
                    "integration_id": str(integration.integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
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
        - Batch-resolves participants across all channels (single query)
        - Batch-loads memberships per commit batch
        - Commits in batches of _CHANNEL_BATCH_SIZE for throughput

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
                "Incremental channel sync started",
                extra={
                    "event": "incremental_channel_sync_started",
                    "integration_id": str(integration.integration_id),
                    "provider": provider,
                    "channel_count": len(channel_list),
                },
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
                    "created_count": 0,
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
                external_ids, provider
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0

            t0 = time.monotonic()

            # Process channels in batches for controlled commit frequency
            for batch_start in range(0, len(channel_list), _CHANNEL_BATCH_SIZE):
                batch = channel_list[batch_start : batch_start + _CHANNEL_BATCH_SIZE]

                for idx, channel_data in enumerate(batch, start=batch_start):
                    try:
                        # Savepoint per channel — failure rolls back only this
                        # channel, not the entire batch
                        async with self.db.begin_nested():
                            external_id = self._extract_channel_id(channel_data, provider)
                            if not external_id:
                                logger.debug(
                                    "Channel %d: skipping (no external_id)",
                                    idx,
                                )
                                records_failed += 1
                                continue

                            sync_action = extract_sync_action(channel_data)
                            action_counts.increment(sync_action)

                            existing_channel = existing_map.get(external_id)
                            is_new = existing_channel is None

                            if is_new:
                                channel = await self._create_channel(
                                    integration,
                                    channel_data,
                                    provider,
                                    external_id,
                                )
                                created_count += 1
                                logger.debug(
                                    "Created channel: external_id=%s name=%s",
                                    external_id,
                                    channel_data.get("displayName"),
                                )

                                members = channel_data.get("members", [])
                                if members:
                                    await self._sync_channel_members(
                                        channel, channel_data, provider
                                    )

                                await self._upsert_integration_state(
                                    channel, integration.integration_id, external_id
                                )
                            else:
                                if self._has_meaningful_channel_changes(
                                    existing_channel, channel_data, provider
                                ):
                                    await self._update_channel(
                                        existing_channel, channel_data, provider
                                    )
                                    updated_count += 1
                                    logger.debug(
                                        "Updated channel: external_id=%s name=%s",
                                        external_id,
                                        channel_data.get("displayName"),
                                    )

                                    members = channel_data.get("members", [])
                                    if members:
                                        await self._sync_channel_members(
                                            existing_channel, channel_data, provider
                                        )
                                else:
                                    skipped_count += 1
                                    logger.debug(
                                        "Skipped unchanged channel: external_id=%s",
                                        external_id,
                                    )

                                    members = channel_data.get("members", [])
                                    if members:
                                        await self._sync_channel_members(
                                            existing_channel, channel_data, provider
                                        )

                                await self._upsert_integration_state(
                                    existing_channel, integration.integration_id, external_id
                                )

                            records_processed += 1

                    except Exception as e:
                        logger.error(
                            "Incremental channel sync failed for single channel",
                            extra={
                                "event": "incremental_channel_sync_item_failed",
                                "channel_index": idx,
                                "external_id": channel_data.get("id"),
                                "error": str(e),
                            },
                            exc_info=True,
                        )
                        records_failed += 1

                # Commit after each batch
                await self.db.commit()

            # Update sync history
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created_count": created_count,
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

            total_time = round(time.monotonic() - t0, 2)
            logger.info(
                "Incremental channel sync completed",
                extra={
                    "event": "incremental_channel_sync_completed",
                    "integration_id": str(integration.integration_id),
                    "provider": provider,
                    "created_count": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "failed": records_failed,
                    "action_counts": action_counts.to_dict(),
                    "total_time_s": total_time,
                },
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created_count": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
            }

        except Exception as e:
            logger.error(
                "Incremental channel sync failed",
                extra={
                    "event": "incremental_channel_sync_failed",
                    "integration_id": str(integration.integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )

            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(channel_list),
                error_details={
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
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
                "Message sync started",
                extra={
                    "event": "message_sync_started",
                    "integration_id": str(integration.integration_id),
                    "provider": provider,
                    "message_count": len(message_list),
                    "channel_id": str(channel_id) if channel_id else None,
                },
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

                    # Resolve channel for this message
                    resolved_channel_id = await self._ensure_channel_id(
                        message_data, provider, channel_id, external_id
                    )

                    # Check if message already exists (unique per channel)
                    existing_message = await self._get_message_by_external_id(
                        resolved_channel_id, external_id
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
                            message_data,
                            provider,
                            external_id,
                            resolved_channel_id,
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
                        "Message sync failed for single message",
                        extra={
                            "event": "message_sync_item_failed",
                            "message_index": idx,
                            "external_id": message_data.get("id"),
                            "error": str(e),
                        },
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
                    "created_count": created_count,
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
                "Message sync completed",
                extra={
                    "event": "message_sync_completed",
                    "integration_id": str(integration.integration_id),
                    "provider": provider,
                    "created_count": created_count,
                    "updated": updated_count,
                    "failed": records_failed,
                },
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created_count": created_count,
                "updated": updated_count,
            }

        except Exception as e:
            logger.error(
                "Message sync failed",
                extra={
                    "event": "message_sync_failed",
                    "integration_id": str(integration.integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
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
        """Get all monitored channels visible to this integration."""
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
                col(MessagingChannel.is_monitored) == "true",
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _get_channel_by_external_id(self, external_id: str) -> MessagingChannel | None:
        """Get existing messaging channel by external_id (org-scoped)."""
        stmt = select(MessagingChannel).where(
            col(MessagingChannel.organisation_id) == self.organisation_id,
            col(MessagingChannel.external_channel_id) == external_id,
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _load_existing_channels_by_external_ids(
        self,
        external_ids: list[str],
        provider: str,
    ) -> dict[str, MessagingChannel]:
        """Batch load existing channels by external IDs for efficient comparison."""
        if not external_ids:
            return {}

        stmt = select(MessagingChannel).where(
            col(MessagingChannel.organisation_id) == self.organisation_id,
            col(MessagingChannel.provider) == provider,
            col(MessagingChannel.external_channel_id).in_(external_ids),
        )
        result = await self.db.execute(stmt)
        channels = result.scalars().all()

        return {ch.external_channel_id: ch for ch in channels}

    async def _upsert_integration_state(
        self,
        channel: MessagingChannel,
        integration_id: UUID,
        external_channel_id: str,
    ) -> None:
        """Create or update integration visibility state for a channel."""
        stmt = select(MessagingChannelIntegrationState).where(
            col(MessagingChannelIntegrationState.channel_id) == channel.channel_id,
            col(MessagingChannelIntegrationState.integration_id) == integration_id,
        )
        result = await self.db.execute(stmt)
        state = result.scalars().first()

        if state:
            state.is_deleted = False
            state.updated_at = datetime.now(UTC)
            self.db.add(state)
        else:
            state = MessagingChannelIntegrationState(
                channel_id=channel.channel_id,
                integration_id=integration_id,
                external_channel_id=external_channel_id,
                organisation_id=self.organisation_id,
            )
            self.db.add(state)

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
        integration: KnowledgeIntegration,  # noqa: ARG002 — kept for caller interface consistency
        channel_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> MessagingChannel:
        """Create a new org-scoped messaging channel."""
        channel = MessagingChannel(
            organisation_id=self.organisation_id,
            provider=provider,
            external_channel_id=external_id,
            last_synced_at=datetime.now(UTC),
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

        channel.last_synced_at = datetime.now(UTC)

        self.db.add(channel)
        await self.db.flush()

    async def _sync_channel_members(
        self,
        channel: MessagingChannel,
        channel_data: dict[str, Any],
        provider: str,
    ) -> int:
        """
        Sync channel members — stores member data directly on the row.

        Batch-loads all existing members for the channel in one query, then
        upserts in-memory. This avoids N+1 queries (one SELECT per member).
        """
        members = channel_data.get("members", [])
        if not members:
            return 0

        # Batch-load all existing members for this channel in one query
        existing_stmt = select(MessagingChannelMember).where(
            col(MessagingChannelMember.channel_id) == channel.channel_id,
        )
        existing_result = await self.db.execute(existing_stmt)
        existing_map: dict[str, MessagingChannelMember] = {
            m.external_user_id: m for m in existing_result.scalars().all()
        }

        synced_count = 0
        current_external_user_ids: set[str] = set()

        for member in members:
            try:
                if provider == "teams":
                    external_user_id = member.get("userId") or member.get("id")
                    display_name = member.get("displayName")
                    email = member.get("email")
                    roles = member.get("roles", [])
                    member_role = roles[0] if roles else "member"
                    joined_at_str = member.get("visibleHistoryStartDateTime")
                    tenant_id = member.get("tenantId")
                else:
                    external_user_id = member.get("id") or member.get("userId")
                    display_name = member.get("displayName") or member.get("name")
                    email = member.get("email")
                    member_role = member.get("role", "member")
                    joined_at_str = member.get("joinedAt")
                    roles = []
                    tenant_id = None

                if not external_user_id:
                    continue

                current_external_user_ids.add(external_user_id)

                joined_at: datetime | None = None
                if joined_at_str and joined_at_str.strip():
                    with contextlib.suppress(ValueError, TypeError):
                        joined_at = datetime.fromisoformat(joined_at_str.replace("Z", "+00:00"))

                email_normalized = email.lower().strip() if email else None

                existing = existing_map.get(external_user_id)
                if existing:
                    existing.display_name = display_name
                    existing.email = email
                    existing.email_normalized = email_normalized
                    existing.member_role = member_role
                    existing.left_at = None
                    existing.is_deleted = False
                    existing.member_metadata = {
                        "roles": roles,
                        "tenant_id": tenant_id,
                        "last_synced_at": datetime.now(UTC).isoformat(),
                    }
                    if joined_at and (existing.joined_at is None or existing.joined_at.year < 1900):
                        existing.joined_at = joined_at
                    self.db.add(existing)
                else:
                    new_member = MessagingChannelMember(
                        organisation_id=self.organisation_id,
                        channel_id=channel.channel_id,
                        external_user_id=external_user_id,
                        email=email,
                        email_normalized=email_normalized,
                        display_name=display_name or "Unknown",
                        joined_at=joined_at,
                        member_role=member_role,
                        member_metadata={
                            "roles": roles,
                            "tenant_id": tenant_id,
                            "last_synced_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    self.db.add(new_member)

                synced_count += 1

            except Exception as e:
                logger.error(
                    "Failed to sync channel member",
                    extra={
                        "event": "channel_member_sync_failed",
                        "channel_id": str(channel.channel_id),
                        "member_id": member.get("userId") or member.get("id"),
                        "error": str(e),
                    },
                    exc_info=True,
                )
                continue

        await self.db.flush()

        # Mark members no longer in the channel as left
        if current_external_user_ids:
            for ext_id, existing_member in existing_map.items():
                if ext_id not in current_external_user_ids and existing_member.left_at is None:
                    existing_member.left_at = datetime.now(UTC)
                    self.db.add(existing_member)

        channel.participant_count = len(members)
        self.db.add(channel)
        await self.db.flush()

        return synced_count

    async def _get_message_by_external_id(
        self, channel_id: UUID, external_id: str
    ) -> MessagingMessage | None:
        """Get existing messaging message by external_id (org-scoped, unique per channel)."""
        stmt = select(MessagingMessage).where(
            col(MessagingMessage.channel_id) == channel_id,
            col(MessagingMessage.external_message_id) == external_id,
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _create_message(
        self,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        channel_id: UUID,
    ) -> MessagingMessage:
        """Create a new org-scoped messaging message."""
        message = MessagingMessage(
            organisation_id=self.organisation_id,
            external_message_id=external_id,
            channel_id=channel_id,
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
        message_data: dict[str, Any],
        provider: str,
        existing_channel_id: UUID | None,
        external_message_id: str,
    ) -> UUID:
        """Ensure we have a channel UUID for this message, resolving from external ID if needed."""
        if existing_channel_id:
            return existing_channel_id

        external_channel_id = self._extract_external_channel_id_from_message(message_data, provider)
        resolved = await self._resolve_channel_id(external_channel_id)
        if resolved:
            return resolved

        raise ValueError(
            "Unable to resolve channel for message "
            f"{external_message_id} (provider={provider}, external_channel_id={external_channel_id})"
        )

    async def _resolve_channel_id(
        self,
        external_channel_id: str | None,
    ) -> UUID | None:
        """Resolve channel UUID using provider-specific identifiers (org-scoped)."""
        if not external_channel_id:
            return None

        cached_id = self._channel_cache.get(external_channel_id)
        if cached_id:
            return cached_id

        channel = await self._get_channel_by_external_id(external_channel_id)
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
                    "Channel-to-deal link already exists",
                    extra={
                        "event": "deal_link_exists",
                        "channel_id": str(channel_id),
                        "deal_id": str(deal_id),
                    },
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
                    "Enabled monitoring for channel",
                    extra={
                        "event": "channel_monitoring_enabled",
                        "channel_id": str(channel_id),
                        "deal_id": str(deal_id),
                    },
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
            logger.error(
                "Failed to link channel to deal",
                extra={
                    "event": "deal_link_failed",
                    "channel_id": str(channel_id),
                    "deal_id": str(deal_id),
                    "error": str(e),
                },
                exc_info=True,
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
                col(MessagingChannel.is_deleted).is_(False),
            )
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
