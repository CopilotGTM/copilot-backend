"""User entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import CRMUser
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService

logger = get_logger(__name__)


class UserSyncService(BaseSyncService[CRMUser]):
    """Service for syncing CRM user/owner data.

    Handles synchronization of user records with:
    - Flexible field extraction using _pick() pattern
    - Team associations and team ID deduplication
    - Active/archived status resolution
    - Multiple field name variations across providers
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync user records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw user records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics
        """
        result = self._init_result()
        self._log_sync_start("users", provider, 0)

        user_list = [u for u in records if isinstance(u, dict)]
        if not user_list:
            result.complete()
            return {"users": 0, **result.to_dict()}

        # Load existing users
        user_external_ids = [
            str(ext_id)
            for user in user_list
            if (ext_id := self._extract_external_id(user)) is not None
        ]
        existing = await self._load_existing_by_external_ids(
            CRMUser,
            user_external_ids,
            integration.integration_id,
        )

        now = utcnow()
        users_processed = 0

        for user in user_list:
            try:
                external_id = self._extract_external_id(user)
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for user: {user}")
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
                self._update_user_record(record, user, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                users_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

            except Exception as exc:
                self._log_error("user", user.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit users
        await self.db.commit()

        result.metadata["users"] = users_processed
        result.complete()
        self._log_sync_complete(result)

        return {
            "users": users_processed,
            **result.to_dict(),
        }

    def _transform_record(self, raw: dict[str, Any], _provider: str) -> CRMUser:
        """Transform provider-specific user data to CRMUser model.

        Args:
            raw: Raw user record from provider
            _provider: Provider name (unused, kept for interface compatibility)

        Returns:
            Transformed CRMUser instance

        Note:
            Simplified transformation - actual logic in _update_user_record.
        """
        external_id = self._extract_external_id(raw)
        return CRMUser(
            organisation_id=self.organisation_id,
            integration_id=UUID("00000000-0000-0000-0000-000000000000"),
            external_id=external_id or "",
            email=self._pick_field(raw, "email"),
        )

    def _update_user_record(
        self,
        record: CRMUser,
        raw: dict[str, Any],
        _provider: str,
    ) -> None:
        """Update CRMUser record with data from provider.

        Args:
            record: Existing CRMUser instance to update
            raw: Raw data from provider
            _provider: Provider name (unused, kept for interface compatibility)
        """
        # Extract properties block
        prop_block = raw.get("properties")
        properties = prop_block if isinstance(prop_block, dict) else {}

        # Helper for flexible field extraction
        def pick(*keys: str) -> Any:
            """Pick first non-empty value from user root or properties."""
            for key in keys:
                # Check root level
                if key in raw and raw[key] not in (None, ""):
                    return raw[key]
                # Check properties
                if key in properties and properties[key] not in (None, ""):
                    return properties[key]
            return None

        # Update all user sections
        self._update_name_fields(record, pick)
        self._update_profile_fields(record, pick)
        self._update_team_associations(record, raw, properties, pick)
        self._update_status_fields(record, pick)
        self._update_date_fields(record, pick)
        self._update_user_raw_data(record, raw, properties)

    def _update_name_fields(
        self,
        record: CRMUser,
        pick: Any,
    ) -> None:
        """Update name-related fields."""
        first_name = pick("firstName", "firstname", "first_name")
        last_name = pick("lastName", "lastname", "last_name")

        record.first_name = first_name if isinstance(first_name, str) else None
        record.last_name = last_name if isinstance(last_name, str) else None

        # Build full name
        full_name = pick("fullName", "fullname", "full_name")
        if full_name and isinstance(full_name, str):
            record.full_name = full_name
        else:
            record.full_name = self._combine_name(
                first_name, last_name, pick("displayName", "name")
            )

    def _update_profile_fields(self, record: CRMUser, pick: Any) -> None:
        """Update profile information fields."""
        email = pick("email")
        record.email = email if isinstance(email, str) else None

        user_role = pick("roleId", "role", "type")
        record.user_role = user_role if isinstance(user_role, str) else None

        job_title = pick("jobTitle", "jobtitle", "title", "positionTitle")
        record.job_title = job_title if isinstance(job_title, str) else None

        time_zone = pick("timeZone", "timezone")
        record.time_zone = time_zone if isinstance(time_zone, str) else None

        locale = pick("locale", "language")
        record.locale = locale if isinstance(locale, str) else None

    def _update_team_associations(
        self,
        record: CRMUser,
        raw: dict[str, Any],
        properties: dict[str, Any],
        pick: Any,
    ) -> None:
        """Update team associations with deduplication."""
        # Primary team
        primary_team = pick("primaryTeamId", "primary_team_id", "teamId")
        if primary_team is not None:
            record.primary_team_id = str(primary_team)

        # Extract team IDs from multiple sources
        team_ids = self._extract_team_ids(raw, properties)

        # Deduplicate while preserving order
        record.team_ids = self._deduplicate_team_ids(team_ids)

    def _extract_team_ids(self, raw: dict[str, Any], properties: dict[str, Any]) -> list[str]:
        """Extract team IDs from various payload formats.

        Args:
            raw: Raw user data
            properties: Properties block

        Returns:
            List of team IDs (may contain duplicates)
        """
        team_ids: list[str] = []

        # Extract from teams payload
        teams_payload = raw.get("teams")
        if not isinstance(teams_payload, list | str):
            teams_payload = properties.get("teams")

        team_ids.extend(self._extract_from_teams_payload(teams_payload))

        # Extract from teamIds array
        raw_team_ids = raw.get("teamIds")
        if isinstance(raw_team_ids, list):
            team_ids.extend(str(team_id) for team_id in raw_team_ids if team_id not in (None, ""))

        return team_ids

    def _extract_from_teams_payload(self, teams_payload: Any) -> list[str]:
        """Extract team IDs from teams payload (list or string).

        Args:
            teams_payload: Teams data (list of dicts or comma-separated string)

        Returns:
            List of team IDs
        """
        team_ids: list[str] = []

        if isinstance(teams_payload, list):
            for team in teams_payload:
                if not isinstance(team, dict):
                    continue
                team_id = team.get("id") or team.get("teamId")
                if team_id:
                    team_ids.append(str(team_id))
        elif isinstance(teams_payload, str):
            team_ids = [part.strip() for part in teams_payload.split(",") if part.strip()]

        return team_ids

    def _deduplicate_team_ids(self, team_ids: list[str]) -> list[str]:
        """Deduplicate team IDs while preserving order.

        Args:
            team_ids: List of team IDs (may contain duplicates)

        Returns:
            Deduplicated list of team IDs
        """
        if not team_ids:
            return []

        seen: set[str] = set()
        ordered_ids: list[str] = []

        for team_id in team_ids:
            if team_id not in seen:
                seen.add(team_id)
                ordered_ids.append(team_id)

        return ordered_ids

    def _update_status_fields(self, record: CRMUser, pick: Any) -> None:
        """Update status fields (active, archived, deleted)."""
        archived_flag = self._as_bool(pick("archived"))
        active_flag = self._as_bool(pick("active", "isActive", "hasAccess"))

        # Resolve active status
        if active_flag is not None:
            record.is_active = active_flag
        elif archived_flag is not None:
            record.is_active = not archived_flag
        # Keep existing value if neither flag present

        # Set remote deletion flag
        if archived_flag is not None:
            record.is_deleted_remote = bool(archived_flag)

    def _update_date_fields(self, record: CRMUser, pick: Any) -> None:
        """Update date-related fields."""
        record.last_login_at = self._parse_date(pick("lastLoginAt", "last_login_at"))
        record.source_created_at = self._parse_date(pick("createdAt", "createdate", "created_at"))
        record.source_modified_at = self._parse_date(pick("updatedAt", "updatedate", "updated_at"))

    def _update_user_raw_data(
        self,
        record: CRMUser,
        raw: dict[str, Any],
        properties: dict[str, Any],
    ) -> None:
        """Update raw data storage fields."""
        # Combine root fields with properties
        combined_properties: dict[str, Any] = {k: v for k, v in raw.items() if k != "properties"}
        if properties:
            combined_properties["properties"] = properties

        record.properties = combined_properties
        record.data_payload = raw

    def _extract_external_id(self, raw: dict[str, Any]) -> str | None:
        """Extract external ID from user data.

        Args:
            raw: Raw user record

        Returns:
            External ID or None
        """
        # Try common ID fields at root level
        for key in ("id", "ownerId", "userId", "user_id"):
            value = raw.get(key)
            if value not in (None, ""):
                return str(value)

        # Try properties block
        properties_block = raw.get("properties")
        if isinstance(properties_block, dict):
            for key in ("id", "ownerId", "userId", "user_id"):
                value = properties_block.get(key)
                if value not in (None, ""):
                    return str(value)

        return None

    def _pick_field(self, raw: dict[str, Any], *keys: str) -> Any:
        """Pick first non-empty field from raw data.

        Args:
            raw: Raw user record
            keys: Field names to try

        Returns:
            First non-empty value or None
        """
        properties_raw = raw.get("properties")
        properties: dict[str, Any] = properties_raw if isinstance(properties_raw, dict) else {}

        for key in keys:
            # Check root level
            if key in raw and raw[key] not in (None, ""):
                return raw[key]
            # Check properties
            if (
                isinstance(properties, dict)
                and key in properties
                and properties[key] not in (None, "")
            ):
                return properties[key]

        return None

    def _combine_name(
        self,
        first: Any,
        last: Any,
        fallback: Any = None,
    ) -> str | None:
        """Combine first and last name, with fallback.

        Args:
            first: First name
            last: Last name
            fallback: Fallback value if both names empty

        Returns:
            Combined name or fallback
        """
        # Ensure string types
        first_str = str(first).strip() if first and isinstance(first, str) else None
        last_str = str(last).strip() if last and isinstance(last, str) else None

        parts = [part for part in (first_str, last_str) if part]
        if parts:
            return " ".join(parts)

        # Return fallback if provided and is string
        if fallback and isinstance(fallback, str):
            return str(fallback).strip() or None

        return None

    def _as_bool(self, value: Any) -> bool | None:
        """Convert value to boolean.

        Args:
            value: Value to convert

        Returns:
            Boolean value or None
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return value != 0

        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False

        return None

    def _parse_date(self, value: Any) -> datetime | None:
        """Parse datetime value.

        Args:
            value: Date value (datetime, timestamp, or string)

        Returns:
            Parsed datetime or None
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, int | float):
            try:
                from datetime import UTC

                return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
            except Exception:
                return None

        return None
