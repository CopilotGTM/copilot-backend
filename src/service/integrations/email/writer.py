"""Database writer for email sync rows."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.email_models import Email, EmailFolder
from src.service.integrations.email.normalizer import EmailPayloadNormalizer


class EmailSyncWriter:
    """Owns email folder/message persistence and folder ID resolution."""

    DEFAULT_MESSAGE_UPSERT_CHUNK_SIZE = 500

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        message_upsert_chunk_size: int = DEFAULT_MESSAGE_UPSERT_CHUNK_SIZE,
    ) -> None:
        """Bind the SQL session and org context for all email write operations."""
        self.session = session
        self.organisation_id = organisation_id
        self.normalizer = EmailPayloadNormalizer()
        self.folder_cache: dict[tuple[UUID, str], UUID] = {}
        self.message_upsert_chunk_size = max(1, message_upsert_chunk_size)

    async def upsert_folder(
        self,
        integration_id: UUID,
        folder_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> EmailFolder:
        """Insert or update one `(organisation_id, integration_id, external_folder_id)` folder.

        The folder cache is refreshed here so later message upserts can avoid
        additional lookups in the same batch.
        """
        values = self.build_folder_upsert_values(
            integration_id,
            folder_data,
            provider,
            external_id,
        )

        stmt: Any = insert(EmailFolder).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["organisation_id", "integration_id", "external_folder_id"],
            set_=self.folder_upsert_update_dict(stmt, values),
        ).returning(EmailFolder)

        result = await self.session.execute(stmt)
        folder = cast(EmailFolder, result.scalars().one())
        self.folder_cache[(integration_id, external_id)] = folder.folder_id
        return folder

    def build_folder_upsert_values(
        self,
        integration_id: UUID,
        folder_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> dict[str, Any]:
        """Build deterministic folder insert/update values from normalized payload.

        A shared path is used by single and bulk upsert flows so `INSERT ON CONFLICT`
        semantics are the same regardless of sync mode.
        """
        fields = self.normalizer.extract_folder_fields(folder_data, provider)
        now = datetime.now(UTC)
        return {
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "external_folder_id": external_id,
            "synced_at": now,
            "created_at": now,
            "updated_at": now,
            **fields,
        }

    @staticmethod
    def folder_upsert_update_dict(stmt: Any, values: dict[str, Any]) -> dict[str, Any]:
        """Define mutable fields for folder conflict resolution."""
        return {
            key: getattr(stmt.excluded, key)
            for key in values
            if key
            not in (
                "folder_id",
                "organisation_id",
                "integration_id",
                "external_folder_id",
                "created_at",
            )
        }

    async def batch_upsert_folders(
        self,
        integration_id: UUID,
        items: list[tuple[dict[str, Any], str]],
        provider: str,
    ) -> dict[str, EmailFolder]:
        """Upsert multiple folders in one statement and return `external_id -> folder`.

        Duplicates in incoming payload are collapsed by `external_id` so only the
        latest row is written and one row-per-folder contract is preserved.
        """
        if not items:
            return {}

        latest_by_external_id: dict[str, dict[str, Any]] = {}
        for folder_data, external_id in items:
            latest_by_external_id[external_id] = folder_data

        values = [
            self.build_folder_upsert_values(integration_id, folder_data, provider, external_id)
            for external_id, folder_data in latest_by_external_id.items()
        ]
        stmt: Any = insert(EmailFolder).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["organisation_id", "integration_id", "external_folder_id"],
            set_=self.folder_upsert_update_dict(stmt, values[0]),
        ).returning(EmailFolder)

        result = await self.session.execute(stmt)
        folders = result.scalars().all()

        folder_map = {folder.external_folder_id: folder for folder in folders}
        for external_id, folder in folder_map.items():
            self.folder_cache[(integration_id, external_id)] = folder.folder_id
        return folder_map

    async def upsert_message(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        folder_id: UUID | None,
    ) -> Email:
        """Insert or update one `(organisation_id, integration_id, external_message_id)` email."""
        resolved_folder_id = await self.ensure_folder_id(
            integration_id,
            message_data,
            provider,
            folder_id,
            external_id,
        )
        values = self.build_message_upsert_values(
            integration_id,
            message_data,
            provider,
            external_id,
            resolved_folder_id,
        )

        stmt: Any = insert(Email).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["organisation_id", "integration_id", "external_message_id"],
            set_=self.message_upsert_update_dict(stmt, values),
        ).returning(Email)

        result = await self.session.execute(stmt)
        return cast(Email, result.scalars().one())

    def build_message_upsert_values(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        external_id: str,
        folder_id: UUID,
    ) -> dict[str, Any]:
        """Build deterministic message insert/update values from normalized payload."""
        fields = self.normalizer.extract_message_fields(message_data, provider)
        now = datetime.now(UTC)
        return {
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "external_message_id": external_id,
            "folder_id": folder_id,
            "synced_at": now,
            "created_at": now,
            "updated_at": now,
            **fields,
        }

    @staticmethod
    def message_upsert_update_dict(stmt: Any, values: dict[str, Any]) -> dict[str, Any]:
        """Define mutable fields for message conflict resolution."""
        updates = {
            key: getattr(stmt.excluded, key)
            for key in values
            if key
            not in (
                "email_id",
                "organisation_id",
                "integration_id",
                "external_message_id",
                "created_at",
            )
        }
        if "email_metadata" in updates:
            # copilotgtm: JSONB merge retains internal keys and old provider-only keys;
            # split provider and pipeline metadata if stale provider keys become material.
            updates["email_metadata"] = Email.email_metadata.op("||")(stmt.excluded.email_metadata)
        return updates

    async def batch_upsert_messages(
        self,
        integration_id: UUID,
        items: list[tuple[dict[str, Any], str]],
        provider: str,
        folder_id: UUID | None,
    ) -> dict[str, Email]:
        """Upsert multiple messages in one statement and return `external_id -> message`.

        Folder IDs are resolved up front so each message row is written with the
        correct foreign key in the same bulk operation.
        """
        if not items:
            return {}

        latest_by_external_id: dict[str, dict[str, Any]] = {}
        for message_data, external_id in items:
            latest_by_external_id[external_id] = message_data

        resolved_folder_ids = await self.resolve_message_folder_ids_for_batch(
            integration_id,
            latest_by_external_id,
            provider,
            folder_id,
        )
        values = [
            self.build_message_upsert_values(
                integration_id,
                message_data,
                provider,
                external_id,
                resolved_folder_ids[external_id],
            )
            for external_id, message_data in latest_by_external_id.items()
        ]

        emails_by_external_id: dict[str, Email] = {}
        for value_chunk in self._chunk_values(values, self.message_upsert_chunk_size):
            stmt: Any = insert(Email).values(value_chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["organisation_id", "integration_id", "external_message_id"],
                set_=self.message_upsert_update_dict(stmt, value_chunk[0]),
            ).returning(Email)

            result = await self.session.execute(stmt)
            for email in result.scalars().all():
                emails_by_external_id[email.external_message_id] = email
        return emails_by_external_id

    @staticmethod
    def _chunk_values(
        values: list[dict[str, Any]],
        chunk_size: int,
    ) -> Iterable[list[dict[str, Any]]]:
        """Yield bounded value groups so one sync cannot exceed DB statement limits."""
        for start in range(0, len(values), chunk_size):
            yield values[start : start + chunk_size]

    async def get_folder_by_external_id(
        self,
        integration_id: UUID,
        external_id: str,
    ) -> EmailFolder | None:
        """Load one folder by its tenant, integration, and provider identity."""
        stmt = select(EmailFolder).where(
            col(EmailFolder.organisation_id) == self.organisation_id,
            col(EmailFolder.integration_id) == integration_id,
            col(EmailFolder.external_folder_id) == external_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def ensure_folder_id(
        self,
        integration_id: UUID,
        message_data: dict[str, Any],
        provider: str,
        existing_folder_id: UUID | None,
        external_message_id: str,
    ) -> UUID:
        """Resolve the folder for a message, raising if it cannot be determined.

        Writes require a non-null `folder_id`; this method guarantees explicit
        failure when provider payloads are inconsistent.
        """
        if existing_folder_id:
            return await self._validate_folder_id(integration_id, existing_folder_id)

        external_folder_id = self.normalizer.extract_external_folder_id_from_message(
            message_data,
            provider,
        )
        resolved = await self.resolve_folder_id(integration_id, external_folder_id)
        if resolved:
            return resolved

        raise ValueError(
            "Unable to resolve folder for message "
            f"{external_message_id} (provider={provider}, external_folder_id={external_folder_id})"
        )

    async def _validate_folder_id(self, integration_id: UUID, folder_id: UUID) -> UUID:
        """Require a supplied folder to belong to this tenant-bound integration."""
        # A UUID is not an ownership proof; keep the explicit tenant predicate even
        # though forced RLS provides the database-level isolation boundary.
        stmt = select(EmailFolder.folder_id).where(
            col(EmailFolder.organisation_id) == self.organisation_id,
            col(EmailFolder.integration_id) == integration_id,
            col(EmailFolder.folder_id) == folder_id,
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise ValueError(f"Folder {folder_id} does not belong to integration {integration_id}")
        return folder_id

    async def resolve_folder_id(
        self,
        integration_id: UUID,
        external_folder_id: str | None,
    ) -> UUID | None:
        """Resolve folder id from cache first, then DB lookup.

        Cache-first resolution keeps incremental message sync O(1) for hot folders
        and reduces repeated read traffic in large batches.
        """
        if not external_folder_id:
            return None

        cache_key = (integration_id, external_folder_id)
        cached_id = self.folder_cache.get(cache_key)
        if cached_id:
            return cached_id

        folder = await self.get_folder_by_external_id(integration_id, external_folder_id)
        if folder:
            self.folder_cache[cache_key] = folder.folder_id
            return folder.folder_id
        return None

    async def load_folder_ids_by_external_ids(
        self,
        integration_id: UUID,
        external_folder_ids: list[str],
    ) -> dict[str, UUID]:
        """Load a folder-id map for many external folder identifiers in one query."""
        unique_external_ids = list(dict.fromkeys(external_folder_ids))
        resolved = {
            external_id: self.folder_cache[(integration_id, external_id)]
            for external_id in unique_external_ids
            if (integration_id, external_id) in self.folder_cache
        }
        missing_external_ids = [
            external_id for external_id in unique_external_ids if external_id not in resolved
        ]

        if missing_external_ids:
            # One tenant-scoped SELECT keeps message upserts batched without
            # trusting provider folder IDs as an ownership boundary.
            stmt = select(EmailFolder).where(
                col(EmailFolder.organisation_id) == self.organisation_id,
                col(EmailFolder.integration_id) == integration_id,
                col(EmailFolder.external_folder_id).in_(missing_external_ids),
            )
            result = await self.session.execute(stmt)
            for folder in result.scalars().all():
                resolved[folder.external_folder_id] = folder.folder_id
                self.folder_cache[(integration_id, folder.external_folder_id)] = folder.folder_id

        return resolved

    async def resolve_message_folder_ids_for_batch(
        self,
        integration_id: UUID,
        messages_by_external_id: dict[str, dict[str, Any]],
        provider: str,
        folder_id: UUID | None,
    ) -> dict[str, UUID]:
        """Resolve each message's folder id before bulk message upsert.

        If the caller already supplied `folder_id`, that value is reused for all
        messages to avoid per-item folder lookups.
        """
        if folder_id:
            validated_folder_id = await self._validate_folder_id(integration_id, folder_id)
            return dict.fromkeys(messages_by_external_id, validated_folder_id)

        external_folder_by_message = {
            external_id: self.normalizer.extract_external_folder_id_from_message(
                message_data,
                provider,
            )
            for external_id, message_data in messages_by_external_id.items()
        }
        external_folder_ids = [
            external_folder_id
            for external_folder_id in external_folder_by_message.values()
            if external_folder_id is not None
        ]
        folder_ids = await self.load_folder_ids_by_external_ids(
            integration_id,
            external_folder_ids,
        )

        resolved: dict[str, UUID] = {}
        unresolved: list[tuple[str, str | None]] = []
        for external_id, external_folder_id in external_folder_by_message.items():
            if external_folder_id and external_folder_id in folder_ids:
                resolved[external_id] = folder_ids[external_folder_id]
            else:
                unresolved.append((external_id, external_folder_id))

        if unresolved:
            sample = ", ".join(
                f"{external_id}:{external_folder_id}"
                for external_id, external_folder_id in unresolved[:5]
            )
            raise ValueError(f"Unable to resolve folders for {len(unresolved)} messages: {sample}")

        return resolved

    async def load_existing_folders_by_external_ids(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, EmailFolder]:
        """Load tenant-owned folders for IDs already in a sync batch."""
        if not external_ids:
            return {}

        stmt = select(EmailFolder).where(
            col(EmailFolder.organisation_id) == self.organisation_id,
            col(EmailFolder.integration_id) == integration_id,
            col(EmailFolder.external_folder_id).in_(external_ids),
        )
        result = await self.session.execute(stmt)
        folders = result.scalars().all()

        return {folder.external_folder_id: folder for folder in folders}

    async def load_existing_messages_by_external_ids(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, Email]:
        """Load tenant-owned messages for IDs already in a sync batch."""
        if not external_ids:
            return {}

        stmt = select(Email).where(
            col(Email.organisation_id) == self.organisation_id,
            col(Email.integration_id) == integration_id,
            col(Email.external_message_id).in_(external_ids),
        )
        result = await self.session.execute(stmt)
        messages = result.scalars().all()

        return {message.external_message_id: message for message in messages}
