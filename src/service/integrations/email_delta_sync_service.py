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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import attributes
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import EmailFolder
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.email_sync_service import EmailSyncService
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.email import gmail_email, outlook_email

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
    credentials: dict[str, Any]
    access_token_expires_at: datetime | None


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
    IntegrationType.GMAIL,
)

# Note: Folder monitoring is controlled by is_monitored flag in email_folders table
# By default, inbox and sent folders are set to is_monitored=True
# Users can enable/disable monitoring for any folder

# Cursor storage keys in connection_metadata
EMAIL_DELTA_LINKS_KEY = "email_folder_delta_links"  # Outlook: per-folder delta links
GMAIL_HISTORY_ID_KEY = "gmail_history_id"  # Gmail: single global historyId

_PROVIDER_MAP: dict[IntegrationType, str] = {
    IntegrationType.OUTLOOK_EMAIL: "outlook",
    IntegrationType.GMAIL: "gmail",
}


def _get_provider_key(integration_type: IntegrationType) -> str:
    """Get provider key for integration type."""
    return _PROVIDER_MAP.get(integration_type, "outlook")


def _get_cursor_for_folder(
    sync_data: IntegrationSyncData,
    external_folder_id: str,
) -> str | None:
    """Get the incremental sync cursor for a folder.

    Outlook: per-folder deltaLink from email_folder_delta_links.{folder_id}
    Gmail: single global historyId shared across all labels
    """
    metadata = sync_data.connection_metadata or {}
    if sync_data.provider == "gmail":
        return metadata.get(GMAIL_HISTORY_ID_KEY)
    return metadata.get(EMAIL_DELTA_LINKS_KEY, {}).get(external_folder_id)


def _persist_cursors(
    integration: KnowledgeIntegration,
    provider: str,
    cursor_updates: dict[str, str | None],
) -> None:
    """Write cursor updates back to connection_metadata.

    Outlook: stores per-folder delta links
    Gmail: stores the highest historyId (global)
    """
    if integration.connection_metadata is None:
        integration.connection_metadata = {}

    if provider == "gmail":
        # Take the highest historyId across all label syncs
        max_hid = 0
        for val in cursor_updates.values():
            if val and val.isdigit():
                max_hid = max(max_hid, int(val))
        if max_hid:
            integration.connection_metadata[GMAIL_HISTORY_ID_KEY] = str(max_hid)
    else:
        # Outlook: per-folder delta links
        if EMAIL_DELTA_LINKS_KEY not in integration.connection_metadata:
            integration.connection_metadata[EMAIL_DELTA_LINKS_KEY] = {}
        persisted_count = 0
        for folder_id, delta_link in cursor_updates.items():
            if delta_link:
                integration.connection_metadata[EMAIL_DELTA_LINKS_KEY][folder_id] = delta_link
                persisted_count += 1
        logger.info(
            "Delta links persisted to connection_metadata",
            extra={
                "event": "email_delta_links_persisted",
                "integration_id": str(integration.integration_id),
                "persisted": persisted_count,
                "total": len(cursor_updates),
            },
        )


async def _fetch_folders_for_provider(
    *,
    sync_data: IntegrationSyncData,
    token_info: IntegrationTokenInfo,
) -> list[dict[str, Any]]:
    """Route folder fetch to the correct provider."""
    if sync_data.provider == "gmail":
        return await gmail_email.fetch_labels(
            token_info=token_info,
            connection_id=sync_data.connection_id,
            is_initial=False,
            last_synced_at=sync_data.last_synced_at,
            provider_config_key=sync_data.provider_key,
        )
    if sync_data.provider == "outlook":
        return await outlook_email.fetch_folders(
            token_info=token_info,
            connection_id=sync_data.connection_id,
            is_initial=False,
            last_synced_at=sync_data.last_synced_at,
            provider_config_key=sync_data.provider_key,
        )
    logger.warning(
        "Unsupported email provider for folder fetch",
        extra={
            "event": "email_unsupported_provider_folder",
            "provider": sync_data.provider,
            "integration_id": str(sync_data.integration_id),
        },
    )
    return []


