"""Contact entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import CRMContact
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService

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
        self._update_contact_owner_fields(record, properties)
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

    def _update_contact_owner_fields(self, record: CRMContact, properties: dict[str, Any]) -> None:
        """Update owner information fields."""
        owner_id = properties.get("hubspot_owner_id")
        if owner_id is not None:
            record.owner_external_id = owner_id

        owner_email = properties.get("hubspot_owner_email")
        if isinstance(owner_email, str):
            record.owner_email = owner_email

        owner_name = properties.get("hubspot_owner_name")
        if isinstance(owner_name, str):
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
