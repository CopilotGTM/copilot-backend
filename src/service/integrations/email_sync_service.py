"""
Email sync service for Outlook email integration.

This service writes to:
1. email_folders - Email folder/label records
2. emails - Individual email message records
3. integration_sync_history - Sync tracking

This service does NOT create OrganisationSource or OrganisationDocument records yet.
Email data is stored in dedicated email tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email, EmailFolder
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def datetime_now_naive() -> datetime:
    """Return current UTC timestamp without timezone info for naive columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class EmailSyncService:
    """
    Service to sync email folders and messages from external providers.

    Scope: This service ONLY modifies email-specific tables.
    - email_folders
    - emails
    - integration_sync_history

    This service does NOT touch:
    - organisation_sources
    - organisations_source_knowledge
    - organisation_documents
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id
        # In-memory cache of external_folder_id -> folder primary key for faster lookups
        self._folder_cache: dict[str, UUID] = {}

    async def sync_folders(
        self,
        *,
        integration: KnowledgeIntegration,
        folders: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """
        Sync email folders from external provider.

        Tables Modified:
        - email_folders (primary)
        - integration_sync_history (tracking)
        - knowledge_integrations (last_synced_at update only)

        Args:
            integration: The integration record
            folders: List of folder dictionaries from provider
            provider: Provider name (outlook, gmail, etc.)

        Returns:
            Dictionary with sync statistics
        """
        # Cache integration_id to avoid lazy-loading after commits expire the object
        integration_id = integration.integration_id

        sync_history = await self._create_sync_history(integration_id)

        try:
            folder_list = list(folders)
            logger.debug(
                "sync_folders started: integration=%s provider=%s folder_count=%d",
                integration_id,
                provider,
                len(folder_list),
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0

            for idx, folder_data in enumerate(folder_list):
                try:
                    external_id = self._extract_folder_id(folder_data, provider)
                    if not external_id:
                        logger.debug("Folder %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Upsert folder to handle race conditions
                    folder = await self._upsert_folder(
                        integration_id, folder_data, provider, external_id
                    )

                    if folder.created_at == folder.updated_at:
                        created_count += 1
                        logger.debug(
                            "Created folder: external_id=%s name=%s",
                            external_id,
                            folder_data.get("displayName"),
                        )
                    else:
                        updated_count += 1
                        logger.debug(
                            "Updated folder: external_id=%s name=%s",
                            external_id,
                            folder_data.get("displayName"),
                        )

                    records_processed += 1

                    # Commit in batches of 50
                    if records_processed % 50 == 0:
                        await self.db.commit()
                        logger.debug("Batch commit: %d folders processed", records_processed)

                except Exception as e:
                    logger.error(
                        "Failed to sync folder %d (external_id=%s): %s",
                        idx,
                        folder_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
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

            logger.debug(
                "Folder sync completed: integration=%s created=%d updated=%d failed=%d",
                integration_id,
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
                "Folder sync failed: integration=%s error=%s",
                integration_id,
                str(e),
                exc_info=True,
            )

            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(folder_list),
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
        folder_id: UUID | None = None,
    ) -> dict[str, Any]:
        """
        Sync email messages from external provider.

        Tables Modified:
        - emails (primary)
        - integration_sync_history (tracking)
        - knowledge_integrations (last_synced_at update only)

        Args:
            integration: The integration record
            messages: List of message dictionaries from provider
            provider: Provider name (outlook, gmail, etc.)
            folder_id: Optional folder ID for folder-specific sync

        Returns:
            Dictionary with sync statistics
        """
        # Cache integration_id to avoid lazy-loading after commits expire the object
        integration_id = integration.integration_id

        sync_history = await self._create_sync_history(integration_id)

        try:
            message_list = list(messages)
            logger.debug(
                "sync_messages started: integration=%s provider=%s message_count=%d folder_id=%s",
                integration_id,
                provider,
                len(message_list),
                folder_id,
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

                    # Upsert message to handle race conditions
                    message = await self._upsert_message(
                        integration_id, message_data, provider, external_id, folder_id
                    )

                    if message.created_at == message.updated_at:
                        created_count += 1
                        logger.debug(
                            "Created message: external_id=%s subject=%s",
                            external_id,
                            message_data.get("subject"),
                        )
                    else:
                        updated_count += 1
                        logger.debug(
                            "Updated message: external_id=%s subject=%s",
                            external_id,
                            message_data.get("subject"),
                        )

                    records_processed += 1

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

            # Final commit (no-op if already committed per message)
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
                    "folder_id": str(folder_id) if folder_id else None,
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.debug(
                "Message sync completed: integration=%s created=%d updated=%d failed=%d",
                integration_id,
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
                integration_id,
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

        logger.debug("Created sync history: sync_id=%s", sync_history.sync_id)
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

        logger.debug(
            "Updated sync history: sync_id=%s status=%s processed=%d failed=%d",
            sync_history.sync_id,
            status.value,
            records_processed,
            records_failed,
        )

    async def _upsert_folder(
        self,
        integration_id: UUID,
        folder_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> EmailFolder:
        """
        Upsert email folder to handle race conditions.

        Uses PostgreSQL ON CONFLICT DO UPDATE to atomically insert or update.
        """
        fields = self._extract_folder_fields(folder_data, provider)

        values = {
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "external_folder_id": external_id,
            "synced_at": datetime.now(UTC),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            **fields,
        }

        stmt = insert(EmailFolder).values(**values)

        # Prepare update values (exclude primary key and immutable fields)
        update_dict = {
            k: v
            for k, v in values.items()
            if k
            not in (
                "folder_id",
                "organisation_id",
                "integration_id",
                "external_folder_id",
                "created_at",
            )
        }

        stmt = stmt.on_conflict_do_update(
            index_elements=["integration_id", "external_folder_id"],
            set_=update_dict,
        ).returning(EmailFolder)

        result = await self.db.execute(stmt)
        folder = result.scalars().one()

        # Update cache
        self._folder_cache[external_id] = folder.folder_id

        return folder

    async def _upsert_message(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        folder_id: UUID | None,
    ) -> Email:
        """
        Upsert email message to handle race conditions.

        Uses PostgreSQL ON CONFLICT DO UPDATE to atomically insert or update.
        """
        fields = self._extract_message_fields(message_data, provider)

        # Resolve folder ID
        resolved_folder_id = await self._ensure_folder_id(
            integration_id,
            message_data,
            provider,
            folder_id,
            external_id,
        )

        values = {
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "external_message_id": external_id,
            "folder_id": resolved_folder_id,
            "synced_at": datetime.now(UTC),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            **fields,
        }

        stmt = insert(Email).values(**values)

        # Prepare update values (exclude primary key and immutable fields)
        update_dict = {
            k: v
            for k, v in values.items()
            if k
            not in (
                "email_id",
                "organisation_id",
                "integration_id",
                "external_message_id",
                "created_at",
            )
        }

        stmt = stmt.on_conflict_do_update(
            index_elements=["integration_id", "external_message_id"],
            set_=update_dict,
        ).returning(Email)

        result = await self.db.execute(stmt)
        return result.scalars().one()

    async def _get_folder_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> EmailFolder | None:
        """Get existing email folder by external_id."""
        stmt = select(EmailFolder).where(
            EmailFolder.integration_id == integration_id,  # type: ignore[arg-type]
            EmailFolder.external_folder_id == external_id,  # type: ignore[arg-type]
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    @staticmethod
    def _extract_folder_id(folder_data: dict[str, Any], _provider: str) -> str | None:
        """Extract external ID from folder data."""
        return folder_data.get("id")

    @staticmethod
    def _extract_message_id(message_data: dict[str, Any], _provider: str) -> str | None:
        """Extract external ID from message data."""
        return message_data.get("id")

    @classmethod
    def _extract_folder_fields(cls, folder_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract folder fields from provider data."""
        if provider in ("microsoft", "outlook"):
            return cls._extract_outlook_folder_fields(folder_data)
        return cls._extract_generic_folder_fields(folder_data)

    @classmethod
    def _extract_outlook_folder_fields(cls, folder_data: dict[str, Any]) -> dict[str, Any]:
        """Extract folder fields from Outlook/Microsoft data."""
        return {
            "folder_name": folder_data.get("displayName", "Unnamed Folder"),
            "folder_path": None,  # Outlook doesn't provide full path in single API call
            "folder_type": cls._classify_folder_type(folder_data.get("displayName", "")),
            "is_monitored": "false",  # Default; can be configured later (stored as string in DB)
            "total_count": folder_data.get("totalItemCount", 0),
            "unread_count": folder_data.get("unreadItemCount", 0),
            "folder_metadata": {
                "parent_folder_id": folder_data.get("parentFolderId"),
                "child_folder_count": folder_data.get("childFolderCount", 0),
                "is_hidden": folder_data.get("isHidden", False),
            },
        }

    @staticmethod
    def _classify_folder_type(folder_name: str) -> str:
        """Classify folder type based on name."""
        name_lower = folder_name.lower()
        if name_lower == "inbox":
            return "inbox"
        elif name_lower in ("sent items", "sent"):
            return "sent"
        elif name_lower in ("drafts",):
            return "drafts"
        elif name_lower in ("deleted items", "trash"):
            return "trash"
        elif name_lower in ("archive",):
            return "archive"
        return "user"

    @staticmethod
    def _extract_generic_folder_fields(folder_data: dict[str, Any]) -> dict[str, Any]:
        """Extract folder fields from generic provider data."""
        return {
            "folder_name": folder_data.get("name") or folder_data.get("displayName", "Unnamed"),
            "folder_path": None,
            "folder_type": "user",
            "is_monitored": "false",  # Stored as string in DB
            "total_count": 0,
            "unread_count": 0,
            "folder_metadata": {},
        }

    @classmethod
    def _extract_message_fields(cls, message_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract message fields from provider data."""
        if provider in ("microsoft", "outlook"):
            return cls._extract_outlook_message_fields(message_data)
        return cls._extract_generic_message_fields(message_data)

    @classmethod
    def _extract_outlook_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from Outlook/Microsoft data."""
        from_address = message_data.get("from", {}).get("emailAddress", {})

        # Extract recipients
        to_recipients = message_data.get("toRecipients", [])
        cc_recipients = message_data.get("ccRecipients", [])
        bcc_recipients = message_data.get("bccRecipients", [])

        # Extract body
        body = message_data.get("body", {})
        body_type = body.get("contentType", "text")
        body_content = body.get("content", "")

        return {
            "thread_id": message_data.get("conversationId"),
            "internet_message_id": message_data.get("internetMessageId"),
            "in_reply_to": None,  # Would need full headers
            "from_email": from_address.get("address", ""),
            "from_name": from_address.get("name"),
            "to_emails": [r.get("emailAddress", {}).get("address") for r in to_recipients],
            "cc_emails": [r.get("emailAddress", {}).get("address") for r in cc_recipients],
            "bcc_emails": [r.get("emailAddress", {}).get("address") for r in bcc_recipients],
            "subject": message_data.get("subject"),
            "body_text": body_content if body_type == "text" else None,
            "body_html": body_content if body_type == "html" else None,
            "snippet": message_data.get("bodyPreview"),
            "received_at": cls._parse_iso_datetime(message_data.get("receivedDateTime")),
            "sent_at": cls._parse_iso_datetime(message_data.get("sentDateTime")),
            "is_read": "true" if message_data.get("isRead", False) else "false",
            "is_starred": "false",  # Outlook uses flag instead (stored as string in DB)
            "is_important": "true" if message_data.get("importance") == "high" else "false",
            "is_draft": "true" if message_data.get("isDraft", False) else "false",
            "has_attachments": "true" if message_data.get("hasAttachments", False) else "false",
            "labels": message_data.get("categories", []),
            "email_metadata": {
                "importance": message_data.get("importance"),
                "flag": message_data.get("flag"),
                "inference_classification": message_data.get("inferenceClassification"),
                "internet_message_id": message_data.get("internetMessageId"),
                "parent_folder_id": message_data.get("parentFolderId"),
            },
        }

    @staticmethod
    def _extract_generic_message_fields(message_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from generic provider data."""
        return {
            "thread_id": message_data.get("threadId"),
            "internet_message_id": message_data.get("internetMessageId")
            or message_data.get("messageId"),
            "from_email": message_data.get("from", ""),
            "from_name": None,
            "to_emails": message_data.get("to", []),
            "cc_emails": message_data.get("cc", []),
            "bcc_emails": message_data.get("bcc", []),
            "subject": message_data.get("subject"),
            "body_text": message_data.get("body"),
            "body_html": None,
            "snippet": message_data.get("snippet"),
            "received_at": datetime.now(UTC),
            "sent_at": None,
            "is_read": "true" if message_data.get("isRead", False) else "false",
            "is_starred": "true" if message_data.get("isStarred", False) else "false",
            "is_important": "false",  # Stored as string in DB
            "is_draft": "false",  # Stored as string in DB
            "has_attachments": "false",  # Stored as string in DB
            "labels": [],
            "email_metadata": {},
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

    async def _ensure_folder_id(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        existing_folder_id: UUID | None,
        external_message_id: str,
    ) -> UUID:
        """Ensure we have a folder UUID for this message, resolving from external ID if needed."""
        if existing_folder_id:
            return existing_folder_id

        external_folder_id = self._extract_external_folder_id_from_message(message_data, provider)
        resolved = await self._resolve_folder_id(integration_id, external_folder_id)
        if resolved:
            return resolved

        if existing_folder_id:
            return existing_folder_id

        raise ValueError(
            "Unable to resolve folder for message "
            f"{external_message_id} (provider={provider}, external_folder_id={external_folder_id})"
        )

    async def _resolve_folder_id(
        self,
        integration_id: UUID,
        external_folder_id: str | None,
    ) -> UUID | None:
        """Resolve folder UUID using provider-specific identifiers."""
        if not external_folder_id:
            return None

        cached_id = self._folder_cache.get(external_folder_id)
        if cached_id:
            return cached_id

        folder = await self._get_folder_by_external_id(integration_id, external_folder_id)
        if folder:
            self._folder_cache[external_folder_id] = folder.folder_id
            return folder.folder_id
        return None

    @staticmethod
    def _extract_external_folder_id_from_message(
        message_data: dict[str, Any],
        provider: str,
    ) -> str | None:
        """Extract provider-specific folder identifier from message payload."""
        if provider in ("microsoft", "outlook"):
            return message_data.get("parentFolderId") or message_data.get("parent_folder_id")
        return message_data.get("folderId")


__all__ = ["EmailSyncService"]
