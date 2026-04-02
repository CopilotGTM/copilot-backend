"""Contact entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import (
    CRMAssociationCategory,
    CRMCompany,
    CRMContact,
    CRMObjectAssociation,
    CRMObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncActionCounts,
    extract_sync_action,
)

logger = get_logger(__name__)


class ContactSyncService(BaseSyncService[CRMContact]):
    """Service for syncing CRM contact data.

    Handles synchronization of contact records with properties including:
    - Personal information (name, email, phone)
    - Company associations
    - Owner associations
    - Lifecycle and lead status
    - Address information
    - Activity tracking
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync contact records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw contact records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics
        """
        result = self._init_result()
        self._log_sync_start("contacts", provider, 0)

        contact_list = [c for c in records if isinstance(c, dict)]
        if not contact_list:
            result.complete()
            return {"contacts": 0, **result.to_dict()}

        # Load existing contacts
        contact_external_ids = [str(c.get("id")) for c in contact_list if c.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMContact,
            contact_external_ids,
            integration.integration_id,
        )

        # Pre-load companies referenced by contacts so we can resolve to_local_id where possible.
        referenced_company_external_ids: set[str] = set()
        for raw_contact in contact_list:
            properties = raw_contact.get("properties") or {}
            referenced_company_external_ids.update(
                self._extract_contact_company_external_ids(properties, provider)
            )

        company_local_id_by_external_id: dict[str, UUID] = {}
        if referenced_company_external_ids:
            stmt = (
                select(CRMCompany)
                .where(CRMCompany.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMCompany.external_id.in_(list(referenced_company_external_ids)))  # type: ignore[attr-defined]
            )
            result_companies = await self.db.execute(stmt)
            company_local_id_by_external_id = {
                row.external_id: row.company_id for row in result_companies.scalars().all()
            }

        now = utcnow()
        contacts_processed = 0

        for contact in contact_list:
            try:
                external_id = contact.get("id")
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for contact: {contact}")
                    continue

                # Get or create record
                record = existing.get(str(external_id))
                is_new = record is None

                if not record:
                    record = CRMContact(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=str(external_id),
                    )

                # Transform and update record
                self._update_contact_record(record, contact, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                contacts_processed += 1
                result.increment_processed()

                # Normalize contact ↔ company relationship via crm_object_associations.
                properties = contact.get("properties") or {}
                company_external_ids = self._extract_contact_company_external_ids(
                    properties, provider
                )
                if company_external_ids:
                    await self._upsert_contact_company_associations(
                        integration_id=integration.integration_id,
                        provider=provider,
                        contact_record=record,
                        company_external_ids=company_external_ids,
                        company_local_id_by_external_id=company_local_id_by_external_id,
                        now=now,
                    )

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

            except Exception as exc:
                self._log_error("contact", contact.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit contacts
        await self.db.commit()

        result.metadata.update({"contacts": contacts_processed})
        result.complete()
        self._log_sync_complete(result)

        return {"contacts": contacts_processed, **result.to_dict()}

    async def sync_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync contact records using incremental optimization.

        This method optimizes sync by:
        - Using database state to determine if records are new or existing
        - Running change detection to skip unchanged records
        - Handling archived/deleted records via soft-delete

        Args:
            integration: KnowledgeIntegration instance
            records: Raw contact records with optional _sync_metadata
            provider: Provider name (e.g., 'hubspot', 'salesforce')

        Returns:
            Dictionary with sync statistics including skipped count
        """
        result = self._init_result()
        contact_list = [c for c in records if isinstance(c, dict)]
        self._log_sync_start("contacts (incremental)", provider, len(contact_list))

        if not contact_list:
            result.complete()
            return {
                "contacts": 0,
                "skipped": 0,
                **result.to_dict(),
            }

        # Track action counts for logging (observability only)
        action_counts = SyncActionCounts()

        # Load existing contacts for change detection
        contact_external_ids = [str(c.get("id")) for c in contact_list if c.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMContact,
            contact_external_ids,
            integration.integration_id,
        )

        # Pre-load companies for association resolution
        referenced_company_external_ids: set[str] = set()
        for raw_contact in contact_list:
            properties = raw_contact.get("properties") or {}
            referenced_company_external_ids.update(
                self._extract_contact_company_external_ids(properties, provider)
            )

        company_local_id_by_external_id: dict[str, UUID] = {}
        if referenced_company_external_ids:
            stmt = (
                select(CRMCompany)
                .where(CRMCompany.integration_id == integration.integration_id)
                .where(CRMCompany.external_id.in_(list(referenced_company_external_ids)))
            )
            result_companies = await self.db.execute(stmt)
            company_local_id_by_external_id = {
                row.external_id: row.company_id for row in result_companies.scalars().all()
            }

        now = utcnow()
        contacts_processed = 0
        contacts_skipped = 0

        for contact in contact_list:
            try:
                external_id = contact.get("id")
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for contact: {contact}")
                    continue

                # Extract sync action for logging and deletion handling
                sync_action = extract_sync_action(contact)
                if sync_action:
                    action_counts.increment(sync_action)

                # Check database state - this is the source of truth
                record = existing.get(str(external_id))
                is_new = record is None

                # Check if record is archived/deleted
                is_archived = contact.get("archived", False) or contact.get("IsDeleted", False)

                # Handle deletion
                if is_archived or sync_action == SyncAction.DELETED:
                    if record:
                        record.is_deleted = True
                        record.synced_at = now
                        record.last_seen_at = now
                        self.db.add(record)
                        result.increment_processed()
                        contacts_processed += 1
                        logger.debug("Soft-deleted contact: external_id=%s", external_id)
                    continue

                # Handle new records
                if is_new:
                    record = CRMContact(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=str(external_id),
                    )
                    self._update_contact_record(record, contact, provider)
                    record.synced_at = now
                    record.last_seen_at = now
                    record.is_deleted = False

                    self.db.add(record)
                    contacts_processed += 1
                    result.increment_processed()
                    result.increment_created()

                else:
                    # Existing record - check for meaningful changes
                    if self._has_meaningful_changes(record, contact):
                        self._update_contact_record(record, contact, provider)
                        record.synced_at = now
                        record.last_seen_at = now
                        record.is_deleted = False

                        self.db.add(record)
                        contacts_processed += 1
                        result.increment_processed()
                        result.increment_updated()
                    else:
                        # No meaningful changes - skip database write
                        contacts_skipped += 1
                        action_counts.unchanged += 1
                        logger.debug("Skipping unchanged contact: external_id=%s", external_id)
                        continue

                # Handle company associations for processed contacts
                properties = contact.get("properties") or {}
                company_external_ids = self._extract_contact_company_external_ids(
                    properties, provider
                )
                if company_external_ids:
                    await self._upsert_contact_company_associations(
                        integration_id=integration.integration_id,
                        provider=provider,
                        contact_record=record,
                        company_external_ids=company_external_ids,
                        company_local_id_by_external_id=company_local_id_by_external_id,
                        now=now,
                    )

            except Exception as exc:
                self._log_error("contact", contact.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit contacts
        await self.db.commit()

        result.metadata.update(
            {
                "contacts": contacts_processed,
                "skipped": contacts_skipped,
            }
        )
        result.complete()
        self._log_sync_complete(result)

        logger.info(
            "Contact incremental sync complete: processed=%d skipped=%d (added=%d updated=%d deleted=%d)",
            contacts_processed,
            contacts_skipped,
            action_counts.added,
            action_counts.updated,
            action_counts.deleted,
        )

        return {
            "contacts": contacts_processed,
            "skipped": contacts_skipped,
            "action_counts": action_counts.to_dict(),
            **result.to_dict(),
        }

    def _has_meaningful_changes(self, existing: CRMContact, incoming: dict[str, Any]) -> bool:
        """Check if incoming contact data has meaningful changes.

        Args:
            existing: Existing CRMContact record from database
            incoming: Incoming contact data from provider

        Returns:
            True if meaningful changes detected, False otherwise
        """
        properties = incoming.get("properties") or {}

        # Check source_modified_at as primary indicator
        incoming_modified = self._parse_date(incoming.get("updatedAt")) or self._parse_date(
            properties.get("hs_lastmodifieddate")
        )
        if (
            incoming_modified
            and existing.source_modified_at
            and incoming_modified > existing.source_modified_at
        ):
            return True

        # Compare key fields
        checks = [
            (existing.first_name, properties.get("firstname")),
            (existing.last_name, properties.get("lastname")),
            (existing.email, properties.get("email")),
            (existing.phone, properties.get("phone")),
            (existing.job_title, properties.get("jobtitle") or properties.get("title")),
            (existing.owner_external_id, properties.get("hubspot_owner_id")),
            (existing.lifecycle_stage, properties.get("lifecyclestage")),
            (
                existing.lead_status,
                properties.get("hs_lead_status") or properties.get("leadstatus"),
            ),
            (existing.company_name, properties.get("company")),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        return False

    def _extract_contact_company_external_ids(
        self, properties: dict[str, Any], provider: str
    ) -> list[str]:
        """Extract company/account external IDs from a contact payload.

        We keep this deliberately best-effort and provider-tolerant. The goal is to
        start normalizing contact↔company relationships without requiring extra API calls.
        """

        candidates: list[Any] = []

        # HubSpot: primary associated company id
        # Commonly stored as string numeric.
        candidates.append(properties.get("associatedcompanyid"))

        # Salesforce: primary AccountId on Contact
        candidates.append(properties.get("accountid"))
        candidates.append(properties.get("account_id"))
        candidates.append(properties.get("accountId"))

        # Pipedrive: person org id
        candidates.append(properties.get("org_id"))
        candidates.append(properties.get("orgid"))

        company_ids: list[str] = []
        for value in candidates:
            if value is None:
                continue
            if isinstance(value, dict):
                # Pipedrive org_id can sometimes be an object with {value, name}
                dict_value = value.get("value")
                if dict_value is not None:
                    company_ids.append(str(dict_value))
                continue
            if isinstance(value, str | int):
                value_str = str(value).strip()
                if value_str:
                    company_ids.append(value_str)

        # De-dupe while preserving order
        seen: set[str] = set()
        unique_company_ids: list[str] = []
        for cid in company_ids:
            if cid not in seen:
                seen.add(cid)
                unique_company_ids.append(cid)

        # Provider-specific: if we ever want to add additional keys, keep it contained here.
        _ = provider
        return unique_company_ids

    async def _upsert_contact_company_associations(
        self,
        *,
        integration_id: UUID,
        provider: str,
        contact_record: CRMContact,
        company_external_ids: list[str],
        company_local_id_by_external_id: dict[str, UUID],
        now: datetime,
    ) -> None:
        """Upsert contact→company associations into crm_object_associations."""

        if provider == "hubspot":
            association_category = CRMAssociationCategory.HUBSPOT_DEFINED.value
        elif provider == "salesforce":
            association_category = CRMAssociationCategory.SALESFORCE_STANDARD.value
        elif provider == "pipedrive":
            association_category = CRMAssociationCategory.PIPEDRIVE_STANDARD.value
        elif provider == "zoho":
            association_category = CRMAssociationCategory.ZOHO_STANDARD.value
        else:
            association_category = CRMAssociationCategory.OTHER.value

        # Primary association: first company ID is considered primary.
        for index, company_external_id in enumerate(company_external_ids):
            is_primary = index == 0
            to_local_id = company_local_id_by_external_id.get(company_external_id)

            insert_stmt = pg_insert(CRMObjectAssociation).values(
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                is_deleted=False,
                external_id=None,
                from_object_type=CRMObjectType.CONTACT.value,
                from_external_id=contact_record.external_id,
                from_local_id=contact_record.contact_id,
                to_object_type=CRMObjectType.COMPANY.value,
                to_external_id=company_external_id,
                to_local_id=to_local_id,
                association_category=association_category,
                association_type_id="contact_company",
                association_label=None,
                role_label="primary" if is_primary else None,
                is_primary=is_primary,
                properties={},
                source_created_at=None,
                source_modified_at=contact_record.source_modified_at,
                synced_at=now,
                last_seen_at=now,
                data_payload=None,
            )

            # Upsert based on the logical uniqueness constraint.
            upsert_stmt = insert_stmt.on_conflict_do_update(
                index_elements=[
                    CRMObjectAssociation.organisation_id,
                    CRMObjectAssociation.integration_id,
                    CRMObjectAssociation.from_object_type,
                    CRMObjectAssociation.from_external_id,
                    CRMObjectAssociation.to_object_type,
                    CRMObjectAssociation.to_external_id,
                    CRMObjectAssociation.association_type_id,
                ],
                set_={
                    "updated_at": now,
                    "is_deleted": False,
                    "from_local_id": contact_record.contact_id,
                    "to_local_id": to_local_id,
                    "association_category": association_category,
                    "role_label": "primary" if is_primary else None,
                    "is_primary": is_primary,
                    "source_modified_at": contact_record.source_modified_at,
                    "synced_at": now,
                    "last_seen_at": now,
                },
            )

            await self.db.execute(upsert_stmt)

    def _transform_record(self, raw: dict[str, Any], _provider: str) -> CRMContact:
        """Transform provider-specific contact data to CRMContact model.

        Args:
            raw: Raw contact record from provider
            _provider: Provider name (unused, kept for interface compatibility)

        Returns:
            Transformed CRMContact instance
        """
        # Simplified transformation - actual logic in _update_contact_record
        return CRMContact(
            organisation_id=self.organisation_id,
            integration_id=UUID("00000000-0000-0000-0000-000000000000"),
            external_id=str(raw.get("id", "")),
            email=raw.get("properties", {}).get("email"),
        )

    def _update_contact_record(
        self,
        record: CRMContact,
        raw: dict[str, Any],
        _provider: str,
    ) -> None:
        """Update CRMContact record with data from provider.

        Args:
            record: Existing CRMContact instance to update
            raw: Raw data from provider
            _provider: Provider name (unused, kept for interface compatibility)
        """
        properties = raw.get("properties") or {}
        updated_at = (
            self._parse_date(raw.get("updatedAt"))
            or self._parse_date(properties.get("hs_lastmodifieddate"))
            or self._parse_date(properties.get("lastmodifieddate"))
        )
        created_at = self._parse_date(raw.get("createdAt")) or self._parse_date(
            properties.get("createdate")
        )

        # Update all contact sections
        self._update_email_fields(record, properties)
        self._update_name_fields(record, properties)
        self._update_professional_fields(record, properties)
        self._update_status_fields(record, properties)
        self._update_contact_owner_fields(record, properties, raw)
        self._update_contact_info_fields(record, properties)
        self._update_address_fields(record, properties)
        self._update_activity_fields(record, properties, updated_at)
        self._update_contact_raw_data(record, raw, properties, created_at, updated_at)

    def _update_email_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update email-related fields."""
        record.email = properties.get("email")
        additional_emails = self._parse_email_list(
            properties.get("hs_additional_emails") or properties.get("additional_emails")
        )
        record.additional_emails = additional_emails if additional_emails else []

    def _update_name_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update name-related fields."""
        first_name = properties.get("firstname")
        last_name = properties.get("lastname")
        record.first_name = first_name
        record.last_name = last_name
        record.full_name = properties.get("fullname") or self._build_full_name(
            first_name, last_name, properties.get("company")
        )

    def _update_professional_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update professional information fields."""
        record.job_title = properties.get("jobtitle") or properties.get("title")
        record.company_name = properties.get("company")

    def _update_status_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update status-related fields."""
        record.lifecycle_stage = properties.get("lifecyclestage")
        record.lead_status = properties.get("hs_lead_status") or properties.get("leadstatus")
        record.marketing_status = properties.get("hs_marketable_status") or properties.get(
            "hs_content_membership_email_status"
        )

    def _update_contact_owner_fields(
        self, record: CRMContact, properties: dict[str, Any], raw: dict[str, Any] | None = None
    ) -> None:
        """Update owner information fields."""
        raw = raw or {}

        # Try multiple possible property names for owner ID
        # Check both properties dict and root level of raw object
        owner_id = (
            properties.get("hubspot_owner_id")
            or properties.get("hs_owner_id")
            or properties.get("owner_id")
            or properties.get("ownerId")
            or raw.get("hubspot_owner_id")
            or raw.get("ownerId")
            or raw.get("owner_id")
        )
        owner_email = (
            properties.get("hubspot_owner_email")
            or properties.get("hs_owner_email")
            or properties.get("owner_email")
            or raw.get("hubspot_owner_email")
            or raw.get("owner_email")
        )
        owner_name = (
            properties.get("hubspot_owner_name")
            or properties.get("hs_owner_name")
            or properties.get("owner_name")
            or raw.get("hubspot_owner_name")
            or raw.get("owner_name")
        )

        # Log owner fields for debugging - always log first few contacts
        # Also log any keys that contain 'owner' for debugging
        owner_related_props = [k for k in properties if "owner" in k.lower()]
        owner_related_raw = [k for k in raw if "owner" in k.lower()]
        logger.info(
            "Contact owner fields from HubSpot: contact_external_id=%s, "
            "hubspot_owner_id=%r, owner_email=%r, owner_name=%r, "
            "owner_props=%s, owner_raw=%s, raw_keys=%s",
            record.external_id,
            owner_id,
            owner_email,
            owner_name,
            owner_related_props,
            owner_related_raw,
            list(raw.keys())[:15],  # Root level keys for debugging
        )

        # Set owner_external_id if we have a valid (non-empty) owner ID
        if owner_id and str(owner_id).strip():
            record.owner_external_id = str(owner_id)
            logger.info(
                "Set owner_external_id=%s for contact %s",
                record.owner_external_id,
                record.external_id,
            )

        if isinstance(owner_email, str) and owner_email.strip():
            record.owner_email = owner_email

        if isinstance(owner_name, str) and owner_name.strip():
            record.owner_name = owner_name

    def _update_contact_info_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update contact information fields (phone, etc)."""
        record.phone = properties.get("phone")
        record.mobile_phone = properties.get("mobilephone")
        record.fax = properties.get("fax")
        record.website = properties.get("website")

    def _update_address_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update address-related fields."""
        record.address = properties.get("address")
        record.city = properties.get("city")
        record.state = properties.get("state") or properties.get("state_region")
        record.postal_code = properties.get("zip") or properties.get("postalcode")
        record.country = properties.get("country")
        record.timezone = properties.get("timezone")

    def _update_activity_fields(
        self, record: CRMContact, properties: dict[str, Any], updated_at: datetime | None
    ) -> None:
        """Update activity tracking fields."""
        record.last_contacted_at = self._parse_date(
            properties.get("last_contacted")
        ) or self._parse_date(properties.get("hs_last_contacted"))
        record.last_activity_at = (
            self._parse_date(properties.get("lastactivitydate"))
            or self._parse_date(properties.get("hs_lastactivitydate"))
            or self._parse_date(properties.get("recent_activity_date"))
            or updated_at
        )

    def _update_contact_raw_data(
        self,
        record: CRMContact,
        raw: dict[str, Any],
        properties: dict[str, Any],
        created_at: datetime | None,
        updated_at: datetime | None,
    ) -> None:
        """Update raw data storage fields."""
        record.source_created_at = created_at
        record.source_modified_at = updated_at
        record.properties = {k: v for k, v in properties.items() if v is not None}
        record.metrics = self._extract_contact_metrics(properties)
        record.data_payload = raw

    def _parse_email_list(self, value: Any) -> list[str] | None:
        """Parse comma-separated email list.

        Args:
            value: String with semicolon or comma-separated emails

        Returns:
            List of email addresses or None
        """
        if not value or not isinstance(value, str):
            return None

        # Split by semicolon or comma
        if ";" in value:
            emails = [e.strip() for e in value.split(";") if e.strip()]
        else:
            emails = [e.strip() for e in value.split(",") if e.strip()]

        return emails if emails else None

    def _build_full_name(
        self,
        first_name: str | None,
        last_name: str | None,
        company: str | None,
    ) -> str | None:
        """Build full name from components.

        Args:
            first_name: First name
            last_name: Last name
            company: Company name (fallback if no personal name)

        Returns:
            Combined full name or None
        """
        parts = []
        if first_name:
            parts.append(first_name)
        if last_name:
            parts.append(last_name)

        if parts:
            return " ".join(parts)
        elif company:
            return company

        return None

    def _extract_contact_metrics(self, properties: dict[str, Any]) -> dict[str, Any]:
        """Extract metric fields from contact properties.

        Args:
            properties: Raw contact properties

        Returns:
            Dictionary of metrics
        """
        metrics = {}

        # Engagement metrics
        metric_keys = [
            "hs_email_open",
            "hs_email_click",
            "hs_email_bounce",
            "num_notes",
            "num_contacted_notes",
            "num_conversion_events",
            "hs_analytics_num_page_views",
            "hs_analytics_num_visits",
            "hs_analytics_num_event_completions",
        ]

        for key in metric_keys:
            if key in properties and properties[key] is not None:
                try:
                    metrics[key] = int(properties[key])
                except (TypeError, ValueError):
                    metrics[key] = properties[key]

        return metrics

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
