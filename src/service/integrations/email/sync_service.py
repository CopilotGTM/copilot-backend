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
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email, EmailFolder
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.utils.logger import get_logger
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.producers.email import EmailEventAdapter
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY
from src.service.integrations.email.change_detector import (
    has_meaningful_folder_changes,
    has_meaningful_message_changes,
)
from src.service.integrations.email.normalizer import (
    EmailPayloadNormalizer,
)
from src.service.integrations.email.writer import EmailSyncWriter
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
_SUPPRESSED_AUTOMATION_SOURCE_MODES = frozenset({"initial_sync", "backfill"})


def _stored_flag(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().casefold() == "true")


class EmailAutomationEventEnqueuer(Protocol):
    """Transaction-local automation dispatch boundary used by email writes."""

    async def enqueue(self, envelope: AutomationEventEnvelope) -> object:
        """Queue the current Email Automation Event Enqueuer work while preserving its tenant and idempotency boundaries."""
        ...


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

    MONITORED_FOLDER_TYPES = EmailPayloadNormalizer.MONITORED_FOLDER_TYPES
    GMAIL_LABEL_TYPE_MAP = EmailPayloadNormalizer.GMAIL_LABEL_TYPE_MAP
    _GMAIL_LABEL_TYPE_MAP = GMAIL_LABEL_TYPE_MAP

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        automation_events: EmailAutomationEventEnqueuer | None = None,
    ) -> None:
        self.db = session
        self.organisation_id = organisation_id
        self._sync_history = SyncHistoryManager(session)
        self._normalizer = EmailPayloadNormalizer()
        self._writer = EmailSyncWriter(session, organisation_id)
        self._automation_events = automation_events or AutomationEventDispatchService(
            session,
            organisation_id,
            registry=AUTOMATION_REGISTRY,
        )
        self._audience_user_ids_by_email: dict[str, set[UUID]] | None = None

    @property
    def _folder_cache(self) -> dict[tuple[UUID, str], UUID]:
        """Compatibility view over the writer's folder cache."""
        return self._writer.folder_cache

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
        records_committed = 0

        try:
            folder_list = list(folders)
            logger.debug(
                "Folder sync started",
                extra={
                    "event": "email_folder_sync_started",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "folder_count": len(folder_list),
                },
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
                    else:
                        updated_count += 1

                    records_processed += 1

                    if records_processed % 50 == 0:
                        await self.db.commit()
                        records_committed = records_processed

                except Exception as e:
                    logger.error(
                        "Failed to sync email folder",
                        extra={
                            "event": "email_folder_sync_item_failed",
                            "folder_index": idx,
                            "external_id": folder_data.get("id"),
                            "error": str(e),
                        },
                        exc_info=True,
                    )
                    records_failed += 1

            await self.db.commit()
            records_committed = records_processed

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

            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Folder sync completed",
                extra={
                    "event": "email_folder_sync_completed",
                    "integration_id": str(integration_id),
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
                "Folder sync failed",
                extra={
                    "event": "email_folder_sync_failed",
                    "integration_id": str(integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )

            await self._sync_history.fail(
                sync_history,
                integration_id=integration_id,
                records_processed=records_committed,
                records_failed=len(folder_list),
                error=e,
            )

            raise

    async def sync_messages(
        self,
        *,
        integration: KnowledgeIntegration,
        messages: Iterable[dict[str, Any]],
        provider: str,
        folder_id: UUID | None = None,
        source_mode: str = "system",
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
        records_committed = 0

        try:
            message_list = list(messages)
            logger.debug(
                "Message sync started",
                extra={
                    "event": "email_message_sync_started",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "message_count": len(message_list),
                    "folder_id": str(folder_id) if folder_id else None,
                },
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            changed_email_ids: list[str] = []

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

                    is_created = message.created_at == message.updated_at
                    if is_created:
                        created_count += 1
                    else:
                        updated_count += 1

                    changed_email_ids.append(str(message.email_id))
                    records_processed += 1
                    if is_created and source_mode not in _SUPPRESSED_AUTOMATION_SOURCE_MODES:
                        await self._enqueue_email_event(
                            message,
                            integration=integration,
                            provider=provider,
                            source_mode=source_mode,
                        )

                    if records_processed % 50 == 0:
                        await self.db.commit()
                        records_committed = records_processed

                except Exception as e:
                    logger.error(
                        "Failed to sync email message",
                        extra={
                            "event": "email_message_sync_item_failed",
                            "message_index": idx,
                            "external_id": message_data.get("id"),
                            "error": str(e),
                        },
                        exc_info=True,
                    )
                    if self.db.in_transaction():
                        await self.db.rollback()
                    records_failed += 1

            # Persist batched changes before emitting terminal run metadata.
            await self.db.commit()
            records_committed = records_processed
            if records_failed:
                raise RuntimeError(
                    f"{records_failed} of {len(message_list)} email messages failed to persist"
                )

            # Persist run outcome and action counts for operational visibility.
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created_count": created_count,
                    "updated": updated_count,
                    "provider": provider,
                    "folder_id": str(folder_id) if folder_id else None,
                },
            )

            # Record successful completion timestamp for this integration.
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Message sync completed",
                extra={
                    "event": "email_message_sync_completed",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "created_count": created_count,
                    "updated": updated_count,
                    "failed": records_failed,
                    "folder_id": str(folder_id) if folder_id else None,
                },
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created_count": created_count,
                "updated": updated_count,
                "changed_email_ids": changed_email_ids,
            }

        except Exception as e:
            logger.error(
                "Email message sync failed",
                extra={
                    "event": "email_message_sync_failed",
                    "integration_id": str(integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )

            await self._sync_history.fail(
                sync_history,
                integration_id=integration_id,
                records_processed=records_committed,
                records_failed=len(message_list),
                error=e,
            )

            raise

    async def _upsert_folder(
        self,
        integration_id: UUID,
        folder_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> EmailFolder:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.upsert_folder(integration_id, folder_data, provider, external_id)

    def _build_folder_upsert_values(
        self,
        integration_id: UUID,
        folder_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return self._writer.build_folder_upsert_values(
            integration_id,
            folder_data,
            provider,
            external_id,
        )

    @staticmethod
    def _folder_upsert_update_dict(stmt: Any, values: dict[str, Any]) -> dict[str, Any]:
        return EmailSyncWriter.folder_upsert_update_dict(stmt, values)

    async def _batch_upsert_folders(
        self,
        integration_id: UUID,
        items: list[tuple[dict[str, Any], str]],
        provider: str,
    ) -> dict[str, EmailFolder]:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.batch_upsert_folders(integration_id, items, provider)

    async def _upsert_message(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        folder_id: UUID | None,
    ) -> Email:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.upsert_message(
            integration_id,
            message_data,
            provider,
            external_id,
            folder_id,
        )

    def _build_message_upsert_values(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        folder_id: UUID,
    ) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return self._writer.build_message_upsert_values(
            integration_id,
            message_data,
            provider,
            external_id,
            folder_id,
        )

    @staticmethod
    def _message_upsert_update_dict(stmt: Any, values: dict[str, Any]) -> dict[str, Any]:
        return EmailSyncWriter.message_upsert_update_dict(stmt, values)

    async def _batch_upsert_messages(
        self,
        integration_id: UUID,
        items: list[tuple[dict[str, Any], str]],
        provider: str,
        folder_id: UUID | None,
    ) -> dict[str, Email]:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.batch_upsert_messages(integration_id, items, provider, folder_id)

    async def _get_folder_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> EmailFolder | None:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.get_folder_by_external_id(integration_id, external_id)

    @staticmethod
    def _extract_folder_id(folder_data: dict[str, Any], provider: str) -> str | None:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_folder_id(folder_data, provider)

    @staticmethod
    def _extract_message_id(message_data: dict[str, Any], provider: str) -> str | None:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_message_id(message_data, provider)

    @classmethod
    def _extract_folder_fields(cls, folder_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_folder_fields(folder_data, provider)

    @staticmethod
    def _extract_outlook_folder_fields(folder_data: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_outlook_folder_fields(folder_data)

    @staticmethod
    def _classify_folder_type(folder_name: str) -> str:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.classify_folder_type(folder_name)

    @staticmethod
    def _extract_gmail_folder_fields(label_data: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_gmail_folder_fields(label_data)

    @staticmethod
    def _extract_generic_folder_fields(folder_data: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_generic_folder_fields(folder_data)

    @classmethod
    def _extract_message_fields(cls, message_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_message_fields(message_data, provider)

    @staticmethod
    def _extract_outlook_message_fields(message_data: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_outlook_message_fields(message_data)

    @staticmethod
    def _extract_gmail_message_fields(message_data: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_gmail_message_fields(message_data)

    @staticmethod
    def _extract_generic_message_fields(message_data: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_generic_message_fields(message_data)

    async def _ensure_folder_id(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        existing_folder_id: UUID | None,
        external_message_id: str,
    ) -> UUID:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.ensure_folder_id(
            integration_id,
            message_data,
            provider,
            existing_folder_id,
            external_message_id,
        )

    async def _resolve_folder_id(
        self,
        integration_id: UUID,
        external_folder_id: str | None,
    ) -> UUID | None:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.resolve_folder_id(integration_id, external_folder_id)

    async def _load_folder_ids_by_external_ids(
        self,
        integration_id: UUID,
        external_folder_ids: list[str],
    ) -> dict[str, UUID]:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.load_folder_ids_by_external_ids(
            integration_id,
            external_folder_ids,
        )

    async def _resolve_message_folder_ids_for_batch(
        self,
        integration_id: UUID,
        messages_by_external_id: dict[str, dict[str, Any]],
        provider: str,
        folder_id: UUID | None,
    ) -> dict[str, UUID]:
        return await self._writer.resolve_message_folder_ids_for_batch(
            integration_id,
            messages_by_external_id,
            provider,
            folder_id,
        )

    @staticmethod
    def _extract_external_folder_id_from_message(
        message_data: dict[str, Any],
        provider: str,
    ) -> str | None:
        """Compatibility wrapper for tests and older call sites."""
        return EmailPayloadNormalizer.extract_external_folder_id_from_message(
            message_data,
            provider,
        )

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
        records_committed = 0

        try:
            folder_list = list(folders)
            logger.debug(
                "Incremental folder sync started",
                extra={
                    "event": "email_folder_incremental_sync_started",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "folder_count": len(folder_list),
                },
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
                    "created_count": 0,
                    "updated": 0,
                    "skipped": 0,
                    "changed_email_ids": [],
                }

            # Track action counts for logging (observability only)
            action_counts = SyncActionCounts()

            folder_external_ids = [
                eid
                for f in folder_list
                for eid in [self._extract_folder_id(f, provider)]
                if eid is not None
            ]
            existing = await self._load_existing_folders_by_external_ids(
                integration_id, folder_external_ids
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0
            changed_folders: list[tuple[dict[str, Any], str]] = []

            for idx, folder_data in enumerate(folder_list):
                try:
                    external_id = self._extract_folder_id(folder_data, provider)
                    if not external_id:
                        records_failed += 1
                        continue

                    sync_action = extract_sync_action(folder_data)
                    if sync_action:
                        action_counts.increment(sync_action)

                    record = existing.get(external_id)
                    is_new = record is None

                    if sync_action == SyncAction.DELETED:
                        continue

                    if is_new or record is None:
                        changed_folders.append((folder_data, external_id))
                        records_processed += 1
                        created_count += 1
                    else:
                        if self._has_meaningful_folder_changes(record, folder_data, provider):
                            changed_folders.append((folder_data, external_id))
                            records_processed += 1
                            updated_count += 1
                        else:
                            skipped_count += 1
                            action_counts.unchanged += 1
                            continue

                except Exception as e:
                    logger.error(
                        "Failed to sync email folder",
                        extra={
                            "event": "email_folder_sync_item_failed",
                            "folder_index": idx,
                            "external_id": folder_data.get("id"),
                            "error": str(e),
                        },
                        exc_info=True,
                    )
                    records_failed += 1

            if changed_folders:
                await self._batch_upsert_folders(integration_id, changed_folders, provider)

            # Persist all folder upserts before final run bookkeeping.
            await self.db.commit()
            records_committed = records_processed

            # Store run metadata so the scheduler can make recovery decisions.
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

            # Record successful completion timestamp for this integration.
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Incremental folder sync completed",
                extra={
                    "event": "email_folder_incremental_sync_completed",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "created_count": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "failed": records_failed,
                },
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created_count": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "action_counts": action_counts.to_dict(),
            }

        except Exception as e:
            logger.error(
                "Folder incremental sync failed",
                extra={
                    "event": "email_folder_incremental_sync_failed",
                    "integration_id": str(integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )

            await self._sync_history.fail(
                sync_history,
                integration_id=integration_id,
                records_processed=records_committed,
                records_failed=len(folder_list),
                error=e,
            )

            raise

    async def sync_messages_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        messages: Iterable[dict[str, Any]],
        provider: str,
        folder_id: UUID | None = None,
        source_mode: str = "incremental_sync",
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
        records_committed = 0

        try:
            message_list = list(messages)
            logger.debug(
                "Incremental message sync started",
                extra={
                    "event": "email_message_incremental_sync_started",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "message_count": len(message_list),
                    "folder_id": str(folder_id) if folder_id else None,
                },
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
                    "created_count": 0,
                    "updated": 0,
                    "skipped": 0,
                }

            # Track action counts for logging (observability only)
            action_counts = SyncActionCounts()

            message_external_ids = [
                eid
                for m in message_list
                for eid in [self._extract_message_id(m, provider)]
                if eid is not None
            ]
            existing = await self._load_existing_messages_by_external_ids(
                integration_id, message_external_ids
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0
            changed_email_ids: list[str] = []
            changed_messages: list[tuple[dict[str, Any], str]] = []
            change_kind_by_external_id: dict[str, str] = {}
            event_external_ids: set[str] = set()

            for idx, message_data in enumerate(message_list):
                try:
                    external_id = self._extract_message_id(message_data, provider)
                    if not external_id:
                        records_failed += 1
                        continue

                    sync_action = extract_sync_action(message_data)
                    if sync_action:
                        action_counts.increment(sync_action)

                    record = existing.get(external_id)
                    is_new = record is None

                    if sync_action == SyncAction.DELETED:
                        if record is not None and not record.is_deleted:
                            record.is_deleted = True
                            records_processed += 1
                            updated_count += 1
                            changed_email_ids.append(str(record.email_id))
                        continue

                    if is_new or record is None:
                        changed_messages.append((message_data, external_id))
                        change_kind_by_external_id[external_id] = "created"
                        event_external_ids.add(external_id)
                    else:
                        if self._has_meaningful_message_changes(record, message_data, provider):
                            changed_messages.append((message_data, external_id))
                            change_kind_by_external_id[external_id] = "updated"
                            normalized = self._normalizer.normalize_message(message_data, provider)
                            if _stored_flag(record.is_draft) and not _stored_flag(
                                normalized.fields.get("is_draft")
                            ):
                                event_external_ids.add(external_id)
                        else:
                            skipped_count += 1
                            action_counts.unchanged += 1
                            continue

                except Exception as e:
                    logger.error(
                        "Failed to sync email message (incremental)",
                        extra={
                            "event": "email_message_incremental_sync_item_failed",
                            "message_index": idx,
                            "external_id": message_data.get("id"),
                            "error": str(e),
                        },
                        exc_info=True,
                    )
                    records_failed += 1

            if changed_messages:
                (
                    processed,
                    failed,
                    created,
                    updated,
                    persisted_email_ids,
                ) = await self._persist_incremental_messages(
                    integration,
                    changed_messages,
                    provider,
                    folder_id,
                    change_kind_by_external_id,
                    event_external_ids,
                    source_mode,
                )
                records_processed += processed
                records_failed += failed
                created_count += created
                updated_count += updated
                changed_email_ids.extend(persisted_email_ids)

            # Persist all message upserts before final run bookkeeping.
            await self.db.commit()
            records_committed = records_processed
            if records_failed:
                raise RuntimeError(
                    f"{records_failed} of {len(message_list)} email messages failed to persist"
                )

            # Store run metadata so the scheduler can make recovery decisions.
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
                    "folder_id": str(folder_id) if folder_id else None,
                    "action_counts": action_counts.to_dict(),
                },
            )

            # Record successful completion timestamp for this integration.
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Incremental message sync completed",
                extra={
                    "event": "email_message_incremental_sync_completed",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "created_count": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "failed": records_failed,
                    "folder_id": str(folder_id) if folder_id else None,
                },
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created_count": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "changed_email_ids": changed_email_ids,
                "action_counts": action_counts.to_dict(),
            }

        except Exception as e:
            logger.error(
                "Email message incremental sync failed",
                extra={
                    "event": "email_message_incremental_sync_failed",
                    "integration_id": str(integration_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )

            await self._sync_history.fail(
                sync_history,
                integration_id=integration_id,
                records_processed=records_committed,
                records_failed=len(message_list),
                error=e,
            )

            raise

    async def _persist_incremental_messages(
        self,
        integration: KnowledgeIntegration,
        changed_messages: list[tuple[dict[str, Any], str]],
        provider: str,
        folder_id: UUID | None,
        change_kind_by_external_id: dict[str, str],
        event_external_ids: set[str],
        source_mode: str,
    ) -> tuple[int, int, int, int, list[str]]:
        """Persist changed messages with batch speed and item-level failure recovery.

        The optimistic path keeps the N+1 fix. The fallback restores the old
        failure boundary when one malformed row would otherwise poison the batch.
        """
        integration_id = integration.integration_id
        try:
            written = await self._batch_upsert_messages(
                integration_id,
                changed_messages,
                provider,
                folder_id,
            )
            for message in written.values():
                if (
                    message.external_message_id in event_external_ids
                    and source_mode not in _SUPPRESSED_AUTOMATION_SOURCE_MODES
                ):
                    await self._enqueue_email_event(
                        message,
                        integration=integration,
                        provider=provider,
                        source_mode=source_mode,
                    )
            return self._count_written_message_changes(
                written.values(),
                change_kind_by_external_id,
            )
        except Exception as e:
            logger.warning(
                "Batch email message upsert failed; falling back to item writes",
                extra={
                    "event": "email_message_incremental_batch_upsert_failed",
                    "integration_id": str(integration_id),
                    "provider": provider,
                    "message_count": len(changed_messages),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            if self.db.in_transaction():
                await self.db.rollback()

        return await self._persist_incremental_messages_individually(
            integration,
            changed_messages,
            provider,
            folder_id,
            change_kind_by_external_id,
            event_external_ids,
            source_mode,
        )

    async def _persist_incremental_messages_individually(
        self,
        integration: KnowledgeIntegration,
        changed_messages: list[tuple[dict[str, Any], str]],
        provider: str,
        folder_id: UUID | None,
        change_kind_by_external_id: dict[str, str],
        event_external_ids: set[str],
        source_mode: str,
    ) -> tuple[int, int, int, int, list[str]]:
        """Persist changed messages one at a time after batch failure."""
        integration_id = integration.integration_id
        records_processed = 0
        records_failed = 0
        created_count = 0
        updated_count = 0
        changed_email_ids: list[str] = []

        for idx, (message_data, external_id) in enumerate(changed_messages):
            try:
                message = await self._upsert_message(
                    integration_id,
                    message_data,
                    provider,
                    external_id,
                    folder_id,
                )
                if (
                    external_id in event_external_ids
                    and source_mode not in _SUPPRESSED_AUTOMATION_SOURCE_MODES
                ):
                    await self._enqueue_email_event(
                        message,
                        integration=integration,
                        provider=provider,
                        source_mode=source_mode,
                    )
                await self.db.commit()
                records_processed += 1
                changed_email_ids.append(str(message.email_id))
                if change_kind_by_external_id.get(external_id) == "created":
                    created_count += 1
                else:
                    updated_count += 1
            except Exception as e:
                logger.error(
                    "Failed to sync email message (incremental fallback)",
                    extra={
                        "event": "email_message_incremental_fallback_item_failed",
                        "message_index": idx,
                        "external_id": external_id,
                        "error": str(e),
                    },
                    exc_info=True,
                )
                if self.db.in_transaction():
                    await self.db.rollback()
                records_failed += 1

        return records_processed, records_failed, created_count, updated_count, changed_email_ids

    async def _enqueue_email_event(
        self,
        message: Email,
        *,
        integration: KnowledgeIntegration,
        provider: str,
        source_mode: str,
    ) -> None:
        envelope = EmailEventAdapter(self.organisation_id).from_email(
            message,
            integration=integration,
            provider=provider,
            source_mode=source_mode,
            audience_user_ids=await self._email_audience_user_ids(message, integration),
        )
        if envelope is not None:
            await self._automation_events.enqueue(envelope)

    async def _email_audience_user_ids(
        self,
        message: Email,
        integration: KnowledgeIntegration,
    ) -> set[UUID]:
        user_ids = {integration.created_by_user_id}
        emails = {
            value.strip().casefold()
            for value in (
                message.from_email,
                *message.to_emails,
                *message.cc_emails,
                *message.bcc_emails,
            )
            if value and value.strip()
        }
        if not emails:
            return user_ids
        if self._audience_user_ids_by_email is None:
            rows = await self.db.execute(
                select(col(User.user_id), col(User.email)).where(
                    col(User.organisation_id) == self.organisation_id,
                    col(User.status) == SupabaseUserStatus.ACTIVE,
                    col(User.admin_status) == AdminUserStatus.APPROVED,
                )
            )
            directory: dict[str, set[UUID]] = {}
            for user_id, email in rows.all():
                directory.setdefault(email.strip().casefold(), set()).add(user_id)
            self._audience_user_ids_by_email = directory
        for email in emails:
            user_ids.update(self._audience_user_ids_by_email.get(email, ()))
        return user_ids

    @staticmethod
    def _count_written_message_changes(
        messages: Iterable[Email],
        change_kind_by_external_id: dict[str, str],
    ) -> tuple[int, int, int, int, list[str]]:
        """Convert persisted external IDs into sync counters."""
        records_processed = 0
        created_count = 0
        updated_count = 0
        changed_email_ids: list[str] = []

        for message in messages:
            external_id = message.external_message_id
            records_processed += 1
            changed_email_ids.append(str(message.email_id))
            if change_kind_by_external_id.get(external_id) == "created":
                created_count += 1
            else:
                updated_count += 1

        return records_processed, 0, created_count, updated_count, changed_email_ids

    async def _load_existing_folders_by_external_ids(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, EmailFolder]:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.load_existing_folders_by_external_ids(
            integration_id,
            external_ids,
        )

    async def _load_existing_messages_by_external_ids(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, Email]:
        """Compatibility wrapper for tests and older call sites."""
        return await self._writer.load_existing_messages_by_external_ids(
            integration_id,
            external_ids,
        )

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
        normalized = self._normalizer.normalize_folder(incoming, provider)
        return has_meaningful_folder_changes(existing, normalized)

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
        normalized = self._normalizer.normalize_message(incoming, provider)
        return has_meaningful_message_changes(existing, normalized)


__all__ = ["EmailSyncService"]
