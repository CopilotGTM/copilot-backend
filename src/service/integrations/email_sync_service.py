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

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email, EmailFolder
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncActionCounts,
    extract_sync_action,
)

logger = get_logger(__name__)


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
        self._sync_history = SyncHistoryManager(session)
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

        sync_history = await self._sync_history.start(integration_id)

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

            await self._sync_history.complete(
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

        sync_history = await self._sync_history.start(integration_id)

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
            await self._sync_history.complete(
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
            col(EmailFolder.integration_id) == integration_id,
            col(EmailFolder.external_folder_id) == external_id,
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
        if provider in ("gmail", "google"):
            return cls._extract_gmail_folder_fields(folder_data)
        return cls._extract_generic_folder_fields(folder_data)

    @classmethod
    def _extract_outlook_folder_fields(cls, folder_data: dict[str, Any]) -> dict[str, Any]:
        """Extract folder fields from Outlook/Microsoft data."""
        folder_type = cls._classify_folder_type(folder_data.get("displayName", ""))
        # Only monitor inbox and sent folders by default
        # is_monitored is stored as string in DB ("true"/"false")
        is_monitored = "true" if folder_type in cls.MONITORED_FOLDER_TYPES else "false"
        return {
            "folder_name": folder_data.get("displayName", "Unnamed Folder"),
            "folder_path": None,  # Outlook doesn't provide full path in single API call
            "folder_type": folder_type,
            "is_monitored": is_monitored,
            "total_count": folder_data.get("totalItemCount", 0),
            "unread_count": folder_data.get("unreadItemCount", 0),
            "folder_metadata": {
                "parent_folder_id": folder_data.get("parentFolderId"),
                "child_folder_count": folder_data.get("childFolderCount", 0),
                "is_hidden": folder_data.get("isHidden", False),
            },
        }

    # Folder types that should be monitored (messages synced)
    MONITORED_FOLDER_TYPES = ("inbox", "sent")

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
        elif name_lower in ("junk email", "junk", "spam"):
            return "junk"
        elif name_lower in ("archive",):
            return "archive"
        return "user"

    # Gmail label ID -> folder_type mapping
    _GMAIL_LABEL_TYPE_MAP: dict[str, str] = {
        "INBOX": "inbox",
        "SENT": "sent",
        "DRAFT": "drafts",
        "TRASH": "trash",
        "SPAM": "junk",
        "STARRED": "starred",
        "IMPORTANT": "important",
    }

    @classmethod
    def _extract_gmail_folder_fields(cls, label_data: dict[str, Any]) -> dict[str, Any]:
        """Extract folder fields from Gmail label data."""
        label_id = label_data.get("id", "")
        label_name = label_data.get("name", label_id)

        # System labels use ID for classification; user labels fall through to "user"
        folder_type = cls._GMAIL_LABEL_TYPE_MAP.get(label_id, "user")
        # Category labels (CATEGORY_PROMOTIONS, etc.)
        if label_id.startswith("CATEGORY_"):
            folder_type = "category"

        is_monitored = "true" if folder_type in cls.MONITORED_FOLDER_TYPES else "false"

        return {
            "folder_name": label_name,
            "folder_path": None,
            "folder_type": folder_type,
            "is_monitored": is_monitored,
            "total_count": label_data.get("messagesTotal", 0),
            "unread_count": label_data.get("messagesUnread", 0),
            "folder_metadata": {
                "label_type": label_data.get("type"),
                "label_list_visibility": label_data.get("labelListVisibility"),
                "message_list_visibility": label_data.get("messageListVisibility"),
            },
        }

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
        if provider in ("gmail", "google"):
            return cls._extract_gmail_message_fields(message_data)
        return cls._extract_generic_message_fields(message_data)

    @classmethod
    def _extract_outlook_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from Outlook/Microsoft data."""
        from_address = message_data.get("from", {}).get("emailAddress", {})

        # Extract recipients
        to_recipients = message_data.get("toRecipients", [])
        cc_recipients = message_data.get("ccRecipients", [])
        bcc_recipients = message_data.get("bccRecipients", [])

        # Extract body; generate plain text from HTML when the provider only sends HTML
        body = message_data.get("body", {})
        body_type = body.get("contentType", "text")
        body_content = body.get("content", "")

        body_text = body_content if body_type == "text" else None
        body_html = body_content if body_type == "html" else None
        if not body_text and body_html:
            body_text = cls._html_to_text(body_html)

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
            "body_text": body_text,
            "body_html": body_html,
            "snippet": message_data.get("bodyPreview"),
            "received_at": cls._parse_datetime(message_data.get("receivedDateTime")),
            "sent_at": cls._parse_datetime(message_data.get("sentDateTime")),
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

    @classmethod
    def _extract_gmail_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from Gmail API message (format=full)."""
        from src.workers.integrations_worker.providers.email.gmail_email import (
            extract_body_from_payload,
            get_header,
        )

        headers = message_data.get("payload", {}).get("headers", [])
        payload = message_data.get("payload", {})

        # Parse sender: "Display Name <email@example.com>" or just "email@example.com"
        from_raw = get_header(headers, "From") or ""
        from_email, from_name = _parse_email_address(from_raw)

        # Parse recipients
        to_emails = _parse_email_list(get_header(headers, "To"))
        cc_emails = _parse_email_list(get_header(headers, "Cc"))
        bcc_emails = _parse_email_list(get_header(headers, "Bcc"))

        # Body from recursive MIME parsing; generate plain text from HTML when missing
        body_text, body_html = extract_body_from_payload(payload)
        if not body_text and body_html:
            body_text = cls._html_to_text(body_html)

        # received_at from internalDate (Unix milliseconds)
        internal_date_ms = message_data.get("internalDate")
        received_at = (
            datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=UTC)
            if internal_date_ms
            else datetime.now(UTC)
        )

        # sent_at from Date header
        sent_at = cls._parse_datetime(get_header(headers, "Date"))

        label_ids = message_data.get("labelIds", [])
        label_set = set(label_ids)

        has_attachments = any(
            part.get("filename") for part in payload.get("parts", []) if part.get("filename")
        )

        return {
            "thread_id": message_data.get("threadId"),
            "internet_message_id": get_header(headers, "Message-ID"),
            "in_reply_to": get_header(headers, "In-Reply-To"),
            "from_email": from_email,
            "from_name": from_name,
            "to_emails": to_emails,
            "cc_emails": cc_emails,
            "bcc_emails": bcc_emails,
            "subject": get_header(headers, "Subject"),
            "body_text": body_text,
            "body_html": body_html,
            "snippet": message_data.get("snippet"),
            "received_at": received_at,
            "sent_at": sent_at,
            "is_read": "false" if "UNREAD" in label_set else "true",
            "is_starred": "true" if "STARRED" in label_set else "false",
            "is_important": "true" if "IMPORTANT" in label_set else "false",
            "is_draft": "true" if "DRAFT" in label_set else "false",
            "has_attachments": "true" if has_attachments else "false",
            "labels": label_ids,
            "email_metadata": {
                "gmail_id": message_data.get("id"),
                "history_id": message_data.get("historyId"),
                "size_estimate": message_data.get("sizeEstimate"),
                "label_ids": label_ids,
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
    def _parse_datetime(value: str | None) -> datetime | None:
        """Parse a datetime string (ISO 8601 or RFC 2822) to a tz-aware datetime.

        Outlook sends ISO 8601 (sentDateTime); Gmail sends RFC 2822 (Date header).
        """
        if not value:
            return None
        # ISO 8601 first (Outlook)
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except (ValueError, AttributeError):
            pass
        # RFC 2822 fallback (Gmail Date header, e.g. "Sun, 16 Feb 2026 10:30:45 +0530")
        try:
            return parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert HTML body to plain text by stripping tags.

        Used as fallback when the email provider only returns HTML
        (most modern emails omit the text/plain MIME part).
        """
        # Remove <style> and <script> blocks entirely
        text = re.sub(
            r"<(style|script)[^>]*>.*?</\1>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Block-level elements → newlines
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode common HTML entities
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"')
        # Collapse excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

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
        if provider in ("gmail", "google"):
            # Injected by gmail_email.py during fetch to track which label the message came from
            return message_data.get("_gmail_label_id")
        return message_data.get("folderId")

    # -------------------------------------------------------------------------
    # Incremental Sync Methods
    # -------------------------------------------------------------------------

    async def sync_folders_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        folders: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """
        Sync email folders using incremental optimization.

        This method optimizes sync by:
        - Using database state to determine if records are new or existing
        - Running change detection to skip unchanged folders
        - Handling deleted folders via soft-delete

        Args:
            integration: KnowledgeIntegration instance
            folders: Raw folder records with optional _sync_metadata
            provider: Provider name (outlook, gmail, etc.)

        Returns:
            Dictionary with sync statistics including skipped count
        """
        integration_id = integration.integration_id
        sync_history = await self._sync_history.start(integration_id)

        try:
            folder_list = list(folders)
            logger.debug(
                "sync_folders_incremental started: integration=%s provider=%s folder_count=%d",
                integration_id,
                provider,
                len(folder_list),
            )

            if not folder_list:
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

            # Track action counts for logging (observability only)
            action_counts = SyncActionCounts()

            # Load existing folders for change detection
            folder_external_ids = [
                self._extract_folder_id(f, provider)
                for f in folder_list
                if self._extract_folder_id(f, provider)
            ]
            existing = await self._load_existing_folders_by_external_ids(
                integration_id, folder_external_ids
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0

            for idx, folder_data in enumerate(folder_list):
                try:
                    external_id = self._extract_folder_id(folder_data, provider)
                    if not external_id:
                        logger.debug("Folder %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Extract sync action for logging and deletion handling
                    sync_action = extract_sync_action(folder_data)
                    if sync_action:
                        action_counts.increment(sync_action)

                    # Check database state - this is the source of truth
                    record = existing.get(external_id)
                    is_new = record is None

                    # Handle deleted folders
                    if sync_action == SyncAction.DELETED:
                        # Folders don't have a soft-delete flag, skip deleted ones
                        logger.debug("Skipping deleted folder: external_id=%s", external_id)
                        continue

                    # Handle new records
                    if is_new:
                        folder = await self._upsert_folder(
                            integration_id, folder_data, provider, external_id
                        )
                        records_processed += 1
                        created_count += 1
                        logger.debug(
                            "Created folder: external_id=%s name=%s",
                            external_id,
                            folder.folder_name,
                        )
                    else:
                        # Existing record - check for meaningful changes
                        if self._has_meaningful_folder_changes(record, folder_data, provider):
                            folder = await self._upsert_folder(
                                integration_id, folder_data, provider, external_id
                            )
                            records_processed += 1
                            updated_count += 1
                            logger.debug(
                                "Updated folder: external_id=%s name=%s",
                                external_id,
                                folder.folder_name,
                            )
                        else:
                            # No meaningful changes - skip database write
                            skipped_count += 1
                            action_counts.unchanged += 1
                            logger.debug(
                                "Skipping unchanged folder: external_id=%s",
                                external_id,
                            )
                            continue

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

            logger.debug(
                "Folder incremental sync completed: integration=%s created=%d updated=%d skipped=%d failed=%d",
                integration_id,
                created_count,
                updated_count,
                skipped_count,
                records_failed,
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "action_counts": action_counts.to_dict(),
            }

        except Exception as e:
            logger.error(
                "Folder incremental sync failed: integration=%s error=%s",
                integration_id,
                str(e),
                exc_info=True,
            )

            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(folder_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def sync_messages_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        messages: Iterable[dict[str, Any]],
        provider: str,
        folder_id: UUID | None = None,
    ) -> dict[str, Any]:
        """
        Sync email messages using incremental optimization.

        This method optimizes sync by:
        - Using database state to determine if records are new or existing
        - Running change detection to skip unchanged messages
        - Handling deleted messages

        Args:
            integration: KnowledgeIntegration instance
            messages: Raw message records with optional _sync_metadata
            provider: Provider name (outlook, gmail, etc.)
            folder_id: Optional folder ID for folder-specific sync

        Returns:
            Dictionary with sync statistics including skipped count
        """
        integration_id = integration.integration_id
        sync_history = await self._sync_history.start(integration_id)

        try:
            message_list = list(messages)
            logger.debug(
                "sync_messages_incremental started: integration=%s provider=%s message_count=%d folder_id=%s",
                integration_id,
                provider,
                len(message_list),
                folder_id,
            )

            if not message_list:
                await self._sync_history.complete(
                    sync_history,
                    status=SyncStatus.SUCCESS,
                    records_processed=0,
                    records_failed=0,
                    metadata={
                        "provider": provider,
                        "skipped": 0,
                        "folder_id": str(folder_id) if folder_id else None,
                    },
                )
                return {
                    "status": "success",
                    "records_processed": 0,
                    "records_failed": 0,
                    "created": 0,
                    "updated": 0,
                    "skipped": 0,
                }

            # Track action counts for logging (observability only)
            action_counts = SyncActionCounts()

            # Load existing messages for change detection
            message_external_ids = [
                self._extract_message_id(m, provider)
                for m in message_list
                if self._extract_message_id(m, provider)
            ]
            existing = await self._load_existing_messages_by_external_ids(
                integration_id, message_external_ids
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0

            for idx, message_data in enumerate(message_list):
                try:
                    external_id = self._extract_message_id(message_data, provider)
                    if not external_id:
                        logger.debug("Message %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Extract sync action for logging and deletion handling
                    sync_action = extract_sync_action(message_data)
                    if sync_action:
                        action_counts.increment(sync_action)

                    # Check database state - this is the source of truth
                    record = existing.get(external_id)
                    is_new = record is None

                    # Handle deleted messages
                    if sync_action == SyncAction.DELETED:
                        # Skip deleted messages (we don't store them)
                        logger.debug("Skipping deleted message: external_id=%s", external_id)
                        continue

                    # Handle new records
                    if is_new:
                        message = await self._upsert_message(
                            integration_id, message_data, provider, external_id, folder_id
                        )
                        records_processed += 1
                        created_count += 1
                        logger.debug(
                            "Created message: external_id=%s subject=%s",
                            external_id,
                            message.subject,
                        )
                    else:
                        # Existing record - check for meaningful changes
                        if self._has_meaningful_message_changes(record, message_data, provider):
                            message = await self._upsert_message(
                                integration_id, message_data, provider, external_id, folder_id
                            )
                            records_processed += 1
                            updated_count += 1
                            logger.debug(
                                "Updated message: external_id=%s subject=%s",
                                external_id,
                                message.subject,
                            )
                        else:
                            # No meaningful changes - skip database write
                            skipped_count += 1
                            action_counts.unchanged += 1
                            logger.debug(
                                "Skipping unchanged message: external_id=%s",
                                external_id,
                            )
                            continue

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
                    "folder_id": str(folder_id) if folder_id else None,
                    "action_counts": action_counts.to_dict(),
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.debug(
                "Message incremental sync completed: integration=%s created=%d updated=%d skipped=%d failed=%d",
                integration_id,
                created_count,
                updated_count,
                skipped_count,
                records_failed,
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "action_counts": action_counts.to_dict(),
            }

        except Exception as e:
            logger.error(
                "Message incremental sync failed: integration=%s error=%s",
                integration_id,
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

            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def _load_existing_folders_by_external_ids(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, EmailFolder]:
        """Load existing email folders by external IDs for change detection.

        Args:
            integration_id: Integration ID
            external_ids: List of external IDs to look up

        Returns:
            Dictionary mapping external_folder_id -> EmailFolder
        """
        if not external_ids:
            return {}

        stmt = select(EmailFolder).where(
            col(EmailFolder.integration_id) == integration_id,
            col(EmailFolder.external_folder_id).in_(external_ids),
        )
        result = await self.db.execute(stmt)
        folders = result.scalars().all()

        return {f.external_folder_id: f for f in folders}

    async def _load_existing_messages_by_external_ids(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, Email]:
        """Load existing email messages by external IDs for change detection.

        Args:
            integration_id: Integration ID
            external_ids: List of external IDs to look up

        Returns:
            Dictionary mapping external_message_id -> Email
        """
        if not external_ids:
            return {}

        stmt = select(Email).where(
            col(Email.integration_id) == integration_id,
            col(Email.external_message_id).in_(external_ids),
        )
        result = await self.db.execute(stmt)
        messages = result.scalars().all()

        return {m.external_message_id: m for m in messages}

    def _has_meaningful_folder_changes(
        self,
        existing: EmailFolder,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming folder data has meaningful changes.

        Args:
            existing: Existing EmailFolder record from database
            incoming: Incoming folder data from provider
            provider: Provider name (outlook, gmail, etc.)

        Returns:
            True if meaningful changes detected, False otherwise
        """
        fields = self._extract_folder_fields(incoming, provider)

        # Compare key fields
        checks = [
            (existing.folder_name, fields.get("folder_name")),
            (existing.folder_type, fields.get("folder_type")),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        # Compare counts (meaningful for folder monitoring)
        incoming_total = fields.get("total_count", 0)
        incoming_unread = fields.get("unread_count", 0)

        if existing.total_count != incoming_total:
            return True
        return existing.unread_count != incoming_unread

    def _has_meaningful_message_changes(
        self,
        existing: Email,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming message data has meaningful changes.

        Args:
            existing: Existing Email record from database
            incoming: Incoming message data from provider
            provider: Provider name (outlook, gmail, etc.)

        Returns:
            True if meaningful changes detected, False otherwise
        """
        fields = self._extract_message_fields(incoming, provider)

        # Compare key string fields
        checks = [
            (existing.subject, fields.get("subject")),
            (existing.from_email, fields.get("from_email")),
            (existing.is_read, fields.get("is_read")),
            (existing.is_starred, fields.get("is_starred")),
            (existing.is_important, fields.get("is_important")),
            (existing.is_draft, fields.get("is_draft")),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        # Compare labels (list comparison)
        existing_labels = existing.labels or []
        incoming_labels = fields.get("labels") or []
        if set(existing_labels) != set(incoming_labels):
            return True

        # Check if message moved to different folder
        incoming_folder_id = self._extract_external_folder_id_from_message(incoming, provider)
        existing_folder_metadata = existing.email_metadata or {}
        existing_parent_folder = existing_folder_metadata.get("parent_folder_id")
        return bool(
            incoming_folder_id
            and existing_parent_folder
            and incoming_folder_id != existing_parent_folder
        )


def _parse_email_address(raw: str) -> tuple[str, str | None]:
    """Parse 'Display Name <email@example.com>' into (email, name).

    Also handles bare 'email@example.com' with no display name.
    """
    raw = raw.strip()
    if "<" in raw and raw.endswith(">"):
        # "Display Name <email@example.com>"
        name_part, email_part = raw.rsplit("<", 1)
        return email_part.rstrip(">").strip(), name_part.strip().strip('"') or None
    return raw, None


def _parse_email_list(header_value: str | None) -> list[str]:
    """Parse a comma-separated email header into a list of email addresses."""
    if not header_value:
        return []
    addresses: list[str] = []
    for part in header_value.split(","):
        email, _ = _parse_email_address(part)
        if email:
            addresses.append(email)
    return addresses


__all__ = ["EmailSyncService"]
