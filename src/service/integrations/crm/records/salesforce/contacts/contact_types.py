"""DTOs for Salesforce contacts record sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceContact:
    external_id: str
    first_name: str | None
    last_name: str | None
    full_name: str | None
    job_title: str | None
    company_name: str | None
    company_external_id: str | None
    email: str | None
    additional_emails: tuple[str, ...]
    phone: str | None
    mobile_phone: str | None
    fax: str | None
    website: str | None
    owner_external_id: str | None
    lifecycle_stage: str | None
    lead_status: str | None
    marketing_status: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    timezone: str | None
    last_contacted_at: datetime | None
    last_activity_at: datetime | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    properties: dict[str, Any]
    data_payload: dict[str, Any]


@dataclass(slots=True)
class ContactWriteResult:
    contacts_created: int = 0
    contacts_updated: int = 0
    unresolved_company_link: int = 0
    unresolved_owner_link: int = 0


@dataclass(slots=True)
class ContactSyncResult(RecordSyncResult):
    fetched_contacts: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    unresolved_company_link: int = 0
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
                "fetched_contacts": self.fetched_contacts,
                "contacts_created": self.contacts_created,
                "contacts_updated": self.contacts_updated,
                "unresolved_company_link": self.unresolved_company_link,
                "unresolved_owner_link": self.unresolved_owner_link,
                "skipped_malformed": self.skipped_malformed,
                "deal_backfill_rows_updated": self.deal_backfill_rows_updated,
                "deal_backfill_per_op": self.deal_backfill_per_op,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
