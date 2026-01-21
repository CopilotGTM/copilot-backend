"""
Email Delta Sync Service.

Handles email synchronization orchestration for scheduled tasks.
ALL business logic for email sync is centralized here.

Tasks call this service - they are thin triggers only.

Architecture:
- Tier 1 (all_orgs_task) queries orgs, dispatches Tier 2 tasks
- Tier 2 (org_task) calls this service for each org
- Tier 3 (integration_task) calls this service for single integration

This service orchestrates:
1. Folder sync (always done first)
2. Message sync for important/monitored folders only

Important Folders (synced by default):
- inbox: Main mailbox
- sent: Sent messages

Additional folders can be enabled via is_monitored flag.

Session Management:
    This service follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Load integration + folder info, release session
    2. Fetch folders from external API (NO session held)
    3. Session 2: Write folders to DB, release session
    4. For each folder:
       a. Fetch messages from external API (NO session held)
       b. Session N: Write messages to DB, release session
    5. Session final: Update integration metadata
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import EmailFolder
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.email_sync_service import EmailSyncService
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers.email.outlook_email import (
    FetchMessagesResult,
    fetch_folders,
    fetch_messages,
)

logger = get_logger(__name__)


@dataclass
class IntegrationSyncData:
    """Data extracted from integration needed for sync operations.

    This dataclass holds the minimal data needed to perform external API calls,
    allowing us to release the database session before making potentially
    long-running API calls.
    """

    integration_id: UUID
    integration_type: IntegrationType
    integration_type_value: str
    connection_id: str
    provider: str
    provider_key: str
    last_synced_at: datetime | None
    connection_metadata: dict[str, Any] | None


@dataclass
class FolderSyncData:
    """Data extracted from folder needed for message sync."""

    folder_id: UUID
    external_folder_id: str
    folder_name: str | None
    folder_type: str | None
    last_synced_at: datetime | None
    delta_link: str | None  # Retrieved from integration metadata


# Email integration types to sync
EMAIL_INTEGRATION_TYPES = (
    IntegrationType.OUTLOOK_EMAIL,
    # IntegrationType.GMAIL,  # Not yet implemented
)

# Note: Folder monitoring is controlled by is_monitored flag in email_folders table
# By default, inbox and sent folders are set to is_monitored=True
# Users can enable/disable monitoring for any folder

# Key for storing email folder delta links in connection_metadata
EMAIL_DELTA_LINKS_KEY = "email_folder_delta_links"


def _get_provider_key(integration_type: IntegrationType) -> str:
    """Get Nango provider config key for integration type."""
    return to_nango_connect_integration_key(integration_type)


def _get_folder_delta_link(integration: KnowledgeIntegration, folder_id: str) -> str | None:
    """
    Get stored deltaLink for a specific folder from connection_metadata.

    Args:
        integration: The integration with connection_metadata
        folder_id: The external folder ID

    Returns:
        Stored deltaLink or None if not found
    """
    metadata = integration.connection_metadata or {}
    delta_links = metadata.get(EMAIL_DELTA_LINKS_KEY, {})
    return delta_links.get(folder_id)


def _set_folder_delta_link(
    integration: KnowledgeIntegration,
    folder_id: str,
    delta_link: str | None,
) -> None:
    """
    Store deltaLink for a specific folder in connection_metadata.

    Args:
        integration: The integration to update
        folder_id: The external folder ID
        delta_link: The deltaLink to store (or None to remove)
    """
    if integration.connection_metadata is None:
        integration.connection_metadata = {}

    if EMAIL_DELTA_LINKS_KEY not in integration.connection_metadata:
        integration.connection_metadata[EMAIL_DELTA_LINKS_KEY] = {}

    if delta_link:
        integration.connection_metadata[EMAIL_DELTA_LINKS_KEY][folder_id] = delta_link
    elif folder_id in integration.connection_metadata[EMAIL_DELTA_LINKS_KEY]:
        del integration.connection_metadata[EMAIL_DELTA_LINKS_KEY][folder_id]


def _get_folder_delta_link_from_metadata(
    metadata: dict[str, Any] | None,
    folder_id: str,
) -> str | None:
    """
    Get stored deltaLink for a specific folder from extracted metadata dict.

    Args:
        metadata: The extracted connection_metadata dictionary
        folder_id: The external folder ID

    Returns:
        Stored deltaLink or None if not found
    """
    if not metadata:
        return None
    delta_links = metadata.get(EMAIL_DELTA_LINKS_KEY, {})
    return delta_links.get(folder_id)


class EmailDeltaSyncService:
    """
    Orchestration service for email delta sync.

    This service centralizes all email sync logic.
    Celery tasks should only call public methods here.

    Public Methods (called by tasks):
    - sync_integration() - Sync single integration (Tier 3 entry point)

    The service handles:
    - Folder discovery and sync
    - Message sync for important/monitored folders
    - Result aggregation and error handling

    Session Management:
    - Uses fresh session pattern to avoid holding connections during API calls
    - Sessions are acquired only when DB operations are needed
    - Sessions are released before external API calls
    """

    async def sync_integration(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, Any]:
        """
        Public entry point for single integration sync.

        Uses fresh session pattern:
        1. Session 1: Load integration + folder info, release session
        2. Fetch folders from API (NO session)
        3. Session 2: Write folders, get monitored folders, release session
        4. For each folder: fetch messages (NO session), write to DB (fresh session)
        5. Session final: Update integration metadata

        Used by:
        - Tier 2 org_task (for each integration in the org)
        - Tier 3 integration_task (for manual/frontend triggers)

        Args:
            organisation_id: The organization UUID
            integration_id: The specific integration to sync

        Returns:
            Dictionary with sync results
        """
        logger.info(
            "Starting email sync: org=%s integration=%s",
            organisation_id,
            integration_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Load integration data and validate
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        sync_data: IntegrationSyncData | None = None

        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(EMAIL_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(stmt)
            integration = result.scalars().first()

            if not integration:
                logger.warning(
                    "Email integration not found or inactive: org=%s integration=%s",
                    organisation_id,
                    integration_id,
                )
                return {
                    "status": "failed",
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "error": "Integration not found or inactive",
                }

            # Extract data needed for sync
            int_type = (
                IntegrationType(integration.integration_type)
                if isinstance(integration.integration_type, str)
                else integration.integration_type
            )
            sync_data = IntegrationSyncData(
                integration_id=integration.integration_id,
                integration_type=int_type,
                integration_type_value=(
                    int_type.value if hasattr(int_type, "value") else str(int_type)
                ),
                connection_id=integration.connection_id,  # type: ignore
                provider="outlook",  # Currently only Outlook is supported
                provider_key=_get_provider_key(int_type),
                last_synced_at=integration.last_synced_at,
                connection_metadata=dict(integration.connection_metadata or {}),
            )
        # Session is now released

        if not sync_data:
            return {
                "status": "failed",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "error": "Failed to acquire database session",
            }

        try:
            return await self._sync_integration_fresh_session(
                org_id=organisation_id,
                sync_data=sync_data,
            )
        except Exception as e:
            logger.error(
                "Email sync failed: org=%s integration=%s error=%s",
                organisation_id,
                integration_id,
                str(e),
                exc_info=True,
            )
            return {
                "status": "failed",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "integration_type": sync_data.integration_type_value,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def _sync_integration_fresh_session(
        self,
        org_id: UUID,
        sync_data: IntegrationSyncData,
    ) -> dict[str, Any]:
        """
        Sync a single email integration using fresh session pattern.

        Flow:
        1. Fetch folders from API (NO session held)
        2. Session: Write folders to DB, get monitored folders
        3. For each folder: fetch messages (NO session), write to DB (fresh session)
        4. Session: Update integration metadata

        Args:
            org_id: Organization UUID
            sync_data: Extracted integration data

        Returns:
            Dictionary with detailed sync results
        """
        logger.info(
            "Syncing email integration: org=%s integration=%s type=%s",
            org_id,
            sync_data.integration_id,
            sync_data.integration_type_value,
        )

        nango = NangoClient()
        try:
            # ─────────────────────────────────────────────────────────────────
            # Phase 2: Fetch folders from external API (NO session held)
            # ─────────────────────────────────────────────────────────────────
            logger.debug(
                "Fetching folders: integration=%s",
                sync_data.integration_id,
            )

            folders_data = await fetch_folders(
                nango=nango,
                connection_id=sync_data.connection_id,
                is_initial=False,  # Always incremental for scheduled sync
                last_synced_at=sync_data.last_synced_at,
                provider_config_key=sync_data.provider_key,
            )

            # ─────────────────────────────────────────────────────────────────
            # Phase 3: Write folders to DB + get monitored folders
            # Fresh session acquired and released in this block
            # ─────────────────────────────────────────────────────────────────
            folders_synced = 0
            folders_to_sync: list[FolderSyncData] = []
            # Track delta links updates to persist later
            delta_link_updates: dict[str, str | None] = {}

            async for session in acquire_org_async_session(org_id):
                # Write folders if we have any
                if folders_data:
                    email_service = EmailSyncService(session=session, organisation_id=org_id)
                    # Need to reload integration for the service
                    stmt = select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_id) == sync_data.integration_id,
                    )
                    result = await session.execute(stmt)
                    integration = result.scalar_one_or_none()

                    if integration:
                        folders_result = await email_service.sync_folders_incremental(
                            integration=integration,
                            folders=folders_data,
                            provider=sync_data.provider,
                        )
                        folders_synced = folders_result.get("records_processed", 0)

                # Get monitored folders for message sync
                stmt = select(EmailFolder).where(
                    col(EmailFolder.integration_id) == sync_data.integration_id,
                    col(EmailFolder.is_deleted).is_(False),
                    col(EmailFolder.is_monitored) == "true",
                )
                result = await session.execute(stmt)
                monitored_folders = result.scalars().all()

                for folder in monitored_folders:
                    # Get delta link from connection_metadata
                    delta_link = _get_folder_delta_link_from_metadata(
                        sync_data.connection_metadata,
                        folder.external_folder_id,
                    )
                    folders_to_sync.append(
                        FolderSyncData(
                            folder_id=folder.folder_id,
                            external_folder_id=folder.external_folder_id,
                            folder_name=folder.folder_name,
                            folder_type=folder.folder_type,
                            last_synced_at=folder.last_synced_at or sync_data.last_synced_at,
                            delta_link=delta_link,
                        )
                    )
            # Session released

            logger.info(
                "Folders to sync messages: integration=%s monitored=%d (folders=%s)",
                sync_data.integration_id,
                len(folders_to_sync),
                [f"{f.folder_name} ({f.folder_type})" for f in folders_to_sync],
            )

            if not folders_to_sync:
                logger.info(
                    "No folders to sync messages for: org=%s integration=%s",
                    org_id,
                    sync_data.integration_id,
                )
                return {
                    "status": "success",
                    "organisation_id": str(org_id),
                    "integration_id": str(sync_data.integration_id),
                    "integration_type": sync_data.integration_type_value,
                    "folders_synced": folders_synced,
                    "messages_synced": 0,
                    "folder_results": [],
                }

            # ─────────────────────────────────────────────────────────────────
            # Phase 4: For each folder, fetch messages then write to DB
            # Each folder uses: API call (no session) -> DB write (fresh session)
            # ─────────────────────────────────────────────────────────────────
            folder_results = []
            total_messages = 0

            for folder_data in folders_to_sync:
                folder_result = await self._sync_folder_messages_fresh_session(
                    nango=nango,
                    org_id=org_id,
                    sync_data=sync_data,
                    folder_data=folder_data,
                    delta_link_updates=delta_link_updates,
                )
                folder_results.append(folder_result)
                total_messages += folder_result.get("messages_synced", 0)

            # ─────────────────────────────────────────────────────────────────
            # Phase 5: Update integration metadata (delta links + last_synced_at)
            # ─────────────────────────────────────────────────────────────────
            async for session in acquire_org_async_session(org_id):
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == sync_data.integration_id,
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()

                if integration:
                    # Apply delta link updates
                    if delta_link_updates:
                        if integration.connection_metadata is None:
                            integration.connection_metadata = {}
                        if EMAIL_DELTA_LINKS_KEY not in integration.connection_metadata:
                            integration.connection_metadata[EMAIL_DELTA_LINKS_KEY] = {}

                        for folder_id, delta_link in delta_link_updates.items():
                            if delta_link:
                                integration.connection_metadata[EMAIL_DELTA_LINKS_KEY][
                                    folder_id
                                ] = delta_link

                    integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                    attributes.flag_modified(integration, "connection_metadata")
                    await session.commit()
            # Session released

            logger.info(
                "Email sync complete: org=%s integration=%s folders=%d messages=%d",
                org_id,
                sync_data.integration_id,
                len(folders_to_sync),
                total_messages,
            )

            return {
                "status": "success",
                "organisation_id": str(org_id),
                "integration_id": str(sync_data.integration_id),
                "integration_type": sync_data.integration_type_value,
                "folders_synced": folders_synced,
                "messages_synced": total_messages,
                "folder_results": folder_results,
            }

        finally:
            await nango.close()

    async def _sync_folder_messages_fresh_session(
        self,
        *,
        nango: NangoClient,
        org_id: UUID,
        sync_data: IntegrationSyncData,
        folder_data: FolderSyncData,
        delta_link_updates: dict[str, str | None],
    ) -> dict[str, Any]:
        """
        Sync messages for a single folder using fresh session pattern.

        Flow:
        1. Fetch messages from external API (NO session held)
        2. Acquire fresh session, write messages to DB, release session

        Args:
            nango: Nango client
            org_id: Organization UUID
            sync_data: Integration sync data
            folder_data: Folder sync data
            delta_link_updates: Dict to accumulate delta link updates

        Returns:
            Dictionary with folder sync results
        """
        folder_name = folder_data.folder_name or folder_data.external_folder_id
        is_initial = folder_data.delta_link is None

        logger.debug(
            "Fetching messages: integration=%s folder=%s (%s) initial=%s",
            sync_data.integration_id,
            folder_name,
            folder_data.folder_type,
            is_initial,
        )

        try:
            # ─────────────────────────────────────────────────────────────────
            # Fetch messages from external API (NO session held)
            # ─────────────────────────────────────────────────────────────────
            fetch_result: FetchMessagesResult = await fetch_messages(
                nango=nango,
                connection_id=sync_data.connection_id,
                is_initial=is_initial,
                last_synced_at=folder_data.last_synced_at,
                delta_link=folder_data.delta_link,
                provider_config_key=sync_data.provider_key,
                folder_id=folder_data.external_folder_id,
            )

            messages_data = fetch_result.messages
            new_delta_link = fetch_result.delta_link

            # Track delta link for later persistence
            if new_delta_link:
                delta_link_updates[folder_data.external_folder_id] = new_delta_link
                logger.debug(
                    "Will store new deltaLink for folder: integration=%s folder=%s",
                    sync_data.integration_id,
                    folder_name,
                )

            if not messages_data:
                logger.debug(
                    "No messages returned: integration=%s folder=%s",
                    sync_data.integration_id,
                    folder_name,
                )
                return {
                    "folder_id": str(folder_data.folder_id),
                    "folder_name": folder_name,
                    "folder_type": folder_data.folder_type,
                    "messages_synced": 0,
                    "is_initial": is_initial,
                }

            # ─────────────────────────────────────────────────────────────────
            # Write messages to DB (fresh session)
            # ─────────────────────────────────────────────────────────────────
            messages_synced = 0

            async for session in acquire_org_async_session(org_id):
                email_service = EmailSyncService(session=session, organisation_id=org_id)

                # Reload integration for the service
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == sync_data.integration_id,
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()

                if not integration:
                    logger.warning(
                        "Integration no longer exists: %s",
                        sync_data.integration_id,
                    )
                    return {
                        "folder_id": str(folder_data.folder_id),
                        "folder_name": folder_name,
                        "folder_type": folder_data.folder_type,
                        "status": "failed",
                        "error": "Integration no longer exists",
                    }

                sync_result = await email_service.sync_messages_incremental(
                    integration=integration,
                    messages=messages_data,
                    provider=sync_data.provider,
                )
                messages_synced = sync_result.get("records_processed", 0)

                # Update folder last_synced_at
                folder_stmt = select(EmailFolder).where(
                    col(EmailFolder.folder_id) == folder_data.folder_id,
                )
                folder_result = await session.execute(folder_stmt)
                folder = folder_result.scalar_one_or_none()
                if folder:
                    folder.last_synced_at = datetime.now(UTC).replace(tzinfo=None)

                await session.commit()
            # Session released

            logger.info(
                "Messages synced: integration=%s folder=%s count=%d initial=%s",
                sync_data.integration_id,
                folder_name,
                messages_synced,
                is_initial,
            )

            return {
                "folder_id": str(folder_data.folder_id),
                "folder_name": folder_name,
                "folder_type": folder_data.folder_type,
                "messages_synced": messages_synced,
                "is_initial": is_initial,
            }

        except Exception as e:
            logger.error(
                "Failed to sync folder messages: integration=%s folder=%s error=%s",
                sync_data.integration_id,
                folder_name,
                str(e),
                exc_info=True,
            )
            return {
                "folder_id": str(folder_data.folder_id),
                "folder_name": folder_name,
                "folder_type": folder_data.folder_type,
                "status": "failed",
                "error": str(e),
            }

    async def _sync_folders(
        self,
        *,
        nango: NangoClient,
        email_service: EmailSyncService,
        integration: KnowledgeIntegration,
        provider: str,
        provider_key: str,
    ) -> dict[str, Any]:
        """
        Fetch and sync email folders.

        Args:
            nango: Nango client
            email_service: Email sync service
            integration: The integration
            provider: Provider name (outlook, gmail)
            provider_key: Nango provider config key

        Returns:
            Dictionary with folder sync results
        """
        logger.debug(
            "Fetching folders: integration=%s",
            integration.integration_id,
        )

        # connection_id is validated earlier in the flow
        assert integration.connection_id is not None

        # Fetch folders from provider
        folders_data = await fetch_folders(
            nango=nango,
            connection_id=integration.connection_id,
            is_initial=False,  # Always incremental for scheduled sync
            last_synced_at=integration.last_synced_at,
            provider_config_key=provider_key,
        )

        if not folders_data:
            logger.debug(
                "No folders returned from provider: integration=%s",
                integration.integration_id,
            )
            return {"folders_synced": 0}

        # Sync folders to database
        result = await email_service.sync_folders_incremental(
            integration=integration,
            folders=folders_data,
            provider=provider,
        )

        folders_synced = result.get("records_processed", 0)
        logger.debug(
            "Folders synced: integration=%s count=%d",
            integration.integration_id,
            folders_synced,
        )

        return {"folders_synced": folders_synced, "details": result}

    async def _get_folders_to_sync(
        self,
        session: AsyncSession,
        integration_id: UUID,
    ) -> list[EmailFolder]:
        """
        Get folders that should have messages synced.

        Returns folders where is_monitored=True.
        By default, inbox and sent are monitored. Users can enable/disable monitoring.

        Args:
            session: Database session
            integration_id: The integration UUID

        Returns:
            List of EmailFolder objects to sync
        """
        # Note: is_monitored is stored as string "true"/"false" in DB
        stmt = select(EmailFolder).where(
            col(EmailFolder.integration_id) == integration_id,
            col(EmailFolder.is_deleted).is_(False),
            col(EmailFolder.is_monitored) == "true",
        )
        result = await session.execute(stmt)
        folders_to_sync = result.scalars().all()

        logger.info(
            "Folders to sync messages: integration=%s monitored=%d (folders=%s)",
            integration_id,
            len(folders_to_sync),
            [f"{f.folder_name} ({f.folder_type})" for f in folders_to_sync],
        )

        return list(folders_to_sync)

    async def _sync_folder_messages(
        self,
        *,
        nango: NangoClient,
        email_service: EmailSyncService,
        integration: KnowledgeIntegration,
        folder: EmailFolder,
        provider: str,
        provider_key: str,
    ) -> dict[str, Any]:
        """
        Sync messages for a single folder using Delta Query.

        Uses Microsoft Graph Delta Query for efficient incremental sync:
        - Initial sync: delta query with receivedDateTime filter (last 90 days)
        - Incremental: uses stored deltaLink (returns ONLY changes)

        DeltaLinks are stored per-folder in integration.connection_metadata.

        Args:
            nango: Nango client
            email_service: Email sync service
            integration: The integration
            folder: The folder to sync
            provider: Provider name
            provider_key: Nango provider config key

        Returns:
            Dictionary with folder sync results
        """
        folder_name = folder.folder_name or folder.external_folder_id
        external_folder_id = folder.external_folder_id

        # Get stored deltaLink for this folder (if any)
        stored_delta_link = _get_folder_delta_link(integration, external_folder_id)
        is_initial = stored_delta_link is None

        logger.debug(
            "Fetching messages: integration=%s folder=%s (%s) initial=%s",
            integration.integration_id,
            folder_name,
            folder.folder_type,
            is_initial,
        )

        # connection_id is validated earlier in the flow
        assert integration.connection_id is not None

        try:
            # Fetch messages using delta query
            fetch_result: FetchMessagesResult = await fetch_messages(
                nango=nango,
                connection_id=integration.connection_id,
                is_initial=is_initial,
                last_synced_at=folder.last_synced_at or integration.last_synced_at,
                delta_link=stored_delta_link,
                provider_config_key=provider_key,
                folder_id=external_folder_id,
            )

            messages_data = fetch_result.messages
            new_delta_link = fetch_result.delta_link

            # Store new deltaLink for next sync
            if new_delta_link:
                _set_folder_delta_link(integration, external_folder_id, new_delta_link)
                logger.debug(
                    "Stored new deltaLink for folder: integration=%s folder=%s",
                    integration.integration_id,
                    folder_name,
                )

            if not messages_data:
                logger.debug(
                    "No messages returned: integration=%s folder=%s",
                    integration.integration_id,
                    folder_name,
                )
                return {
                    "folder_id": str(folder.folder_id),
                    "folder_name": folder_name,
                    "folder_type": folder.folder_type,
                    "messages_synced": 0,
                    "is_initial": is_initial,
                }

            # Sync messages to database
            result = await email_service.sync_messages_incremental(
                integration=integration,
                messages=messages_data,
                provider=provider,
            )

            messages_synced = result.get("records_processed", 0)

            # Update folder last_synced_at
            folder.last_synced_at = datetime.now(UTC).replace(tzinfo=None)

            logger.info(
                "Messages synced: integration=%s folder=%s count=%d initial=%s",
                integration.integration_id,
                folder_name,
                messages_synced,
                is_initial,
            )

            return {
                "folder_id": str(folder.folder_id),
                "folder_name": folder_name,
                "folder_type": folder.folder_type,
                "messages_synced": messages_synced,
                "is_initial": is_initial,
                "details": result,
            }

        except Exception as e:
            logger.error(
                "Failed to sync folder messages: integration=%s folder=%s error=%s",
                integration.integration_id,
                folder_name,
                str(e),
                exc_info=True,
            )
            return {
                "folder_id": str(folder.folder_id),
                "folder_name": folder_name,
                "folder_type": folder.folder_type,
                "status": "failed",
                "error": str(e),
            }
