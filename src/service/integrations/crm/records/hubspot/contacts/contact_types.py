"""DTOs for HubSpot contacts record sync.

No matching step (contacts are external people, not app users). Every
well-formed, non-archived contact is persisted; `company_id` is resolved at
write time from the in-session `{company_external_id → company_id}` map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedHubSpotContact:
    """A HubSpot contact after normalisation — one-to-one with CRMContact columns.

    `company_id` is NOT carried here; it's resolved by the writer against the
    live `crm_companies` snapshot. `company_external_id` is the hand-off that
    later lets us backfill unresolved links.
    """

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
    """Counters returned from `HubSpotContactWriter.apply()`.

    Note on deactivation semantics: `CRMContact` has no `is_active` column
    today, so there is no "deactivated" counter. Contacts are never
    hard-deleted on departure — dropping to `is_deleted=True` would break
    the PK-reuse invariant if the person returns. Adding `is_active` to
    CRMContact (mirroring users/companies) is a deferred follow-up, at which
    point a `contacts_deactivated` counter should be introduced here.
    """

    contacts_created: int = 0
    contacts_updated: int = 0
    contacts_reactivated: int = 0
    contacts_skipped_stale: int = 0
    # How many contacts declared a company_external_id but failed resolution
    # against the live crm_companies snapshot (company archived, not yet
    # synced, or unknown). Row still persisted with company_id=NULL.
    unresolved_company_link: int = 0
    # Same for owner: contact declared owner_external_id but no crm_users row
    # matched. Row still persisted with owner_id=NULL.
    unresolved_owner_link: int = 0


@dataclass(slots=True)
class ContactSyncResult(RecordSyncResult):
    """End-to-end sync summary for HubSpot contacts record sync."""

    fetched_contacts: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    contacts_reactivated: int = 0
    contacts_skipped_stale: int = 0
    unresolved_company_link: int = 0
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
                "fetched_contacts": self.fetched_contacts,
                "contacts_created": self.contacts_created,
                "contacts_updated": self.contacts_updated,
                "contacts_reactivated": self.contacts_reactivated,
                "contacts_skipped_stale": self.contacts_skipped_stale,
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
