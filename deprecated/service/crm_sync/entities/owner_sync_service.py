"""Owner entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import CRMUser
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncContext,
    classify_records,
    log_sync_action_distribution,
)

logger = get_logger(__name__)


class OwnerSyncService(BaseSyncService[CRMUser]):
    """Service for syncing CRM owner/user data.

    Handles synchronization of owner records from CRM platforms.
    Owners represent users who can be assigned to deals, contacts, etc.

    Note: Using CRMUser model as owners are essentially CRM users
    with ownership capabilities.
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync owner records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw owner records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics
        """
        # Initialize result tracking
        result = self._init_result()
        self._log_sync_start("owners", provider, 0)

        # Convert to list for processing
        owner_list = [r for r in records if isinstance(r, dict)]
        if not owner_list:
            result.complete()
            return result.to_dict()

        # Extract external IDs (filter out None values)
        external_ids = [
            str(ext_id)
            for owner in owner_list
            if (ext_id := self._extract_external_id(owner)) is not None
        ]

        # Load existing records
        existing = await self._load_existing_by_external_ids(
            CRMUser,
            external_ids,
            integration.integration_id,
        )

        now = utcnow()
        owners_processed = 0

        for owner in owner_list:
            try:
                external_id = self._extract_external_id(owner)
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for owner: {owner.get('id')}")
                    continue

                # Get or create record
                record = existing.get(external_id)
                is_new = record is None

                if not record:
                    record = CRMUser(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )

                # Transform and update record
                self._update_record(record, owner, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                owners_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

            except Exception as exc:
                self._log_error("owner", owner.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit changes
        await self.db.commit()

        result.metadata["owners_synced"] = owners_processed
        result.complete()
        self._log_sync_complete(result)

        return result.to_dict()

    async def sync_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync owner records incrementally.

        This method is optimized for incremental syncs where records come with
        sync_action metadata. It uses database state as the source of truth.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw owner records from provider API with _sync_metadata
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics
        """
        result = self._init_result()
        self._log_sync_start("owners_incremental", provider, 0)

        owner_list = [r for r in records if isinstance(r, dict)]
        if not owner_list:
            result.complete()
            return result.to_dict()

        # Log sync action distribution for observability
        log_sync_action_distribution(owner_list, "owners", logger)

        # Extract external IDs
        external_ids = [
            str(ext_id)
            for owner in owner_list
            if (ext_id := self._extract_external_id(owner)) is not None
        ]

        # Load existing records to determine create vs update
        existing = await self._load_existing_by_external_ids(
            CRMUser,
            external_ids,
            integration.integration_id,
        )
        existing_ids = set(existing.keys())

        # Classify records for logging (sync_action is only for observability)
        context = SyncContext(existing_ids=existing_ids)
        classify_records(owner_list, context)

        now = utcnow()
        owners_processed = 0
        owners_skipped = 0

        for owner in owner_list:
            try:
                external_id = self._extract_external_id(owner)
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for owner: {owner.get('id')}")
                    continue

                # Extract sync metadata for logging/observability
                sync_metadata = owner.get("_sync_metadata", {})
                sync_action = SyncAction(sync_metadata.get("sync_action", SyncAction.ADDED.value))

                # Handle deletions
                is_archived = owner.get("archived", False) or owner.get("IsDeleted", False)

                if sync_action == SyncAction.DELETED or is_archived:
                    # Soft-delete: mark as deleted if exists
                    record = existing.get(external_id)
                    if record:
                        record.is_deleted = True
                        record.is_active = False
                        record.synced_at = now
                        record.last_seen_at = now
                        self.db.add(record)
                        result.increment_deleted()
                        owners_processed += 1
                    continue

                # Database state is source of truth for create vs update
                record = existing.get(external_id)
                is_new = record is None

                if is_new:
                    # Create new owner
                    record = CRMUser(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )
                else:
                    # Check for meaningful changes before updating
                    if not self._has_meaningful_changes(record, owner, provider):
                        owners_skipped += 1
                        # Update last_seen_at even if no changes
                        record.last_seen_at = now
                        self.db.add(record)
                        continue

                # Transform and update record
                self._update_record(record, owner, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                owners_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

            except Exception as exc:
                self._log_error("owner", owner.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit changes
        await self.db.commit()

        result.metadata["owners_synced"] = owners_processed
        result.metadata["skipped"] = owners_skipped
        result.complete()

        logger.info(
            "Incremental owner sync complete: processed=%d, skipped=%d, created=%d, updated=%d, deleted=%d",
            owners_processed,
            owners_skipped,
            result.created,
            result.updated,
            result.deleted,
        )

        return {
            **result.to_dict(),
            "skipped": owners_skipped,
        }

    def _has_meaningful_changes(
        self,
        existing: CRMUser,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming data has meaningful changes compared to existing record.

        Args:
            existing: Existing CRMUser record from database
            incoming: Incoming owner data from provider
            provider: Provider name for field mapping

        Returns:
            True if there are meaningful changes, False otherwise
        """
        _ = provider  # Reserved for future provider-specific logic

        # Compare key fields
        checks = [
            (existing.email, incoming.get("email")),
            (existing.first_name, incoming.get("firstName")),
            (existing.last_name, incoming.get("lastName")),
            (existing.full_name, self._build_full_name(incoming)),
            (existing.job_title, incoming.get("jobTitle")),
            (existing.user_role, incoming.get("roleId")),
            (existing.is_active, not incoming.get("archived", False)),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        # Check team changes
        incoming_teams = incoming.get("teams", [])
        if isinstance(incoming_teams, list) and incoming_teams:
            new_team_ids = [
                str(t.get("id")) for t in incoming_teams if isinstance(t, dict) and t.get("id")
            ]
            if existing.team_ids != new_team_ids:
                return True

        return False

    def _transform_record(self, raw: dict[str, Any], provider: str) -> CRMUser:
        """Transform provider-specific owner data to CRMUser model.

        Args:
            raw: Raw owner record from provider
            provider: Provider name (reserved for future provider-specific logic)

        Returns:
            Transformed CRMUser instance
        """
        # Note: provider parameter reserved for future provider-specific transformations
        _ = provider  # Explicitly mark as intentionally unused

        # This is a simplified transformation
        # In production, use provider-specific transformers
        return CRMUser(
            organisation_id=self.organisation_id,
            integration_id=UUID("00000000-0000-0000-0000-000000000000"),  # Placeholder
            external_id=str(raw.get("id", "")),
            email=raw.get("email"),
            first_name=raw.get("firstName"),
            last_name=raw.get("lastName"),
            full_name=self._build_full_name(raw),
        )

    def _update_record(self, record: CRMUser, raw: dict[str, Any], provider: str) -> None:
        """Update CRMUser record with data from provider.

        Args:
            record: Existing CRMUser instance to update
            raw: Raw data from provider
            provider: Provider name
        """
        # Basic fields
        record.email = raw.get("email")
        record.first_name = raw.get("firstName")
        record.last_name = raw.get("lastName")
        record.full_name = self._build_full_name(raw)

        # Additional fields based on provider
        if provider == "hubspot":
            record.user_role = raw.get("roleId")
            record.job_title = raw.get("jobTitle")
            record.time_zone = raw.get("timeZone")
            record.locale = raw.get("locale")

            # Team associations
            teams = raw.get("teams", [])
            if isinstance(teams, list) and teams:
                record.team_ids = [
                    str(t.get("id")) for t in teams if isinstance(t, dict) and t.get("id")
                ]

            # Primary team
            primary_team = raw.get("primaryTeamId")
            if primary_team:
                record.primary_team_id = str(primary_team)

        # Store raw data
        record.properties = raw
        record.data_payload = raw

        # Activity status
        record.is_active = not raw.get("archived", False)

    def _extract_external_id(self, raw: dict[str, Any]) -> str | None:
        """Extract external ID from raw record.

        Args:
            raw: Raw owner record

        Returns:
            External ID as string or None
        """
        ext_id = raw.get("id")
        if ext_id:
            return str(ext_id)

        # Try alternate keys
        for key in ["ownerId", "userId", "external_id"]:
            if key in raw and raw[key]:
                return str(raw[key])

        return None

    def _build_full_name(self, raw: dict[str, Any]) -> str | None:
        """Build full name from name components.

        Args:
            raw: Raw owner record

        Returns:
            Full name or None
        """
        # Check if full name provided
        full_name = raw.get("fullName")
        if full_name is not None and isinstance(full_name, str):
            return str(full_name)

        # Build from components
        first_raw = raw.get("firstName")
        first = first_raw.strip() if isinstance(first_raw, str) else ""

        last_raw = raw.get("lastName")
        last = last_raw.strip() if isinstance(last_raw, str) else ""

        if first and last:
            return f"{first} {last}"
        if first:
            return str(first)
        if last:
            return str(last)

        # Fallback to email username
        email = raw.get("email")
        if email is not None and isinstance(email, str) and "@" in email:
            return str(email.split("@")[0])

        return None
