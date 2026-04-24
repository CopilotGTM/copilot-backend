"""DTOs for Salesforce companies record sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceCompany:
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
    companies_created: int = 0
    companies_updated: int = 0
    companies_reactivated: int = 0
    companies_deactivated: int = 0
    unresolved_owner_link: int = 0


@dataclass(slots=True)
class CompanySyncResult(RecordSyncResult):
    fetched_companies: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    companies_reactivated: int = 0
    companies_deactivated: int = 0
    unresolved_owner_link: int = 0
    skipped_malformed: int = 0
    deal_backfill_rows_updated: int = 0
    deal_backfill_per_op: dict[str, int] = field(default_factory=dict)
    error_class: str | None = None
    error_message: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        payload = self.base_metadata()
        payload.update(
            {
                "fetched_companies": self.fetched_companies,
                "companies_created": self.companies_created,
                "companies_updated": self.companies_updated,
                "companies_reactivated": self.companies_reactivated,
                "companies_deactivated": self.companies_deactivated,
                "unresolved_owner_link": self.unresolved_owner_link,
                "skipped_malformed": self.skipped_malformed,
                "deal_backfill_rows_updated": self.deal_backfill_rows_updated,
                "deal_backfill_per_op": self.deal_backfill_per_op,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
