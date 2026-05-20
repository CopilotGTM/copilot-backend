"""DTOs for HubSpot companies (accounts) record sync.

Unlike the users phase, there is no matching step — every well-formed,
non-archived company is persisted. The `NormalizedHubSpotCompany` DTO carries
exactly what `CRMCompany` needs at write time; overflow / extra provider
properties land in `properties` JSONB, and the raw HubSpot record is kept in
`data_payload` for replay/debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedHubSpotCompany:
    """A HubSpot company after normalisation — one-to-one with CRMCompany columns."""

    external_id: str
    name: str | None
    domain: str | None
    website: str | None
    description: str | None
    industry: str | None
    company_type: str | None
    lifecycle_stage: str | None
    owner_external_id: str | None
    number_of_employees: int | None
    annual_revenue: float | None
    phone: str | None
    city: str | None
    state: str | None
    country: str | None
    address: str | None
    postal_code: str | None
    timezone: str | None
    linkedin_url: str | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    properties: dict[str, Any]
    data_payload: dict[str, Any]


@dataclass(slots=True)
class CompanyWriteResult:
    """Counters returned from `HubSpotCompanyWriter.apply()`."""

    companies_created: int = 0
    companies_updated: int = 0
    companies_reactivated: int = 0
    companies_deactivated: int = 0
    companies_skipped_stale: int = 0
    skipped_stale_external_ids: set[str] = field(default_factory=set)
    # How many companies declared an owner_external_id but failed resolution
    # against the live crm_users snapshot (owner archived, not yet synced,
    # or unknown). Row still persisted with owner_id=NULL.
    unresolved_owner_link: int = 0


@dataclass(slots=True)
class CompanySyncResult(RecordSyncResult):
    """End-to-end sync summary for HubSpot companies record sync."""

    fetched_companies: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    companies_reactivated: int = 0
    companies_deactivated: int = 0
    companies_skipped_stale: int = 0
    unresolved_owner_link: int = 0
    skipped_malformed: int = 0
    deal_backfill_rows_updated: int = 0
    deal_backfill_per_op: dict[str, int] = field(default_factory=dict)
    error_class: str | None = None
    error_message: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Build task-safe metadata from the result counters."""

        payload = self.base_metadata()
        payload.update(
            {
                "fetched_companies": self.fetched_companies,
                "companies_created": self.companies_created,
                "companies_updated": self.companies_updated,
                "companies_reactivated": self.companies_reactivated,
                "companies_deactivated": self.companies_deactivated,
                "companies_skipped_stale": self.companies_skipped_stale,
                "unresolved_owner_link": self.unresolved_owner_link,
                "skipped_malformed": self.skipped_malformed,
                "deal_backfill_rows_updated": self.deal_backfill_rows_updated,
                "deal_backfill_per_op": self.deal_backfill_per_op,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