async def _fetch_messages_for_provider(
    *,
    sync_data: IntegrationSyncData,
    folder_data: FolderSyncData,
    token_info: IntegrationTokenInfo,
) -> tuple[list[dict[str, Any]], str | None]:
    """Route message fetch to the correct provider.

    Returns (messages, cursor) where cursor is either a deltaLink or historyId.
    """
    is_initial = folder_data.delta_link is None

    if sync_data.provider == "gmail":
        result = await gmail_email.fetch_messages(
            token_info=token_info,
            connection_id=sync_data.connection_id,
            is_initial=is_initial,
            last_synced_at=folder_data.last_synced_at,
            history_id=folder_data.delta_link,
            provider_config_key=sync_data.provider_key,
            label_id=folder_data.external_folder_id,
        )
        return result.messages, result.history_id

    if sync_data.provider == "outlook":
        result = await outlook_email.fetch_messages(
            token_info=token_info,
            connection_id=sync_data.connection_id,
            is_initial=is_initial,
            last_synced_at=folder_data.last_synced_at,
            delta_link=folder_data.delta_link,
            provider_config_key=sync_data.provider_key,
            folder_id=folder_data.external_folder_id,
        )
        return result.messages, result.delta_link

    logger.warning(
        "Unsupported email provider for message fetch",
        extra={
            "event": "email_unsupported_provider_message",
            "provider": sync_data.provider,
            "integration_id": str(sync_data.integration_id),
        },
    )
    return [], None


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
            "Email sync started",
            extra={
                "event": "email_sync_started",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
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
                    "Email integration not found or inactive",
                    extra={
                        "event": "email_integration_not_found",
                        "organisation_id": str(organisation_id),
                        "integration_id": str(integration_id),
                    },
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
                provider=_PROVIDER_MAP.get(int_type, "outlook"),
                provider_key=_get_provider_key(int_type),
                last_synced_at=integration.last_synced_at,
                connection_metadata=dict(integration.connection_metadata or {}),
                credentials=dict(integration.credentials or {}),
                access_token_expires_at=integration.access_token_expires_at,
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
                "Email sync failed",
                extra={
                    "event": "email_sync_failed",
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
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
            "Syncing email integration",
            extra={
                "event": "email_integration_sync_started",
                "organisation_id": str(org_id),
                "integration_id": str(sync_data.integration_id),
                "integration_type": sync_data.integration_type_value,
            },
        )

        token_info = IntegrationTokenInfo.from_integration(sync_data, org_id)

        # ─────────────────────────────────────────────────────────────────
        # Phase 2: Fetch folders from external API (NO session held)
        # Folders rarely change — skip if synced within the last 24 hours
        # ─────────────────────────────────────────────────────────────────
        folders_data: list[dict[str, Any]] = []
        folder_refresh_interval = timedelta(hours=24)
        folder_sync_due = (
            sync_data.last_synced_at is None
            or (datetime.now(UTC).replace(tzinfo=None) - sync_data.last_synced_at)
            >= folder_refresh_interval
        )

        if folder_sync_due:
            logger.debug(
                "Fetching folders: integration=%s",
                sync_data.integration_id,
            )
            folders_data = await _fetch_folders_for_provider(
                sync_data=sync_data,
                token_info=token_info,
            )
        else:
            logger.debug(
                "Skipping folder fetch (last synced %s): integration=%s",
                sync_data.last_synced_at,
                sync_data.integration_id,
            )

        # ─────────────────────────────────────────────────────────────────
        # Phase 3: Write folders to DB + get monitored folders
        # Fresh session acquired and released in this block
        # ─────────────────────────────────────────────────────────────────
        folders_synced = 0
        folders_to_sync: list[FolderSyncData] = []
        delta_link_updates: dict[str, str | None] = {}

        async for session in acquire_org_async_session(org_id):
            if folders_data:
                email_service = EmailSyncService(session=session, organisation_id=org_id)
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

            stmt = select(EmailFolder).where(
                col(EmailFolder.integration_id) == sync_data.integration_id,
                col(EmailFolder.is_deleted).is_(False),
                col(EmailFolder.is_monitored) == "true",
            )
            result = await session.execute(stmt)
            monitored_folders = result.scalars().all()

            for folder in monitored_folders:
                cursor = _get_cursor_for_folder(sync_data, folder.external_folder_id)
                folders_to_sync.append(
                    FolderSyncData(
                        folder_id=folder.folder_id,
                        external_folder_id=folder.external_folder_id,
                        folder_name=folder.folder_name,
                        folder_type=folder.folder_type,
                        last_synced_at=folder.last_synced_at or sync_data.last_synced_at,
                        delta_link=cursor,
                    )
                )
        # Session released

        logger.info(
            "Folders to sync messages",
            extra={
                "event": "email_monitored_folders_resolved",
                "integration_id": str(sync_data.integration_id),
                "monitored_count": len(folders_to_sync),
                "folders": [f"{f.folder_name} ({f.folder_type})" for f in folders_to_sync],
            },
        )

        if not folders_to_sync:
            logger.info(
                "No monitored folders to sync",
                extra={
                    "event": "email_no_monitored_folders",
                    "organisation_id": str(org_id),
                    "integration_id": str(sync_data.integration_id),
                },
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
                token_info=token_info,
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
                if delta_link_updates:
                    logger.info(
                        "Persisting delta links",
                        extra={
                            "event": "email_delta_links_persisting",
                            "integration_id": str(sync_data.integration_id),
                            "folder_count": len(delta_link_updates),
                            "folder_ids": list(delta_link_updates.keys()),
                        },
                    )
                    _persist_cursors(integration, sync_data.provider, delta_link_updates)
                else:
                    logger.warning(
                        "No delta links to persist — all folders returned None cursor",
                        extra={
                            "event": "email_no_delta_links",
                            "integration_id": str(sync_data.integration_id),
                        },
                    )

                integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                attributes.flag_modified(integration, "connection_metadata")
                await session.commit()

                stored = (integration.connection_metadata or {}).get(EMAIL_DELTA_LINKS_KEY, {})
                logger.info(
                    "Delta links committed",
                    extra={
                        "event": "email_delta_links_committed",
                        "integration_id": str(sync_data.integration_id),
                        "stored_folder_count": len(stored),
                    },
                )
        # Session released

        logger.info(
            "Email sync complete",
            extra={
                "event": "email_sync_completed",
                "organisation_id": str(org_id),
                "integration_id": str(sync_data.integration_id),
                "folders_synced": len(folders_to_sync),
                "messages_synced": total_messages,
            },
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

    async def _sync_folder_messages_fresh_session(
        self,
        *,
        token_info: IntegrationTokenInfo,
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
            messages_data, new_cursor = await _fetch_messages_for_provider(
                sync_data=sync_data,
                folder_data=folder_data,
                token_info=token_info,
            )

            # Track cursor for later persistence (Phase 5 will write to DB)
            if new_cursor:
                delta_link_updates[folder_data.external_folder_id] = new_cursor
                logger.info(
                    "Delta link captured for folder",
                    extra={
                        "event": "email_folder_delta_link_captured",
                        "folder_name": folder_name,
                        "external_folder_id": folder_data.external_folder_id,
                        "cursor_length": len(new_cursor),
                    },
                )
            else:
                logger.warning(
                    "No delta link returned for folder — next sync will be full re-fetch",
                    extra={
                        "event": "email_folder_no_delta_link",
                        "folder_name": folder_name,
                        "external_folder_id": folder_data.external_folder_id,
                    },
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
                        "Integration no longer exists",
                        extra={
                            "event": "email_integration_missing",
                            "integration_id": str(sync_data.integration_id),
                        },
                    )
                    return {
                        "folder_id": str(folder_data.folder_id),
                        "folder_name": folder_name,
                        "folder_type": folder_data.folder_type,
                        "status": "failed",
                        "error": "Integration no longer exists",
                    }

                # Full sync uses always-upsert to ensure field-parsing fixes
                # are applied; incremental sync uses change detection to skip
                # unchanged messages
                if is_initial:
                    sync_result = await email_service.sync_messages(
                        integration=integration,
                        messages=messages_data,
                        provider=sync_data.provider,
                    )
                else:
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
                "Folder messages synced",
                extra={
                    "event": "email_folder_messages_synced",
                    "integration_id": str(sync_data.integration_id),
                    "folder_name": folder_name,
                    "messages_synced": messages_synced,
                    "is_initial": is_initial,
                },
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
                "Failed to sync folder messages",
                extra={
                    "event": "email_folder_messages_sync_failed",
                    "integration_id": str(sync_data.integration_id),
                    "folder_name": folder_name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            return {
                "folder_id": str(folder_data.folder_id),
                "folder_name": folder_name,
                "folder_type": folder_data.folder_type,
                "status": "failed",
                "error": str(e),
            }
