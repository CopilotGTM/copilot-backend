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
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import EmailFolder
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.email_sync_service import EmailSyncService
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers.email.outlook_email import (
    FetchMessagesResult,
    fetch_folders,
    fetch_messages,
)

logger = get_logger(__name__)

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
    """

    async def sync_integration(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, Any]:
        """
        Public entry point for single integration sync.

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

        async for session in acquire_org_async_session(organisation_id):
            # Load integration
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_id == integration_id,
                KnowledgeIntegration.integration_type.in_(EMAIL_INTEGRATION_TYPES),
                KnowledgeIntegration.is_active == True,  # noqa: E712
                KnowledgeIntegration.connection_id.isnot(None),
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

            try:
                return await self._sync_integration(
                    session=session,
                    org_id=organisation_id,
                    integration=integration,
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
                    "integration_type": integration.integration_type.value
                    if hasattr(integration.integration_type, "value")
                    else str(integration.integration_type),
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        return {
            "status": "failed",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "error": "Failed to acquire database session",
        }

    async def _sync_integration(
        self,
        session: AsyncSession,
        org_id: UUID,
        integration: KnowledgeIntegration,
    ) -> dict[str, Any]:
        """
        Sync a single email integration.

        Flow:
        1. Fetch and sync folders
        2. Get important/monitored folders from DB
        3. For each folder: sync messages
        4. Return combined results

        Args:
            session: Database session
            org_id: Organization UUID
            integration: The integration to sync

        Returns:
            Dictionary with detailed sync results
        """
        integration_id = integration.integration_id
        integration_type = (
            integration.integration_type.value
            if hasattr(integration.integration_type, "value")
            else str(integration.integration_type)
        )

        logger.info(
            "Syncing email integration: org=%s integration=%s type=%s",
            org_id,
            integration_id,
            integration_type,
        )

        nango = NangoClient()
        email_service = EmailSyncService(session=session, organisation_id=org_id)

        try:
            # Get provider key for API calls
            int_type = (
                IntegrationType(integration.integration_type)
                if isinstance(integration.integration_type, str)
                else integration.integration_type
            )
            provider_key = _get_provider_key(int_type)
            provider = "outlook"  # Currently only Outlook is supported

            # Step 1: Fetch and sync folders
            folders_result = await self._sync_folders(
                nango=nango,
                email_service=email_service,
                integration=integration,
                provider=provider,
                provider_key=provider_key,
            )

            # Step 2: Get folders to sync messages for
            folders_to_sync = await self._get_folders_to_sync(
                session=session,
                integration_id=integration_id,
            )

            if not folders_to_sync:
                logger.info(
                    "No folders to sync messages for: org=%s integration=%s",
                    org_id,
                    integration_id,
                )
                return {
                    "status": "success",
                    "organisation_id": str(org_id),
                    "integration_id": str(integration_id),
                    "integration_type": integration_type,
                    "folders_synced": folders_result.get("folders_synced", 0),
                    "messages_synced": 0,
                    "folder_results": [],
                }

            # Step 3: Sync messages for each folder
            folder_results = []
            total_messages = 0

            for folder in folders_to_sync:
                folder_result = await self._sync_folder_messages(
                    nango=nango,
                    email_service=email_service,
                    integration=integration,
                    folder=folder,
                    provider=provider,
                    provider_key=provider_key,
                )
                folder_results.append(folder_result)
                total_messages += folder_result.get("messages_synced", 0)

            # Update integration last_synced_at and persist deltaLinks
            integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
            # Flag connection_metadata as modified so SQLAlchemy persists deltaLink changes
            attributes.flag_modified(integration, "connection_metadata")
            await session.commit()

            logger.info(
                "Email sync complete: org=%s integration=%s folders=%d messages=%d",
                org_id,
                integration_id,
                len(folders_to_sync),
                total_messages,
            )

            return {
                "status": "success",
                "organisation_id": str(org_id),
                "integration_id": str(integration_id),
                "integration_type": integration_type,
                "folders_synced": folders_result.get("folders_synced", 0),
                "messages_synced": total_messages,
                "folder_results": folder_results,
            }

        finally:
            await nango.close()

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
            EmailFolder.integration_id == integration_id,
            EmailFolder.is_deleted == False,  # noqa: E712
            EmailFolder.is_monitored == "true",
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
