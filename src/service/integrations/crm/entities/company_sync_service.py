"""Company entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import CRMCompany
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService

logger = get_logger(__name__)


class CompanySyncService(BaseSyncService[CRMCompany]):
    """Service for syncing CRM company/account data.

    Handles synchronization of company records with properties including:
    - Company information (name, domain, website)
    - Business metrics (revenue, employees)
    - Owner associations
    - Address information
    - Social media links
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync company records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw company records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics
        """
        result = self._init_result()
        self._log_sync_start("companies", provider, 0)

        company_list = [c for c in records if isinstance(c, dict)]
        if not company_list:
            result.complete()
            return {"companies": 0, **result.to_dict()}

        # Load existing companies
        company_external_ids = [str(c.get("id")) for c in company_list if c.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMCompany,
            company_external_ids,
            integration.integration_id,
        )

        now = utcnow()
        companies_processed = 0

        for company in company_list:
            try:
                external_id = company.get("id")
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for company: {company}")
                    continue

                # Get or create record
                record = existing.get(str(external_id))
                is_new = record is None

                if not record:
                    record = CRMCompany(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=str(external_id),
                    )

                # Transform and update record
                self._update_company_record(record, company, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_active = True
                record.is_deleted = False

                self.db.add(record)
                companies_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

            except Exception as exc:
                self._log_error("company", company.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit companies
        await self.db.commit()

        result.metadata.update({"companies": companies_processed})
        result.complete()
        self._log_sync_complete(result)

        return {"companies": companies_processed, **result.to_dict()}

    def _transform_record(self, raw: dict[str, Any], _provider: str) -> CRMCompany:
        """Transform provider-specific company data to CRMCompany model.

        Args:
            raw: Raw company record from provider
            _provider: Provider name (unused, kept for interface compatibility)

        Returns:
            Transformed CRMCompany instance
        """
        # Simplified transformation - actual logic in _update_company_record
        return CRMCompany(
            organisation_id=self.organisation_id,
            integration_id=UUID("00000000-0000-0000-0000-000000000000"),
            external_id=str(raw.get("id", "")),
            name=raw.get("properties", {}).get("name") or raw.get("name"),
        )

    def _update_company_record(
        self,
        record: CRMCompany,
        raw: dict[str, Any],
        _provider: str,
    ) -> None:
        """Update CRMCompany record with data from provider.

        Args:
            record: Existing CRMCompany instance to update
            raw: Raw data from provider
            _provider: Provider name (unused, kept for interface compatibility)
        """
        properties = raw.get("properties") or {}
        updated_at = self._parse_date(raw.get("updatedAt")) or self._parse_date(
            properties.get("hs_lastmodifieddate")
        )
        created_at = self._parse_date(raw.get("createdAt")) or self._parse_date(
            properties.get("createdate")
        )

        # Update all company sections
        self._update_core_company_fields(record, raw, properties)
        self._update_company_owner_fields(record, properties)
        self._update_business_metrics(record, properties)
        self._update_company_contact_info(record, properties)
        self._update_company_social_links(record, properties)
        self._update_company_raw_data(record, raw, properties, created_at, updated_at)

    def _update_core_company_fields(
        self, record: CRMCompany, raw: dict[str, Any], properties: dict[str, Any]
    ) -> None:
        """Update core company information fields."""
        record.name = properties.get("name") or raw.get("name")
        record.domain = properties.get("domain")
        record.website = properties.get("website")
        record.description = properties.get("description")
        record.industry = properties.get("industry")
        record.company_type = properties.get("type")
        record.lifecycle_stage = properties.get("lifecyclestage")

    def _update_company_owner_fields(self, record: CRMCompany, properties: dict[str, Any]) -> None:
        """Update company owner information fields."""
        owner_id = properties.get("hubspot_owner_id")
        if owner_id is not None:
            record.owner_external_id = str(owner_id)

        owner_email = properties.get("hubspot_owner_email")
        if isinstance(owner_email, str):
            record.owner_email = owner_email

        owner_name = properties.get("hubspot_owner_name")
        if isinstance(owner_name, str):
            record.owner_name = owner_name

    def _update_business_metrics(self, record: CRMCompany, properties: dict[str, Any]) -> None:
        """Update business metrics fields."""
        num_employees = properties.get("numberofemployees")
        if num_employees is not None:
            try:
                record.number_of_employees = int(num_employees)
            except (ValueError, TypeError):
                record.number_of_employees = None

        annual_rev = properties.get("annualrevenue")
        if annual_rev is not None:
            try:
                record.annual_revenue = float(annual_rev)
            except (ValueError, TypeError):
                record.annual_revenue = None

    def _update_company_contact_info(self, record: CRMCompany, properties: dict[str, Any]) -> None:
        """Update company contact information fields."""
        record.phone = properties.get("phone")
        record.address = properties.get("address") or properties.get("address2")
        record.city = properties.get("city")
        record.state = properties.get("state")
        record.country = properties.get("country")
        record.postal_code = properties.get("zip")
        record.timezone = properties.get("timezone")

    def _update_company_social_links(self, record: CRMCompany, properties: dict[str, Any]) -> None:
        """Update company social media links."""
        linkedin = properties.get("linkedin_company_page") or properties.get("linkedinbio")
        if isinstance(linkedin, str):
            record.linkedin_url = linkedin

    def _update_company_raw_data(
        self,
        record: CRMCompany,
        raw: dict[str, Any],
        properties: dict[str, Any],
        created_at: datetime | None,
        updated_at: datetime | None,
    ) -> None:
        """Update raw data storage fields."""
        record.source_created_at = created_at
        record.source_modified_at = updated_at
        record.properties = properties
        record.data_payload = raw

        # Metrics
        metrics = {}
        if properties.get("hs_total_deal_value"):
            metrics["total_deal_value"] = properties.get("hs_total_deal_value")
        if properties.get("total_revenue"):
            metrics["total_revenue"] = properties.get("total_revenue")
        if properties.get("total_money_raised"):
            metrics["total_money_raised"] = properties.get("total_money_raised")
        record.metrics = metrics

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
